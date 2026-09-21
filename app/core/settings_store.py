"""
Operator-editable settings: the definitive list, their defaults, and safe access.

WHY THESE ARE SETTINGS AND NOT CONSTANTS
========================================
Every value here is a dashboard field, editable by the person who operates the
system rather than the person who wrote it.

That split is deliberate. The operator knows their catalogue, their supplier and
their risk tolerance; the developer does not, and is usually not available at the
moment a threshold turns out to be wrong. So a behaviour that might ever need
adjusting lives in :data:`SPECS` with a label and help text written for a
non-technical reader -- not in a constant that needs a release to change.

The boundary is drawn at business behaviour. Infrastructure (database URL,
credentials, file server, master key) stays in the environment: those change
when the machine changes rather than during normal operation, and putting them
on a web page would be a security hole rather than a convenience.

WHY IT MATTERS TO THE NEXT DEVELOPER
====================================
Read :data:`DEFAULTS` and you know everything the system can be told to do.
There is no second place where behaviour is configured.

Adding a setting is three lines in :data:`DEFAULTS` and nothing else -- no
migration, because the table is key/JSON. Deleting one is safe too: unknown
keys in the database are ignored and reported.

RULES
-----
* Reads go through :func:`get` / :func:`get_all`, which fall back to the
  default if a row is missing. A fresh database therefore behaves correctly
  before anybody visits the settings page.
* Writes go through :func:`set_value`, which validates against the declared
  type and bounds and writes an audit event. Nothing else writes the table.
* ``locked=True`` settings can never be changed through the web interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Setting

log = logging.getLogger(__name__)


class SettingError(ValueError):
    """A setting value was rejected. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class Spec:
    """Declaration of one setting: its default, type, bounds and wording."""

    key: str
    default: Any
    value_type: str  # int | float | bool | str | list[str] | time | enum
    label: str
    help_text: str
    category: str
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = None
    locked: bool = False
    sort_order: int = 100
    #: Marks a setting whose value directly determines what is written to the
    #: seller account. The dashboard shows these with a warning style and
    #: requires a confirmation step -- these are the values where a careless
    #: edit is expensive rather than merely wrong.
    high_impact: bool = False


# ===========================================================================
# THE COMPLETE LIST OF EDITABLE SETTINGS
# ===========================================================================
# HOW THE DEFAULTS BELOW WERE CHOSEN
#
# Every default here sits at the cautious end of its range, not the useful end.
# The system ships refusing to write anything (sync_mode = dry_run) and with
# guardrail thresholds tight enough that an unexpected feed halts the run
# rather than acting on it.
#
# That bias is deliberate, because the two failure modes are not symmetric. A
# default that is too conservative announces itself immediately: a run halts, a
# guardrail trips, somebody widens a threshold. A default that is too permissive
# announces itself only after it has written something wrong to a live account.
# Where a choice existed, it was resolved towards "stop and ask".
#
# The consequence is that a new installation WILL halt on its first full feed
# until an operator has looked at the proposals and widened the thresholds to
# match their own catalogue. That is the intended experience, not a rough edge.
#
# Help-text convention: say what the setting does, then give the trade-off in
# BOTH directions. The reader is deciding a value, not learning an API.

SPECS: list[Spec] = [
    # ------------------------------------------------------------------ MODE
    Spec(
        key="sync_mode",
        default="dry_run",
        value_type="enum",
        choices=["dry_run", "needs_approval", "automatic"],
        label="What the system is allowed to do",
        help_text=(
            "Practice mode works everything out and shows exactly what it would send, "
            "but sends nothing. Ask first prepares the changes and waits for a person "
            "to click Approve. Automatic sends on its own, with the safety rules "
            "watching. Start in practice mode and stay there until you trust it."
        ),
        category="safety",
        sort_order=1,
        high_impact=True,
    ),
    Spec(
        key="paused",
        default=False,
        value_type="bool",
        label="Pause everything",
        help_text=(
            "Stops all activity immediately. Checked before every run, so a paused "
            "system stays paused even if a cycle was already waiting in the queue. "
            "Nothing is lost while paused - the work is simply picked up when you "
            "switch it back on."
        ),
        category="safety",
        sort_order=2,
        high_impact=True,
    ),

    # -------------------------------------------------------------- SCHEDULE
    Spec(
        key="sync_interval_minutes",
        default=60,
        value_type="int",
        min_value=5,
        max_value=1440,
        label="Check the vendor every … minutes",
        help_text=(
            "The vendor publishes a small delta file about every 5 minutes. Start at "
            "60 minutes; once you trust the system, lower this to 15 or "
            "even 5. Amazon's limits are not the constraint - your confidence is."
        ),
        category="schedule",
        sort_order=10,
    ),
    Spec(
        key="sync_offset_minutes",
        default=0,
        value_type="int",
        min_value=0,
        max_value=59,
        label="Run the check at \u2026 minutes past the hour",
        help_text=(
            "Fixes the clock the checks run on. With the interval above set to 60 and "
            "this set to 10, a check happens at 10 past every hour - on the hour it "
            "was set to, not on whatever minute the server was last restarted. "
            "Restarting no longer shifts the timetable or triggers an extra run. "
            "With a shorter interval the same offset repeats: 15 minutes and an "
            "offset of 10 gives 10, 25, 40 and 55 past."
        ),
        category="schedule",
        sort_order=11,
    ),
    Spec(
        key="timezone",
        default="UTC",
        value_type="timezone",
        label="Which timezone decides what 'today' means",
        help_text=(
            "Used to work out which feed files belong to today. The vendor puts the "
            "date in the filename (for example FULL_FEED_100000_20260904.zip), so "
            "this only decides where the day boundary falls. SET IT TO THE VENDOR'S "
            "OWN TIMEZONE, not yours -- that date is the vendor's calendar date, so "
            "any other zone makes the newest feed look like it belongs to a "
            "different day, and it gets skipped. Must be a full IANA name with the "
            "region in front: America/New_York, Europe/London, Asia/Tokyo, "
            "UTC. A name that does not exist is refused when you save, so it can "
            "never quietly fall back to a different day."
        ),
        category="schedule",
        sort_order=11,
    ),
    Spec(
        key="amazon_roles_checked",
        default=False,
        value_type="bool",
        label="Amazon app permissions have been checked",
        help_text=(
            "Tick this once you have confirmed in Seller Central that the app has "
            "'Product Listing' ticked and 'Pricing' unticked, and have saved the new "
            "refresh token. It only silences the reminder on the Status page. "
            "Nothing else changes, and nothing here can grant a permission - if the "
            "role is really missing, the first send still fails with a 403."
        ),
        category="safety",
        sort_order=4,
    ),
    Spec(
        key="catalog_refresh_hour",
        default=3,
        value_type="int",
        min_value=0,
        max_value=23,
        label="Refresh the Amazon catalogue at … o'clock",
        help_text=(
            "Only used when the refresh below is set to once a day. The system "
            "downloads Amazon's All Listings Report to learn the real SKUs and the "
            "quantities Amazon is currently showing. A quiet hour is best. Uses the "
            "timezone above."
        ),
        category="schedule",
        sort_order=12,
    ),
    Spec(
        key="catalog_refresh_hourly",
        default=True,
        value_type="bool",
        label="Refresh the Amazon catalogue every hour",
        help_text=(
            "On, the catalogue is refreshed every hour at the minute below, so each "
            "check decides against a picture of Amazon that is minutes old rather "
            "than up to a day old. It also confirms changes that were sent but not "
            "yet read back, which is what moves them to 'Confirmed on Amazon'. Off, "
            "it runs once a day at the hour above."
        ),
        category="schedule",
        sort_order=13,
        high_impact=True,
    ),
    Spec(
        key="catalog_refresh_offset_minutes",
        default=5,
        value_type="int",
        min_value=0,
        max_value=59,
        label="… at this many minutes past the hour",
        help_text=(
            "Which minute of the hour the hourly catalogue refresh starts. Keep a "
            "gap between this and 'Run the check at … minutes past the hour' so the "
            "two do not collide: a full catalogue refresh can take several minutes, "
            "and a check that starts while one is running will wait for it. They "
            "are separate settings on purpose, so you can move either one without "
            "touching the code."
        ),
        category="schedule",
        sort_order=14,
    ),
    Spec(
        key="max_file_age_hours",
        default=24,
        value_type="int",
        min_value=1,
        max_value=336,
        label="Ignore vendor files older than … hours",
        help_text=(
            "Old delta files sometimes sit on the vendor's server. Anything older "
            "than this is skipped so yesterday's numbers are never replayed over "
            "today's."
        ),
        category="schedule",
        sort_order=13,
    ),
    Spec(
        key="process_only_today",
        default=True,
        value_type="bool",
        label="Only process files dated today",
        help_text=(
            "On by default. The date comes from the filename, so this "
            "is exact. Turning it off makes the system process any unseen file that "
            "is newer than the last one it handled - useful for catching up after "
            "an outage, and safe because a file is never processed twice."
        ),
        category="schedule",
        sort_order=14,
    ),

    # ----------------------------------------------------------------- SCOPE
    Spec(
        key="sku_prefixes_in_scope",
        default=[],
        value_type="list[str]",
        label="SKU prefixes this system may change",
        help_text=(
            "The system will only ever touch listings whose SKU starts with one of "
            "these. EMPTY MEANS NOTHING IS IN SCOPE -- the system touches no "
            "listing at all until you name at least one prefix. That is the "
            "deliberate default: a fresh installation cannot write anything "
            "anywhere until somebody has stated what it owns. If your account buys "
            "from more than one supplier, the "
            "SKU prefix is usually what tells them apart, and naming only the "
            "prefixes this feed actually backs is the single most effective way to "
            "limit the blast radius of a mistake. Use the Coverage report to check: "
            "a prefix where nearly every listing matches a barcode in the feed is "
            "safe to include; one that matches only part way is not, because the "
            "unmatched remainder would eventually be set to zero."
        ),
        category="scope",
        sort_order=20,
        high_impact=True,
    ),
    Spec(
        key="skip_inactive_listings",
        default=True,
        value_type="bool",
        label="Skip listings that are not Active",
        help_text=(
            "Accounts accumulate Inactive and Incomplete listings. Writing a "
            "quantity to those achieves nothing and clutters the history, so they "
            "are skipped by default."
        ),
        category="scope",
        sort_order=21,
    ),
    Spec(
        key="skip_fba_listings",
        default=True,
        value_type="bool",
        label="Never touch products stored at Amazon (FBA)",
        help_text=(
            "When Amazon holds the stock (FBA), Amazon owns the quantity and a "
            "seller may not set it. On a purely merchant-fulfilled account this "
            "never triggers -- which is exactly why it is locked on. A guard that "
            "costs nothing while it is unnecessary is the cheapest kind to keep."
        ),
        category="scope",
        sort_order=22,
        locked=True,
    ),
    Spec(
        key="blacklisted_skus",
        default=[],
        value_type="list[str]",
        label="Never-touch list (exact SKUs)",
        help_text=(
            "Products managed by hand, or bought from somewhere else. The system "
            "will never change these, whatever the vendor says."
        ),
        category="scope",
        sort_order=23,
    ),

    # ------------------------------------------------------- QUANTITY RULES
    Spec(
        key="safety_buffer",
        default=0,
        value_type="int",
        min_value=0,
        max_value=50,
        label="Hold back … units as a safety margin",
        help_text=(
            "Subtracted from the supplier's stock before publishing. The "
            "trade-off: 0 keeps Amazon exactly in step and sells every unit, but "
            "leaves no margin if the supplier's number is stale or a unit is sold "
            "elsewhere between refreshes. 1 or 2 costs a little availability and is "
            "an effective protection against cancelled orders. Raise it if you see "
            "cancellations; lower it if you refresh often."
        ),
        category="quantity",
        sort_order=30,
        high_impact=True,
    ),
    Spec(
        key="max_quantity",
        default=999,
        value_type="int",
        min_value=1,
        max_value=999,
        label="Never publish more than … units",
        help_text=(
            "A hard ceiling, applied after every other rule. Suppliers sometimes "
            "report very large numbers -- a warehouse total rather than what they "
            "will actually ship you -- and promising thousands of units on Amazon "
            "is a risk with no upside. Lower it (10-20) if you would rather sell "
            "out than oversell; the default is high enough not to interfere until "
            "you have decided what your own ceiling should be."
        ),
        category="quantity",
        sort_order=31,
        high_impact=True,
    ),
    Spec(
        key="out_of_stock_at",
        default=0,
        value_type="int",
        min_value=0,
        max_value=20,
        label="Treat as out of stock when vendor stock is … or less",
        help_text=(
            "At 0 a product stays on sale until the supplier genuinely has none, "
            "which sells the most units. Raising it to 1 or 2 takes products off "
            "sale while a little stock remains, trading a small amount of "
            "availability for a meaningful drop in cancelled orders."
        ),
        category="quantity",
        sort_order=32,
        high_impact=True,
    ),
    Spec(
        key="min_change_to_push",
        default=0,
        value_type="int",
        min_value=0,
        max_value=50,
        label="Smallest change worth sending to Amazon",
        help_text=(
            "At 0, Amazon stays exactly in step with the supplier. Raising it to 2 "
            "suppresses cosmetic updates -- moving a product from 12 to 11 changes "
            "nothing a buyer will notice -- which reduces write volume if you sync "
            "frequently. It costs accuracy at the low end, where the difference "
            "between 2 and 1 does matter."
        ),
        category="quantity",
        sort_order=33,
    ),
    Spec(
        key="missing_full_feeds_before_zero",
        default=2,
        value_type="int",
        min_value=1,
        max_value=10,
        label="Set to 0 after a product is missing from … full feeds",
        help_text=(
            "When a supplier stops carrying a product it simply disappears from "
            "the full feed -- there is no 'discontinued' signal to read. The "
            "trade-off is how far you trust a single absence. 1 reacts immediately "
            "but treats one malformed or partial feed as a discontinuation; 2 waits "
            "for confirmation across two feeds, at the cost of a day's delay. The "
            "default is 2 because a wrong zero takes a product off sale. Only a "
            "FULL feed ever counts here -- absence from a delta feed means "
            "'unchanged', never 'gone'. The listing is never deleted from the "
            "system or from Amazon; only its quantity goes to 0, so it can come "
            "back instantly."
        ),
        category="quantity",
        sort_order=34,
        high_impact=True,
    ),
    Spec(
        key="allow_quantity_increases",
        default=True,
        value_type="bool",
        label="Allow the system to raise quantities",
        help_text=(
            "Turning this off makes the system only ever reduce a quantity or set it "
            "to 0. That protects account health with no chance of overselling, at the "
            "cost of leaving sales on the table. A useful setting for a nervous first "
            "week."
        ),
        category="quantity",
        sort_order=35,
        high_impact=True,
    ),
    Spec(
        key="format_overrides",
        default={},
        value_type="json",
        label="Different rules per product type",
        help_text=(
            "Optional. Lets you set a different maximum or safety margin for a "
            "particular product type. The vendor uses BULK, STD, CASE, PACK, SINGLE, KIT, SET "
            'and others. Example: {"BULK": {"max_quantity": 8}}.'
        ),
        category="quantity",
        sort_order=36,
    ),

    # ------------------------------------------------------------ GUARDRAILS
    Spec(
        key="max_changes_per_run",
        default=5000,
        value_type="int",
        min_value=1,
        max_value=200000,
        label="Change at most … products in one run",
        help_text=(
            "A brake, not a limit on the work. Anything above this waits for the "
            "next run, worst cases first. It matters most on the first full-feed "
            "run, when the entire gap between supplier and Amazon has to be closed "
            "at once. Spreading that over several runs means no single run ever "
            "looks like a runaway, and leaves a human the chance to notice if one "
            "is. As an illustration, a 40,000-product backlog at 2,000 per run "
            "clears in about twenty runs; raise it once you have watched a few."
        ),
        category="guardrails",
        sort_order=40,
        high_impact=True,
    ),
    Spec(
        key="guardrail_max_percent_changed",
        default=25.0,
        value_type="float",
        min_value=0.1,
        max_value=100.0,
        label="Stop if more than …% of the catalogue would change",
        help_text=(
            "Protects against a corrupt or wrong-vendor file. If a single run wants "
            "to change more than this share of the in-scope catalogue, it stops and "
            "emails you instead of proceeding."
        ),
        category="guardrails",
        sort_order=41,
    ),
    Spec(
        key="guardrail_max_zeroing",
        default=2000,
        value_type="int",
        min_value=1,
        max_value=100000,
        label="Stop if more than … products would go to zero",
        help_text=(
            "The most damaging thing this system could ever do is switch off the "
            "whole catalogue at once. This is the rule that prevents it. A normal day "
            "zeroes a few dozen to a few hundred products."
        ),
        category="guardrails",
        sort_order=42,
        high_impact=True,
    ),
    Spec(
        key="guardrail_min_feed_rows_percent",
        default=50.0,
        value_type="float",
        min_value=1.0,
        max_value=100.0,
        label="Reject a full feed with fewer than …% of its usual rows",
        help_text=(
            "A truncated download looks exactly like 'everything went out of "
            "stock', which is the most expensive thing a feed can be wrong "
            "about. Each full feed is compared against the MEDIAN size of "
            "previous ones -- a median rather than the last one, so a single "
            "odd feed cannot drag the baseline with it. Anything under this "
            "share of that median is quarantined and reported instead of "
            "processed."
        ),
        category="guardrails",
        sort_order=43,
    ),
    Spec(
        key="guardrail_require_known_header",
        default=True,
        value_type="bool",
        label="Reject a feed whose column names have changed",
        help_text=(
            "The expected header is barcode|brand|title|price|stock|format. If the "
            "vendor changes it, stock could end up read from the price column. Better "
            "to stop and tell you."
        ),
        category="guardrails",
        sort_order=44,
    ),
    Spec(
        key="verify_after_push",
        default=True,
        value_type="bool",
        label="Read Amazon back to confirm each change landed",
        help_text=(
            "Amazon can accept a batch and still quietly reject individual rows. With "
            "this on, the system re-reads what it changed and confirms. Anything that "
            "did not stick is retried and shown on the dashboard. Costs a little time "
            "and is worth it."
        ),
        category="guardrails",
        sort_order=45,
    ),

    # --------------------------------------------------------------- PARSING
    Spec(
        key="feed_delimiter",
        default="|",
        value_type="str",
        label="Character that separates the columns",
        help_text=(
            "The vendor's files are named .csv but are actually separated by the | "
            "character. Change this only if the vendor changes their format."
        ),
        category="parsing",
        sort_order=50,
    ),
    Spec(
        key="column_map",
        default={
            "barcode": "barcode",
            "brand": "brand",
            "title": "title",
            "price": "price",
            "stock": "stock",
            "format": "format",
        },
        value_type="json",
        label="Which column is which",
        help_text=(
            "Left side is what the system needs, right side is what the vendor calls "
            "it. If the vendor renames 'stock' to 'qty', change it here - it takes "
            "two minutes and needs no developer."
        ),
        category="parsing",
        sort_order=51,
    ),
    Spec(
        key="sku_prefix_for_new",
        default="EXAMPLE-",
        value_type="str",
        label="Prefix used to build a SKU from a barcode",
        help_text=(
            "The system builds the Amazon SKU as this prefix followed by the "
            "barcode padded to 13 digits. Padding is not optional: feeds strip "
            "leading zeros and Amazon SKUs keep them, so without padding a large "
            "part of the catalogue is addressed as SKUs that do not exist -- which "
            "Amazon accepts and silently discards."
        ),
        category="parsing",
        sort_order=52,
    ),

    # ---------------------------------------------------------------- REPORTS
    Spec(
        key="generate_reports_for_delta",
        default=True,
        value_type="bool",
        label="Produce the five report files for delta runs too",
        help_text=(
            "Generates the five files separately for each incremental update as "
            "well as for the daily full feed, so it is possible to see exactly "
            "what each one changed. Off, only full-feed runs produce reports, "
            "which is quieter and writes far fewer files."
        ),
        category="reports",
        sort_order=60,
    ),
    Spec(
        key="report_retention_days",
        default=90,
        value_type="int",
        min_value=1,
        max_value=3650,
        label="Keep report files for … days",
        help_text="Older report files are deleted to stop the disk filling up. The database records stay.",
        category="reports",
        sort_order=61,
    ),
    Spec(
        key="keep_feed_files_days",
        default=3,
        value_type="int",
        min_value=0,
        max_value=365,
        label="Keep downloaded vendor files for … days",
        help_text=(
            "The daily full feed is about 75 MB and a copy is kept after it is read, in "
            "case somebody needs to look at the original. Once the rows are in the "
            "database the file itself is not needed, so old ones are deleted. Files that "
            "were rejected are always kept, however old, because those are the ones "
            "somebody needs to inspect. 0 deletes as soon as a file has been read."
        ),
        category="reports",
        sort_order=62,
    ),
    Spec(
        key="keep_catalog_snapshots_days",
        default=30,
        value_type="int",
        min_value=1,
        max_value=3650,
        label="Keep Amazon catalogue snapshots for … days",
        help_text=(
            "Each catalogue refresh saves Amazon's own listing report. These are what "
            "'restore the account to how it looked on a past day' reads, so they are "
            "worth keeping longer than the other files. About 5 MB each."
        ),
        category="reports",
        sort_order=63,
    ),
    Spec(
        key="keep_vendor_history_days",
        default=180,
        value_type="int",
        min_value=7,
        max_value=3650,
        label="Keep vendor change history for … days",
        help_text=(
            "Every time the supplier changes a product's stock or price, a row is "
            "written, so 'what did this cost in March' can still be answered. The "
            "first full feed writes one row per product, and the table grows for as "
            "long as the system runs, so old rows are eventually removed. This is "
            "normally the largest table in the database and the one that governs "
            "how much disk the system needs -- shorten it if space is tight."
        ),
        category="reports",
        sort_order=65,
    ),
    Spec(
        key="keep_push_items_days",
        default=90,
        value_type="int",
        min_value=14,
        max_value=3650,
        label="Keep the per-product change log for … days",
        help_text=(
            "One row is written for every quantity this system changes, holding what "
            "Amazon had before it. That row is what Undo reads, so these are the most "
            "valuable rows in the database - and among the fastest growing, since "
            "a busy installation writes tens of thousands a day for as long as it "
            "runs. "
            "Old rows are removed once nothing can still need them. The batch itself "
            "stays in the history with its totals; only the per-product detail goes, "
            "and Undo on a batch that old refuses clearly rather than doing nothing. "
            "Rows are never removed while anything in their batch is still waiting to "
            "be confirmed, whatever this is set to. Three months is far beyond the "
            "point at which restoring a quantity would help rather than harm - it "
            "would put a three-month-old stock number over today's correct one."
        ),
        category="reports",
        sort_order=66,
    ),
    Spec(
        key="min_free_disk_gb",
        default=2.0,
        value_type="float",
        min_value=0.0,
        max_value=1000.0,
        label="Warn when free disk space falls below … GB",
        help_text=(
            "A full disk stops the sync, and it stops it quietly: the download fails, "
            "nothing can be written, and Amazon keeps showing whatever it last showed. "
            "This warns while there is still time to act. 0 switches the check off."
        ),
        category="reports",
        sort_order=64,
    ),

    # ----------------------------------------------------------------- ALERTS
    Spec(
        key="alert_emails_critical",
        default=[],
        value_type="list[str]",
        label="Email these people about serious problems",
        help_text=(
            "A stopped run, a failed login to the vendor, an expired Amazon token, a "
            "safety rule that fired. These need someone to act."
        ),
        category="alerts",
        sort_order=70,
    ),
    Spec(
        key="alert_emails_summary",
        default=[],
        value_type="list[str]",
        label="Email these people the daily summary",
        help_text="One message a day: what ran, what changed, what needs looking at.",
        category="alerts",
        sort_order=71,
    ),
    Spec(
        key="alert_emails_approval",
        default=[],
        value_type="list[str]",
        label="Email these people when a batch needs approval",
        help_text="Only used when the mode is set to Ask first.",
        category="alerts",
        sort_order=72,
    ),
    Spec(
        key="alert_on_every_run",
        default=False,
        value_type="bool",
        label="Email after every single run",
        help_text=(
            "Off by default. With a 15-minute cycle this would be 96 emails a day and "
            "people stop reading them, which is worse than no alerts at all."
        ),
        category="alerts",
        sort_order=73,
    ),
    Spec(
        key="unmapped_spike_threshold",
        default=90000,
        value_type="int",
        min_value=1,
        max_value=2000000,
        label="Warn if more than … in-stock products cannot be matched",
        help_text=(
            "Counts only products the supplier HAS in stock but which are not "
            "listed on Amazon. A large steady number here is normal and healthy: a "
            "seller lists a deliberately chosen subset of a distributor's "
            "catalogue, so most of the feed is expected to be unmatched. What "
            "matters is a sudden JUMP, which usually means the supplier changed "
            "their barcode format or SKUs were renamed in Seller Central. Set this "
            "comfortably above your normal figure -- read it off the Coverage "
            "report after a few runs -- so the alert fires on change, not on the "
            "baseline."
        ),
        category="alerts",
        sort_order=74,
    ),

    # ------------------------------------------------------------ INVARIANTS
    Spec(
        key="never_send_price",
        default=True,
        value_type="bool",
        label="Never send a price to Amazon",
        help_text=(
            "This cannot be switched off. Prices are yours to set, because you add "
            "shipping, tax and margin. The rule is built into the code with a final "
            "check that refuses to transmit any message containing a price, and this "
            "row exists only so that nobody can create a setting that pretends to "
            "turn it off."
        ),
        category="safety",
        sort_order=3,
        locked=True,
    ),
]

#: Fast lookup by key.
SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}

#: Plain defaults, used when the database has no row yet.
DEFAULTS: dict[str, Any] = {s.key: s.default for s in SPECS}


# ===========================================================================
# Validation
# ===========================================================================

def _valid_timezone(spec: Spec, name: str) -> str:
    """
    Accept only a timezone that really exists, and say so plainly if it does not.

    WHY THIS IS NOT JUST A STRING
    =============================
    This one setting decides which feed files count as "today", and every
    reader of it -- the pipeline, the scheduler, the dashboard -- falls back to
    a default when the name will not load. That fallback is correct in itself:
    a mistyped zone must not crash the run. What it must not do is happen
    silently, and it was.

    The failure it produces is the worst kind. A single mistyped letter is
    accepted, the fallback moves the day boundary by three hours, the newest
    full feed is judged to belong to yesterday, and it is skipped -- with no
    error anywhere, because nothing has actually gone wrong as far as the code
    is concerned. The catalogue then quietly goes stale. That exact symptom,
    arrived at by a different route, is genuinely expensive to diagnose: every
    component reports success, and the only visible symptom is data that is
    quietly a day out of date.

    Rejecting the value at the only point where it can enter is the whole fix:
    an impossible zone can never reach the database, so the fallback can only
    ever fire for a value that was valid when it was saved and later removed
    from the system's timezone database -- which is vanishingly rare, and no
    longer something a typo can cause.
    """
    if not name:
        raise SettingError(f"{spec.label}: cannot be empty")
    try:
        ZoneInfo(name)
    except Exception as exc:
        raise SettingError(
            f"{spec.label}: {name!r} is not a timezone this system knows. "
            f"Use a full name with the region in front, such as "
            f"America/New_York, Europe/London, Asia/Tokyo or UTC. "
            f"Capitals and the underscore matter ({exc})."
        ) from exc
    return name


def _coerce(spec: Spec, raw: Any) -> Any:
    """
    Convert and validate ``raw`` against ``spec``. Raises :class:`SettingError`.

    Error messages are written for the person using the dashboard, not for a
    developer reading a stack trace.
    """
    t = spec.value_type

    try:
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in {"true", "1", "yes", "on"}:
                return True
            if s in {"false", "0", "no", "off", ""}:
                return False
            raise SettingError(f"{spec.label}: expected yes or no, got {raw!r}")

        # Deliberately Any: this function's whole job is to turn one untyped
        # form field into whichever of nine types the spec declares.
        value: Any
        if t == "int":
            value = int(str(raw).strip())
        elif t == "float":
            value = float(str(raw).strip())
        elif t == "str":
            value = str(raw).strip()
        elif t == "timezone":
            value = _valid_timezone(spec, str(raw).strip())
        elif t == "enum":
            value = str(raw).strip()
            if spec.choices and value not in spec.choices:
                raise SettingError(
                    f"{spec.label}: must be one of {', '.join(spec.choices)}, got {value!r}"
                )
        elif t == "list[str]":
            if isinstance(raw, str):
                # accept newline or comma separated input from a textarea
                value = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
            elif isinstance(raw, (list, tuple)):
                value = [str(p).strip() for p in raw if str(p).strip()]
            else:
                raise SettingError(f"{spec.label}: expected a list, got {type(raw).__name__}")
        elif t == "json":
            if isinstance(raw, str):
                import json

                value = json.loads(raw) if raw.strip() else {}
            else:
                value = raw
            if not isinstance(value, (dict, list)):
                raise SettingError(f"{spec.label}: expected a JSON object or list")
        elif t == "time":
            if isinstance(raw, dtime):
                value = raw.strftime("%H:%M")
            else:
                s = str(raw).strip()
                hh, _, mm = s.partition(":")
                dtime(int(hh), int(mm or 0))  # raises if out of range
                value = f"{int(hh):02d}:{int(mm or 0):02d}"
        else:  # pragma: no cover - guarded by the spec list itself
            raise SettingError(f"unknown value_type {t!r} for {spec.key}")
    except SettingError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{spec.label}: {raw!r} is not a valid {t} ({exc})") from exc

    if spec.min_value is not None and isinstance(value, (int, float)) and value < spec.min_value:
        raise SettingError(f"{spec.label}: must be at least {spec.min_value:g}, got {value:g}")
    if spec.max_value is not None and isinstance(value, (int, float)) and value > spec.max_value:
        raise SettingError(f"{spec.label}: must be at most {spec.max_value:g}, got {value:g}")

    return value


# ===========================================================================
# Read
# ===========================================================================

def get(session: Session, key: str, *, default: Any = None) -> Any:
    """
    One setting, falling back to the declared default when no row exists.

    A missing row is normal, not an error: a fresh database has no settings and
    must still behave correctly.
    """
    spec = SPEC_BY_KEY.get(key)
    row = session.get(Setting, key)
    if row is not None:
        return row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
    if spec is not None:
        return spec.default
    return default


def get_all(session: Session) -> dict[str, Any]:
    """
    Every setting, defaults merged with stored overrides.

    Call this ONCE at the start of a run and pass the dict around. Re-reading
    mid-run would let a settings change alter behaviour halfway through, which
    makes a run impossible to explain afterwards. The result is also stored on
    ``Run.settings_snapshot`` for exactly that reason.
    """
    values = dict(DEFAULTS)
    for row in session.execute(select(Setting)).scalars():
        if row.key not in SPEC_BY_KEY:
            log.warning("settings table has unknown key %r; ignoring it", row.key)
            continue
        values[row.key] = (
            row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
        )
    return values


def get_typed(session: Session) -> EffectiveSettings:
    """Settings as an attribute-access object, for readable engine code."""
    return EffectiveSettings(get_all(session))


@dataclass(slots=True)
class EffectiveSettings:
    """
    Thin typed view over the settings dict.

    Exists so the decision engine reads ``s.max_quantity`` rather than
    ``settings["max_quantity"]`` -- fewer chances to typo a key into a silent
    ``None``.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        if name in self.raw:
            return self.raw[name]
        if name in DEFAULTS:
            return DEFAULTS[name]
        raise AttributeError(f"no setting named {name!r}")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.raw)


# ===========================================================================
# Write
# ===========================================================================

def set_value(
    session: Session,
    key: str,
    raw_value: Any,
    *,
    actor: str = "system",
    actor_ip: str | None = None,
    allow_locked: bool = False,
) -> Any:
    """
    Validate and store one setting, writing an audit event.

    ``allow_locked`` exists only for the seeding script. A web request must
    never pass it, which is why the router does not expose it.

    Returns the coerced value that was stored.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"There is no setting called {key!r}.")
    if spec.locked and not allow_locked:
        raise SettingError(
            f"{spec.label} cannot be changed. It is a built-in safety rule, not an option."
        )

    value = _coerce(spec, raw_value)

    row = session.get(Setting, key)
    old = None
    if row is None:
        row = Setting(
            key=key,
            value={"v": value},
            value_type=spec.value_type,
            label=spec.label,
            help_text=spec.help_text,
            category=spec.category,
            min_value=spec.min_value,
            max_value=spec.max_value,
            choices=spec.choices,
            locked=spec.locked,
            sort_order=spec.sort_order,
            updated_by=actor,
        )
        session.add(row)
    else:
        old = row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
        row.value = {"v": value}
        row.updated_by = actor
        # keep the descriptive columns in step with the code
        row.label = spec.label
        row.help_text = spec.help_text
        row.category = spec.category
        row.locked = spec.locked

    session.add(
        AuditEvent(
            action="setting.changed",
            actor=actor,
            actor_ip=actor_ip,
            target=key,
            old_value={"v": old},
            new_value={"v": value},
            detail=spec.label,
        )
    )
    log.info("setting %s changed by %s: %r -> %r", key, actor, old, value)
    return value


def seed_defaults(session: Session, *, actor: str = "system") -> int:
    """
    Insert any missing settings rows. Idempotent; never overwrites.

    Run at startup so the dashboard shows the full list with sensible values on
    a brand new deployment.
    """
    existing = {k for (k,) in session.execute(select(Setting.key)).all()}
    created = 0
    for spec in SPECS:
        if spec.key in existing:
            continue
        session.add(
            Setting(
                key=spec.key,
                value={"v": spec.default},
                value_type=spec.value_type,
                label=spec.label,
                help_text=spec.help_text,
                category=spec.category,
                min_value=spec.min_value,
                max_value=spec.max_value,
                choices=spec.choices,
                locked=spec.locked,
                sort_order=spec.sort_order,
                updated_by=actor,
            )
        )
        created += 1
    if created:
        log.info("seeded %d default settings", created)
    return created
