"""
Shared test fixtures.

The whole suite runs against an in-memory SQLite database and never touches the
network. That is a deliberate constraint: a test that can reach Amazon is a
test that can change a live listing, and no amount of care makes that safe to
have in a CI pipeline.

Anything that genuinely needs a live service is marked ``@pytest.mark.integration``
and excluded by default.
"""

from __future__ import annotations

import base64
import os
import secrets

# Environment must be set BEFORE app.config is imported anywhere, because the
# settings object is built at import time and cached.
os.environ.setdefault("MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(32))
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models import Base  # noqa: E402


@pytest.fixture
def engine():
    """
    A fresh in-memory database per test.

    ``StaticPool`` keeps one connection alive for the whole fixture, which is
    required for ``:memory:`` -- otherwise each checkout gets its own empty
    database and nothing persists between statements.
    """
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    """A committed-per-test session bound to the in-memory database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# ---------------------------------------------------------------------------
# Representative fixture data
# ---------------------------------------------------------------------------
# The VALUES here are synthetic. The SHAPES are not, and that distinction is
# the point of this file.
#
# Fixtures invented from scratch tend to be tidy: every barcode the same
# length, every code checksum-valid, no leading zeros, no junk rows. Tidy
# fixtures test the happy path and nothing else, and the bugs that matter in
# this system are all in the untidy cases -- stripped leading zeros, mixed
# barcode widths, unreadable rows, SKUs belonging to another supplier.
#
# So these rows reproduce the awkward properties of a real supplier feed while
# containing no real product, barcode or account data. The barcodes are
# synthetic but carry correct GTIN check digits, because the code uses the
# checksum to tell a stripped zero from rubbish -- fixtures with invalid check
# digits would make that logic untestable.

#: A representative feed header, full and delta alike.
FEED_HEADER = "barcode|brand|title|price|stock|format"

#: Feed rows. Note the stripped leading zeros on the shorter barcodes: that is
#: the property this system exists to reconcile, reproduced deliberately.
FEED_ROWS = [
    "1115962442528|NORTHWIND|WIDGET STANDARD|12.12|0|STD",
    "1153172181431|NORTHWIND|WIDGET DELUXE|9.00|0|STD",
    "25543055416|ACME|GIZMO (LARGE)|17.73|131|BULK",
    "5319056434|CONTOSO|GADGET ASSORTED|9.84|2|STD",
    "2383326746|GLOBEX|TRINKET & CO ASSORTED|3.99|2|PACK",
]

#: Catalogue SKUs as Amazon reports them. The barcode part is always padded to
#: 13 digits -- the fact the whole project hinges on.
IN_SCOPE_SKUS = [
    "EXAMPLE-0007298811035",
    "EXAMPLE-0001749188257",
    "EXAMPLE-0051449820131",
    "EXAMPLE-4705343071514",   # already 13 digits, no padding needed
    "EXAMPLE-0932826789213",
]

#: SKUs from other suppliers on the same account. The system must never touch
#: these -- their quantities come from feeds it never sees.
OUT_OF_SCOPE_SKUS = [
    "ALT-7263956611093",
    "SUP5-1234567",
    "LEGACY1-060304883303",
    "LEGACY3-075562188589",
    "SUP4-1234567890123-V.G",
]

#: Genuinely damaged SKUs found live on the account. Real data-entry accidents,
#: kept as fixtures so the parser is never allowed to choke on them.
REAL_MALFORMED_SKUS = [
    ": ALT-7263956611093",   # leading colon and space from a paste
    "XAMPLE-080562967123",        # missing the leading H
    "18-HM-7756615",
]


@pytest.fixture
def feed_header() -> str:
    return FEED_HEADER


@pytest.fixture
def feed_rows() -> list[str]:
    return list(FEED_ROWS)


@pytest.fixture
def feed_text(feed_header, feed_rows) -> str:
    """A complete miniature feed file, exactly as the vendor formats it."""
    return "\n".join([feed_header, *feed_rows]) + "\n"


@pytest.fixture
def ams_skus() -> list[str]:
    return list(IN_SCOPE_SKUS)


@pytest.fixture
def out_of_scope_skus() -> list[str]:
    return list(OUT_OF_SCOPE_SKUS)
