# The data model

17 tables and 9 enums, in `app/models.py`. This document explains what each
group is for and why it is shaped that way; the source has per-column detail.

SQLAlchemy 2.0 typed `Mapped[]` style throughout, migrations by Alembic.

## The five groups

```
  configuration & identity      settings · users · credentials · audit_events
                                    │
  what the supplier said          feed_files · vendor_products
                                  vendor_product_history · rejected_rows
                                    │
  what Amazon says                amazon_listings · catalog_syncs
                                    │
  what we did about it            runs · push_batches · push_items
                                    │                        │
                                    │                   ┌────┘
  the undo trail  ────────────────────────────────────┘
                                    │
  things needing a human            unmapped_barcodes · sku_overrides
  and things produced               notifications · report_files
```

The separation between "what the supplier said", "what Amazon says" and "what
we did" is the heart of it. Those are three independent sources of truth, and
the entire value of the system comes from comparing the second against a
decision derived from the first — never from comparing the first against its
own history. See [ARCHITECTURE.md](ARCHITECTURE.md#why-we-compare-against-amazon-not-against-the-last-feed).

---

## Configuration and identity

### `settings`

Key/JSON, not columns. Adding a setting is three lines in `SPECS` and no
migration; removing one is safe because unknown keys in the table are ignored
and reported.

Reads fall back to the declared default when a row is missing, so a fresh
database behaves correctly before anybody opens the settings page — the table
is a record of *deviations from default*, not a required fixture.

Every write goes through `settings_store.set_value`, which validates against
the declared type and bounds and writes an `audit_events` row. Nothing else
writes this table.

### `users`

One shared administrator, seeded at first start. Password hashing, optional
TOTP two-factor, lockout counters.

The `role` column exists and is checked by `require_role()` even though only
one role is currently used. It costs nothing now, and retrofitting it later
would mean a migration *plus* a gap in the audit trail covering the period
before it existed.

### `credentials`

Encrypted secrets: the supplier FTP password, the Amazon client secret and
refresh token, the SMTP password.

AES-GCM under a master key that lives in the environment and **never in the
database**, so a database dump alone yields nothing usable. Only a hint (the
last four characters) is ever returned to the interface — there is no code path
that returns a stored secret to a browser.

### `audit_events`

Append-only. Who changed what, when, and from what to what. Settings changes,
credential updates, logins, approvals, undos.

Append-only matters: a table that can be edited to hide an action is not an
audit trail.

---

## What the supplier said

### `feed_files`

One row per archive seen, with its SHA-256. The hash is how a file is never
processed twice — the name and timestamp are not trustworthy enough on their
own.

Name, size and server timestamp are recorded too, because the filename carries
the date that decides what "today" means. **That date is in the supplier's
calendar, not ours**, which is the sharpest edge in the system; see
[ADAPTING.md](ADAPTING.md#timezones).

Status covers quarantine, so a malformed archive is recorded as seen-and-
rejected rather than silently retried for ever.

### `vendor_products`

Current state per barcode: stock, price, description, and the bookkeeping that
makes dropped-product detection safe —

- `last_full_feed_id` — the full feed that last confirmed this product exists.
  Null means it has only ever appeared in deltas.
- `missing_from_full_feeds` — consecutive **full** feeds that omitted it. This
  counter is incremented in exactly one place, and only when a full feed was
  actually processed. That single-writer discipline is what makes
  [zeroing safe](SAFETY-MODEL.md#why-a-missing-feed-cannot-zero-a-catalogue).

`price` is stored and **never sent to Amazon**. It exists because the generated
reports are built from it, and because "what did this cost in March" is a
question worth being able to answer.

### `vendor_product_history`

Append-only log of every stock or price change. Normally the largest table in
the database and the one that governs how much disk the system needs — the
first full feed writes one row per product in the supplier's catalogue, and it
grows for as long as the system runs. Retention is a setting, and old rows are
removed in a single SQL statement rather than by loading them.

### `rejected_rows`

Rows that could not be parsed, with the reason. Kept rather than discarded: a
sudden jump in rejections means the supplier changed their format, and that is
a signal worth having. It also makes "why is this product missing?" answerable.

---

## What Amazon says

### `amazon_listings`

The cached picture of the account, from the All Listings Report. SKU, barcode,
quantity, status, fulfilment channel.

**`sku_prefix` is denormalised from the SKU on write.** Scope filtering is then
an indexed equality test rather than a `LIKE` scan over the whole catalogue,
and it happens on every run.

`fulfillment_channel` is what stops FBA listings being touched — when Amazon
holds the stock, Amazon owns the quantity and a seller may not set it.

A caution the code reflects: **the report's columns are not guaranteed.** A
report may arrive with only `seller-sku, asin1, price, quantity, status` — no
product-id and no fulfilment-channel column. So the barcode is recovered from
inside the SKU, and the channel is taken from the listing when it is read back
through the Listings API. Neither is assumed present.

### `catalog_syncs`

One row per catalogue refresh: when, how many listings, success or failure.

This is what makes "is our picture of Amazon too old to trust?" answerable, and
that question is itself a guardrail. Deciding from a stale catalogue is how you
re-send changes that already landed.

---

## What we did about it

### `runs`

One row per pipeline pass, whatever the outcome — **including when nothing
changed**, because "we checked at 14:05 and everything already matched" is
information, and a gap in the run history should mean the system was not
running.

Carries the counts, the trigger, the status, and `guardrail_message`: the full
text of whatever halted the run, in the operator's language rather than the
developer's, because a halt has to explain itself to the person deciding
whether to override it.

### `push_batches`

A set of changes sent together, with its transport (`listings` or `feeds`) and
status. The status is committed as `SENDING` *before* the first call to Amazon,
which is what lets stage 0 recognise and settle a batch stranded by a crash.

### `push_items` — the most important table

One row per quantity changed, each carrying **`previous_quantity`: the value
Amazon had before**.

These rows are the undo trail. They are committed *before* anything is sent —
[the durability barrier](SAFETY-MODEL.md#the-durability-barrier) — so if the
process dies mid-send, the record of what to restore already survived.

`result` tracks the lifecycle: `PENDING` → `ACCEPTED` (Amazon took the request)
→ `VERIFIED` (we read it back and it stuck) or `NOT_APPLIED` (it did not). That
`ACCEPTED`/`VERIFIED` distinction is the whole point of read-back verification:
Amazon reports success for writes it discards, so "accepted" is not a claim
about the account's state.

Retention deliberately never prunes an unconfirmed row.

---

## Needing a human, and produced for one

### `unmapped_barcodes`

Products the supplier has **in stock** that could not be matched to a listing.

The in-stock filter is what makes this list usable. A seller lists a
deliberately chosen subset of a distributor's catalogue, so most of any feed is
unmatched by design; recording all of it would bury the few rows that matter. A
product the supplier stocks and the seller does not list is either a missed
opportunity or a broken mapping. One the supplier does not stock is neither.

### `sku_overrides`

Manual barcode → SKU mappings. The highest-trust source in the matcher,
because a human stated it.

### `notifications`

Alerts, queued and flushed rather than sent inline. Sending email from inside a
pipeline stage means an SMTP timeout can fail a run that otherwise succeeded.

### `report_files`

One row per generated report. The rows survive after the files are pruned, so
the history stays visible with the file marked expired rather than simply
vanishing.

---

## The enums

| Enum | Notable values |
|---|---|
| `SyncMode` | `dry_run` · `needs_approval` · `automatic` |
| `FeedKind` | `full` · `delta` — the distinction that makes zeroing safe |
| `FileStatus` | includes `quarantined` |
| `RunTrigger` | `schedule` · `manual` |
| `RunStatus` | includes `halted_by_guardrail`, `dry_run_complete`, `no_changes` |
| `BatchStatus` | includes `sending` — the stranded state |
| `PushMethod` | `listings` · `feeds` |
| `ItemResult` | `pending` · `accepted` · `verified` · `not_applied` |
| `MapSource` | trust tiers; nothing below `manual_override` is ever sent |

All are `str` enums, so they store readably and a database dump can be read
without a decoder ring.

---

## Two conventions worth knowing

**Everything is stored in UTC.** "Which day is it" is a *presentation* concern,
answered with the configured timezone at the edge, never by storing local
times. `utcnow()` in `models.py` is the only clock.

**`BigIntPk` on the high-volume tables.** `push_items` gains a row for every
SKU in every batch, and `vendor_product_history` one per product change. A busy
installation writes millions a year, so the 2.1-billion ceiling of a 32-bit key
is a real horizon rather than a theoretical one — and widening a primary key
after the fact is an outage.

## Migrations

Alembic, with one initial migration. Applied with `alembic upgrade head`.

Note that `alembic downgrade -1` on the initial migration drops every table,
including the undo trail. It is not a rollback mechanism for a live deployment;
restore from a backup instead.
