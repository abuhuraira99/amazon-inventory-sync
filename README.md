# Amazon Inventory Sync

[![CI](https://github.com/abuhuraira99/amazon-inventory-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/abuhuraira99/amazon-inventory-sync/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-377%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.14-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Type checked](https://img.shields.io/badge/mypy-strict-blue)](pyproject.toml)

Keeps an Amazon seller account's stock quantities in step with a supplier's
stock feed. It reads the feed over FTPS, reconciles it against what Amazon
actually shows, and writes back the quantities that are wrong.

**Quantities only. It cannot send a price** — not as a setting that defaults to
off, but structurally: the decision type has no price field, and a guard walks
every outgoing payload and refuses to transmit if it finds one.

---

## The problem

Selling stock you do not have is expensive. Amazon charges you for the
cancellation, the buyer leaves a review about it, and enough of them damage the
account health metrics that determine whether you can sell at all.

Keeping quantities in step by hand does not scale past a few hundred products,
so most sellers reach for a script. The scripts usually work, and then quietly
stop working, because this problem has three properties that make naive
solutions fail **silently**:

**1. Amazon accepts writes it discards.** An update addressed to a SKU that
does not exist returns success. No error, no rejection, nothing in the response
to distinguish it from a real write. A feed can finish `DONE` with rejected
rows inside it. So "the API call succeeded" and "the quantity changed" are
different claims, and only one of them matters.

**2. Comparing feeds instead of state makes failures permanent.** The obvious
design compares today's feed with yesterday's and pushes the difference. Watch
what that does to a single failed write:

```
Monday     supplier says 0. Change detected. Push. It FAILS.
Tuesday    supplier still says 0. Compare 0 with 0 → "no change".
Wednesday  no change. Thursday, no change. Forever.
```

One transient failure becomes permanent, and nothing anywhere records it. The
listing keeps selling stock that does not exist.

**3. Barcodes do not match.** Feeds carry barcodes as numbers, so leading zeros
are stripped somewhere upstream. Amazon SKUs keep them. Get the reconciliation
wrong and a large share of the catalogue is addressed as SKUs that do not
exist — which, per (1), Amazon accepts and ignores.

Every one of those failures is invisible. Nothing crashes, no run goes red, and
the dashboard says everything is fine.

## The approach

**Compare against Amazon's own reported quantity, never against the last feed.**

That one decision fixes (1) and (2) structurally rather than by being careful:

- a failed push leaves the difference in place, so the next run retries it
- running twice is harmless, so retrying is never dangerous
- a human editing a quantity in Seller Central is detected as drift

**Then read back what you wrote.** Every push is followed by re-reading the
affected SKUs and comparing. What did not stick is recorded and retried, not
assumed. Large batches verify a sample and settle the rest from the next
catalogue refresh — and the dashboard says plainly when a batch was sampled
rather than fully verified, because pretending otherwise would be worse than
admitting it.

---

## Architecture

One run is one pass of an eight-stage pipeline. Stages are ordered so that
**durable state is committed before anything irreversible happens.**

```
  ┌────────────────────────────────────────────────────────────────┐
  │  0  RECOVER   settle any batch a previous run left mid-flight  │
  ├────────────────────────────────────────────────────────────────┤
  │  1  FETCH     list the supplier's FTPS folder, download new    │
  │               archives                                         │
  │                                                                │
  │        ──── the FTPS connection closes HERE, deliberately ──── │
  │                                                                │
  │  2  READ      unzip, stream-parse delimited rows                │
  │  3  STORE     upsert products, record change history            │
  │                                                                │
  │        ═══ checkpoint: supplier data is durable ═══             │
  │                                                                │
  │     REPORTS   five .xlsx files, written whether or not          │
  │               Amazon is configured at all                       │
  │                                                                │
  │  4  ASK       what does Amazon currently show?  (cached)        │
  │  5  MATCH     barcode → confirmed SKU, several strategies       │
  │  6  DECIDE    what each quantity should become                  │
  │  7  SAFETY    eight guardrails; any one halts the run           │
  │                                                                │
  │        ═══ DURABILITY BARRIER ═══                               │
  │        push_items, each carrying previous_quantity,             │
  │        are COMMITTED before a single byte is sent               │
  │                                                                │
  │  8  SEND      write quantity only, then read Amazon back        │
  └────────────────────────────────────────────────────────────────┘
```

### Why the connection closes at stage 1

Parsing a million-row feed takes minutes. Holding an FTP control connection
open while doing slow local work gets it closed by the server — correctly, it
is their bandwidth — and the next download then fails with a raw `OSError`.

The subtle damage is not the failed download. It is that a raw `OSError`
escaping the per-file handler takes the whole transaction down, **rolling back
the feed that had just been read successfully.** Downloads now happen back to
back, the connection is hung up, and parsing happens with nothing held open.

### Why prices are structurally impossible

Not "we don't send prices". Three independent layers:

1. `Decision` has no price field. There is nothing to populate.
2. A guardrail scans every decision for price-shaped data before the send path.
3. `assert_quantity_only()` walks the outgoing payload and raises
   `PriceFieldRefused` on any price-related key, however nested.

There is also a `never_send_price` row in the settings table marked
`locked=True`. It is **deliberately read by no code** — it exists so nobody can
later add a setting that pretends to turn the rule off. A test asserts every
*other* setting is actually read by something.

The asymmetry justifies the paranoia: a false positive costs a developer five
minutes and a stack trace. A false negative overwrites a price somebody
calculated deliberately, with no error anywhere, and may go unnoticed for weeks.

---

## The safety model

### The durability barrier

> `push_items` rows, each carrying `previous_quantity`, are **committed**
> before `send_batch` transmits anything.

Everything else rests on this. If the process is killed mid-send — power cut,
deploy, OOM, restart — the record of what the old quantity *was* has already
survived. Undo therefore always has something to restore.

It is easy to get wrong in the natural direction. Wrapping a run in a single
transaction (ingest → decide → push, all or nothing) is the obvious design and
is exactly wrong here: a crash during sending rolls back the very rows undo
needs, so the changes reach Amazon and the record of them does not.

### Why a missing feed cannot zero a catalogue

Suppliers signal "discontinued" by silently dropping the row, so absence is the
only available signal — which makes a truncated download indistinguishable from
"everything sold out". Four independent things prevent that:

- the counter only ever increments when a **full** feed was actually
  processed; absence from a delta feed means "unchanged", never "gone"
- a day with no full feed at all increments nothing
- the feed-size guardrail compares against the **median** of previous feeds, so
  one anomalous feed cannot drag the baseline with it
- the zeroing limit caps the damage even if all of the above were wrong

### The eight guardrails

Any one halts the run before anything is sent.

| Guardrail | Catches |
|---|---|
| % of catalogue changed | a feed that disagrees with reality wholesale |
| number of products being zeroed | a catalogue-wide wipe |
| feed rows vs **median** of previous feeds | a truncated download |
| feed header matches known columns | a changed or wrong file |
| **no price in any decision** | the one rule that is not configurable |
| unmapped-barcode spike | supplier changed their barcode format |
| catalogue freshness | our picture of Amazon is too old to trust |
| per-run change limit | any run that looks like a runaway |

### Three modes

| Mode | What it does |
|---|---|
| `dry_run` | Reads everything, decides everything, **writes nothing**. The default. |
| `needs_approval` | Prepares a batch and waits for a human to approve it |
| `automatic` | Sends, within the guardrails |

Practice mode is enforced at the HTTP client, so it is the same code path with
the same payload and no side effects — a real rehearsal rather than a separate
branch that might diverge.

One subtlety worth noting: `reports.create` is an HTTP POST that changes
nothing on the account, so practice mode must still allow it. Blocking it means
practice mode can never fetch the catalogue, so it can never demonstrate
anything — a "cautious" practice mode that is simply broken, and that reports
success while being so.

---

## Quick start

Requires Python 3.12+ and PostgreSQL 16. SQLite is used for tests only; the app
refuses to start in production on SQLite, for the reason given below.

```bash
git clone https://github.com/abuhuraira99/amazon-inventory-sync
cd amazon-inventory-sync

python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # then fill in MASTER_KEY, SESSION_SECRET, DATABASE_URL
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or with Docker:

```bash
cp .env.example .env
docker compose up -d
```

The dashboard binds to `127.0.0.1` by design. Reach it over an SSH tunnel,
Tailscale or Cloudflare Tunnel — **do not** expose port 8000.

### Try it without an Amazon account

The coverage tool runs offline, against two files you can export in a few
minutes, and needs no credentials:

```bash
python scripts/stage0_coverage.py     --feed     FULL_FEED.zip     --listings All+Listings+Report.txt     --prefix   YOUR-PREFIX-
```

Pass `--prefix` for each SKU prefix you want measured — without it nothing is
in scope, which is the same fail-safe default the running system uses.

It answers the question worth answering before enabling any writes: *how much
of this catalogue could actually be synced, and what would be missed?* It also
shows what naive SKU construction would have matched, which is usually the
moment the barcode-padding problem becomes concrete.

---

## Configuration

Two kinds, deliberately separated:

**`.env`** — infrastructure. Database URL, encryption key, file server,
credentials, SMTP. Changes when the machine or account changes. Requires a
restart. Not editable from the web interface.

**Settings page** — 41 business settings stored in the database, edited by the
operator, audited on change, and re-read every run so a change takes effect
without a restart. There is no second place where behaviour is configured.

The shipped defaults are the cautious end of every range:

| Setting | Default | Why |
|---|---|---|
| `sync_mode` | `dry_run` | writes nothing until somebody decides otherwise |
| `sku_prefixes_in_scope` | `[]` | **empty means nothing is in scope**, not everything |
| `missing_full_feeds_before_zero` | `2` | one absence is not proof of discontinuation |
| `never_send_price` | locked `True` | read by no code, by design |

A new installation therefore does nothing to anybody's account until it is
deliberately configured — and *will* halt on its first full feed until an
operator has looked at the proposals and widened the thresholds to match their
catalogue. That is the intended experience, not a rough edge.

### Credential handling

Credentials are encrypted with AES-GCM under a master key held only in the
environment, never in the database. The web interface can show the last four
characters and nothing else — there is no code path that returns a stored
secret to a browser. The operator enters their own credentials, which means a
developer can build and support the system without ever holding one.

---

## Interesting engineering

Things worth reading the source for, if you are evaluating whether this is
serious work:

| | Where |
|---|---|
| **Barcode normalisation** across EAN/UPC/GTIN forms. Uses the GTIN check digit to tell "a leading zero was stripped" from "this is genuine rubbish" — validity as a *ranking signal*, never a gate, because real catalogues contain listings whose barcode fails the checksum. | [`app/core/barcode.py`](app/core/barcode.py) |
| **Read-back verification**, with honest sampling above a threshold and settlement from the catalogue refresh — which costs no extra API calls, because the quantities are already in memory. | [`app/engine/pusher.py`](app/engine/pusher.py) |
| **The durability barrier** and the advisory-lock run lock. | [`app/db.py`](app/db.py) |
| **Undo**, to a batch, several batches, or a whole past day. | [`app/engine/rollback.py`](app/engine/rollback.py) |
| **Recording a failure that is itself a database error** — a DB error aborts the transaction, so writing "this run failed" fails too with `PendingRollbackError`, and the run stays "Running" for ever with no alert. | [`app/engine/pipeline.py`](app/engine/pipeline.py) |
| **Rate limiting** at a fraction of documented ceilings, because the quota belongs to the seller account and other tools draw on it. | [`app/amazon/client.py`](app/amazon/client.py) |
| **Sanitising at the edge** — one NUL byte anywhere in a million rows raises `psycopg.DataError` and kills the whole catalogue load, because PostgreSQL text cannot contain NUL. | [`app/vendor/parser.py`](app/vendor/parser.py) |
| **Material Design 3 dashboard**, hand-written CSS, no framework, no build step, works without JavaScript. | [`app/static/`](app/static/) |

### Some decisions and their reasons

**PostgreSQL required in production.** The run lock is a `pg_try_advisory_lock`,
so two runs can never overlap. On SQLite it is a no-op — which is why
`app/config.py` refuses to start in production on SQLite rather than running
without a lock it believes it has. Tests use SQLite and that is the only place
it is allowed.

**XlsxWriter, not CSV.** Opening a CSV containing `0007298811035` in Excel
silently turns it into `7298811035`. Someone then copies that into an Amazon
template and produces a SKU that does not exist. Real `.xlsx` with the barcode
columns declared as text cannot be mangled that way.

**No `print()` anywhere in `app/`.** A `print` bypasses the credential
redaction filter *and* goes to a stdout that a service manager may discard
entirely. Everything goes through a rotating file handler.

**No CI secrets and no automatic deploy.** Deliberate. A pipeline that could
authenticate to a live seller account is a pipeline that could change a live
listing, and no amount of care makes that safe to leave sitting in a
repository. The test suite needs neither: it runs on in-memory SQLite with
Amazon and the supplier simply not configured, which is also a realistic state.

---

## Development

```bash
pip install -r requirements-dev.txt

pytest                       # 377 tests, ~50s, no network, no real database
ruff check app tests
mypy app                     # 42 source files, strict
```

All three run in CI on every push and pull request. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | the eight stages, and why each decision was made |
| [docs/SAFETY-MODEL.md](docs/SAFETY-MODEL.md) | durability, verification, guardrails, undo |
| [docs/DATA-MODEL.md](docs/DATA-MODEL.md) | the tables and what each one is for |
| [docs/ADAPTING.md](docs/ADAPTING.md) | a different feed format, or a different marketplace |
| [docs/AMAZON-APP-SETUP.md](docs/AMAZON-APP-SETUP.md) | creating the SP-API application, and which roles **not** to request |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | running it for real |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | day-to-day, and the staged rollout |

## Status and scope

Working software with a real test suite, not a finished product. It syncs
quantities from one supplier feed to one marketplace. It does not manage
prices, listings, orders or fulfilment, and it is not a general SP-API client.

Adapting it to a different feed format is mostly configuration; a different
marketplace needs a little code. Both are covered in
[docs/ADAPTING.md](docs/ADAPTING.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
