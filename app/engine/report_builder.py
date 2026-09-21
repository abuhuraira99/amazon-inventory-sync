"""
The five report files produced by each run.

WHY REPORTS ARE A FIRST-CLASS OUTPUT
====================================
A sync tool that only syncs is hard to trust and hard to adopt. These files are
what the system produces *before* anybody enables writes, and they stay useful
afterwards: In Stock and New Products answer questions an operator has anyway,
independently of whether the automation is turned on.

That also makes the safest deployment path a genuinely useful one. Running in
practice mode produces every report and sends nothing, so the system earns
trust by being useful first and authoritative second.

All five are produced, for every full-feed run AND every delta run, kept
separately, with a history rather than only the newest copy.

The five:
  1. **In Stock**            products that came back into stock, or whose stock
                             changed. The team's main working file.
  2. **New Products**        barcodes never seen before, with stock. Their other
                             main file -- these are listing opportunities.
  3. **Out of Stock**        products that dropped to zero.
  4. **Price Changed**       vendor cost movements. Informational: this system
                             never sends a price to Amazon.
  5. **Current In Stock DB** the whole current in-stock catalogue.

WHY .XLSX AND NOT .CSV
======================
Because Excel destroys barcodes.

Open a CSV containing ``0007298811035`` in Excel and it becomes the number
7298811035 -- the leading zeros are gone, silently, on open. The team then
copies that into the Amazon template and produces a SKU that does not exist.
Since a large share of any zero-padded catalogue depends on those leading
zeros, a CSV pipeline is actively dangerous for this data.

Real .xlsx with the barcode and SKU columns declared as **text** cannot be
mangled that way. Excel opens them as strings and leaves them alone.

XlsxWriter runs in ``constant_memory`` mode, so the Current In Stock report --
which covers the whole in-stock catalogue and can run to hundreds of thousands
of rows -- streams to disk instead of being assembled in RAM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import xlsxwriter

from app.core.barcode import build_sku
from app.models import FeedKind

log = logging.getLogger(__name__)

#: Report kinds, in the order the dashboard shows them. The two the team
#: actually uses come first.
REPORT_KINDS = (
    "in_stock",
    "new_products",
    "out_of_stock",
    "price_changed",
    "current_in_stock_database",
)

REPORT_TITLES = {
    "in_stock": "In Stock",
    "new_products": "New Products",
    "out_of_stock": "Out of Stock",
    "price_changed": "Price Changed",
    "current_in_stock_database": "Current In Stock Database",
}

REPORT_DESCRIPTIONS = {
    "in_stock": "Products that came back into stock, or whose stock level changed.",
    "new_products": "Barcodes never seen in a feed before that have stock. Listing opportunities.",
    "out_of_stock": "Products whose stock dropped to zero at the vendor.",
    "price_changed": "Vendor cost changes. For information only - this system never changes an Amazon price.",
    "current_in_stock_database": "Every product the vendor currently has in stock.",
}


@dataclass(slots=True)
class ReportRow:
    """One line of a report."""

    barcode: str
    brand: str = ""
    title: str = ""
    product_format: str = ""
    vendor_stock: int | None = None
    previous_stock: int | None = None
    vendor_price: float | None = None
    previous_price: float | None = None
    amazon_sku: str | None = None
    amazon_quantity: int | None = None
    published_quantity: int | None = None
    note: str = ""


@dataclass(slots=True)
class WrittenReport:
    """Where a report ended up."""

    kind: str
    path: Path
    row_count: int
    size_bytes: int


# ===========================================================================
# Column layouts
# ===========================================================================
# (header, attribute, width, format) -- "text" forces Excel to leave the value
# alone, which is the whole point for barcode and SKU columns.

_COMMON_HEAD: list[tuple[str, str, int, str]] = [
    ("Barcode", "barcode", 16, "text"),
    ("SKU", "amazon_sku", 24, "text"),
    ("Brand", "brand", 28, "general"),
    ("Title", "title", 40, "general"),
    ("Format", "product_format", 9, "general"),
]

LAYOUTS: dict[str, list[tuple[str, str, int, str]]] = {
    "in_stock": [
        *_COMMON_HEAD,
        ("Stock Now", "vendor_stock", 11, "int"),
        ("Stock Before", "previous_stock", 13, "int"),
        ("Published to Amazon", "published_quantity", 19, "int"),
        ("Amazon Shows", "amazon_quantity", 14, "int"),
        ("Vendor Price", "vendor_price", 13, "money"),
        ("Note", "note", 46, "general"),
    ],
    "new_products": [
        *_COMMON_HEAD,
        ("Stock", "vendor_stock", 9, "int"),
        ("Vendor Price", "vendor_price", 13, "money"),
        ("Note", "note", 46, "general"),
    ],
    "out_of_stock": [
        *_COMMON_HEAD,
        ("Stock Before", "previous_stock", 13, "int"),
        ("Amazon Shows", "amazon_quantity", 14, "int"),
        ("Note", "note", 46, "general"),
    ],
    "price_changed": [
        *_COMMON_HEAD,
        ("Price Now", "vendor_price", 12, "money"),
        ("Price Before", "previous_price", 13, "money"),
        ("Stock", "vendor_stock", 9, "int"),
        ("Note", "note", 46, "general"),
    ],
    "current_in_stock_database": [
        *_COMMON_HEAD,
        ("Stock", "vendor_stock", 9, "int"),
        ("Published to Amazon", "published_quantity", 19, "int"),
        ("Amazon Shows", "amazon_quantity", 14, "int"),
        ("Vendor Price", "vendor_price", 13, "money"),
    ],
}


# ===========================================================================
# Writing
# ===========================================================================

def write_report(
    kind: str,
    rows: Iterable[ReportRow],
    *,
    directory: Path,
    run_id: int,
    feed_kind: FeedKind = FeedKind.UNKNOWN,
    feed_filename: str = "",
    sku_prefix: str = "EXAMPLE-",
    generated_at: datetime | None = None,
) -> WrittenReport:
    """
    Write one report as .xlsx and return where it went.

    Rows are streamed, so even a very large report costs almost no memory.

    The filename carries the run id, the feed type and a timestamp, so a delta
    report can never be mistaken for the daily full-feed report -- which was an
    explicit requirement.
    """
    if kind not in LAYOUTS:
        raise ValueError(f"unknown report kind {kind!r}; expected one of {REPORT_KINDS}")

    generated_at = generated_at or datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = generated_at.strftime("%Y%m%d-%H%M%S")
    label = feed_kind.value if feed_kind is not FeedKind.UNKNOWN else "run"
    path = directory / f"{stamp}_run{run_id}_{label}_{kind}.xlsx"

    layout = LAYOUTS[kind]

    workbook = xlsxwriter.Workbook(
        str(path),
        {
            # Rows are flushed as they are written. Required for the large
            # report; harmless for the small ones.
            "constant_memory": True,
            "default_date_format": "yyyy-mm-dd",
            "strings_to_numbers": False,   # never re-interpret a barcode
            "strings_to_formulas": False,  # a title starting with "=" is text
        },
    )

    fmt = {
        "header": workbook.add_format(
            {
                "bold": True,
                "bg_color": "#1F3A5F",
                "font_color": "#FFFFFF",
                "border": 1,
                "border_color": "#16293F",
                "align": "left",
                "valign": "vcenter",
                "text_wrap": True,
            }
        ),
        "title": workbook.add_format({"bold": True, "font_size": 13}),
        "sub": workbook.add_format({"font_color": "#555555", "italic": True}),
        # num_format "@" is Excel's text format. This is the line that stops
        # 0007298811035 from becoming 7298811035.
        "text": workbook.add_format({"num_format": "@"}),
        "int": workbook.add_format({"num_format": "0", "align": "right"}),
        "money": workbook.add_format({"num_format": "0.00", "align": "right"}),
        "general": workbook.add_format({}),
    }

    sheet = workbook.add_worksheet(REPORT_TITLES[kind][:31])

    # -- title block -------------------------------------------------------
    sheet.write(0, 0, REPORT_TITLES[kind], fmt["title"])
    source = f"  |  source: {feed_filename}" if feed_filename else ""
    sheet.write(
        1, 0,
        f"{REPORT_DESCRIPTIONS[kind]}  |  run {run_id}  |  "
        f"{generated_at.strftime('%d %b %Y %H:%M UTC')}{source}",
        fmt["sub"],
    )
    sheet.write(
        2, 0,
        "Barcode and SKU are stored as text so Excel keeps their leading zeros. "
        "Do not convert this file to CSV.",
        fmt["sub"],
    )

    HEADER_ROW = 4

    for col, (header, _attr, width, _kind) in enumerate(layout):
        sheet.write(HEADER_ROW, col, header, fmt["header"])
        sheet.set_column(col, col, width, fmt["text"] if _kind == "text" else None)

    sheet.freeze_panes(HEADER_ROW + 1, 0)
    sheet.autofilter(HEADER_ROW, 0, HEADER_ROW, len(layout) - 1)

    # -- data --------------------------------------------------------------
    count = 0
    for r in rows:
        row_index = HEADER_ROW + 1 + count

        # Derive the SKU if the caller did not supply one, so every report is
        # directly usable in the Amazon template.
        if not r.amazon_sku and r.barcode:
            r.amazon_sku = build_sku(sku_prefix, r.barcode) or ""

        for col, (_header, attr, _width, cell_kind) in enumerate(layout):
            value = getattr(r, attr, None)

            if value is None or value == "":
                sheet.write_blank(row_index, col, None)
                continue

            if cell_kind == "text":
                # write_string, never write() -- write() would let XlsxWriter
                # decide it looks like a number.
                sheet.write_string(row_index, col, str(value), fmt["text"])
            elif cell_kind == "int":
                sheet.write_number(row_index, col, int(value), fmt["int"])
            elif cell_kind == "money":
                sheet.write_number(row_index, col, float(value), fmt["money"])
            else:
                sheet.write_string(row_index, col, str(value), fmt["general"])

        count += 1

    workbook.close()

    size = path.stat().st_size
    log.info("wrote %s: %d rows, %s bytes -> %s", kind, count, f"{size:,}", path.name)
    return WrittenReport(kind=kind, path=path, row_count=count, size_bytes=size)


def write_all_reports(
    data: dict[str, Sequence[ReportRow]],
    *,
    directory: Path,
    run_id: int,
    feed_kind: FeedKind = FeedKind.UNKNOWN,
    feed_filename: str = "",
    sku_prefix: str = "EXAMPLE-",
) -> list[WrittenReport]:
    """
    Write all five reports.

    A kind absent from ``data`` still produces an empty file with its headers.
    That is deliberate: the team downloads the same five files every time, and
    a missing file is indistinguishable from a broken run, whereas an empty one
    plainly says "nothing in this category today".
    """
    written: list[WrittenReport] = []
    generated_at = datetime.now(UTC)

    for kind in REPORT_KINDS:
        written.append(
            write_report(
                kind,
                data.get(kind, ()),
                directory=directory,
                run_id=run_id,
                feed_kind=feed_kind,
                feed_filename=feed_filename,
                sku_prefix=sku_prefix,
                generated_at=generated_at,
            )
        )
    return written


# ===========================================================================
# Housekeeping
# ===========================================================================

def prune_old_reports(directory: Path, *, keep_days: int) -> int:
    """
    Delete report files older than ``keep_days``. Returns how many went.

    Reports are the biggest thing this system writes to disk: five files per
    run, every run. At a 15-minute cycle that is 480 files a day, and the
    Current In Stock report alone is several megabytes. Without pruning, a
    modest disk fills in weeks, and a full disk stops the sync.

    Only the files are removed. The :class:`app.models.ReportFile` rows stay,
    so the history remains visible with the file marked as expired.
    """
    if keep_days <= 0 or not directory.exists():
        return 0

    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    removed = 0
    for path in directory.glob("*.xlsx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover
            log.warning("could not delete old report %s: %s", path.name, exc)

    if removed:
        log.info("pruned %d report files older than %d days", removed, keep_days)
    return removed


def prune_empty_report_directories(reports_root: Path) -> int:
    """
    Remove the now-empty ``run-N`` folders left behind after pruning.

    Returns how many went.

    WHY THIS IS NEEDED
    ==================
    Every run writes its five reports into its own ``reports/run-<id>``
    directory, and the pruner deletes the FILES when they age out. Nothing ever
    removed the directory, so one empty folder was left behind per run --
    permanently. At an hourly schedule that is thousands of empty directories
    a year, leaving an operator to open the reports folder and find no way to
    tell which of them still hold anything.

    The folders are litter, not history: the run history lives in the database,
    and a download resolves the file's full path from its ``ReportFile`` row
    rather than by scanning directories. Nothing in the application reads a
    report directory -- it is only ever a place to write into.

    WHAT IT WILL NOT DO
    ===================
    Only ``run-*`` directories, only when ``iterdir()`` shows them completely
    empty, and never ``reports_root`` itself. A directory still holding a file
    this function does not recognise is left alone rather than force-removed:
    the cost of leaving one behind is an empty folder, and the cost of being
    clever is deleting something somebody still wanted.
    """
    if not reports_root.is_dir():
        return 0

    removed = 0
    for path in reports_root.iterdir():
        if not path.is_dir() or not path.name.startswith("run-"):
            continue
        try:
            if any(path.iterdir()):
                continue  # still holds something; leave it alone
            path.rmdir()
            removed += 1
        except OSError as exc:  # pragma: no cover - permissions, a locked dir
            log.warning("could not remove empty report folder %s: %s", path.name, exc)

    if removed:
        log.info("removed %d empty report folder(s)", removed)
    return removed
