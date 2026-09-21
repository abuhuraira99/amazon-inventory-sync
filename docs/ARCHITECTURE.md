# Architecture

How the system is put together, and why each significant decision went the way
it did. If you only read one other document, make it
[SAFETY-MODEL.md](SAFETY-MODEL.md).

## Contents

- [The shape of the problem](#the-shape-of-the-problem)
- [The eight stages](#the-eight-stages)
- [Why the FTPS connection closes at stage 1](#why-the-ftps-connection-closes-at-stage-1)
- [Why we compare against Amazon, not against the last feed](#why-we-compare-against-amazon-not-against-the-last-feed)
- [Barcode reconciliation](#barcode-reconciliation)
- [Scope as a safety boundary](#scope-as-a-safety-boundary)
- [Transport selection](#transport-selection)
- [Two kinds of configuration](#two-kinds-of-configuration)
- [Scheduling](#scheduling)
- [Observability](#observability)
- [The dashboard](#the-dashboard)
- [Technology choices](#technology-choices)
- [Module map](#module-map)

---

## The shape of the problem

Three properties drive nearly every design decision here. All three produce
failures that are **silent**, which is why the architecture spends so much
effort on verification rather than on throughput.

**Amazon accepts writes it discards.** A quantity update addressed to a SKU
that does not exist returns success. A bulk feed can finish `DONE` and contain
rejected rows. Nothing in a successful response distinguishes "applied" from
"accepted and ignored".

**Absence is ambiguous.** Suppliers signal "discontinued" by dropping the row
from the feed. There is no explicit signal. So a product that vanished and a
download that was truncated look identical, and one of those interpretations
takes a catalogue off sale.

**Identifiers disagree.** The supplier's barcode and the Amazon SKU's embedded
barcode are the same number written two different ways, and reconciling them
wrong silently orphans part of the catalogue.

An architecture that assumed any of these away would look considerably simpler
and would not work.

## The eight stages

One run is one pass of `app/engine/pipeline.py::execute_run`. The ordering is
not incidental: it is arranged so that **durable state is committed before
anything irreversible happens.**

| # | Stage | What it does |
|---|---|---|
| 0 | `RECOVER` | Settle any batch a previous run left mid-flight |
| 1 | `FETCH` | List the FTPS folder, download new archives |
| 2 | `READ` | Unzip, stream-parse delimited rows |
| 3 | `STORE` | Upsert products, record change history |
| — | `REPORTS` | Five `.xlsx` files, written whether or not Amazon is configured |
| 4 | `ASK` | What does Amazon currently show? (cached, refreshed on a schedule) |
| 5 | `MATCH` | Barcode → confirmed SKU |
| 6 | `DECIDE` | What each quantity should become |
| 7 | `SAFETY` | Eight guardrails; any one halts the run |
| 8 | `SEND` | Write quantity only, then read Amazon back |

Two checkpoints matter:

- **after stage 3** — supplier data is durable. Everything downstream can fail
  without losing the ingest.
- **after stage 7** — `push_items` rows carrying `previous_quantity` are
  committed. This is the durability barrier, and it is what makes undo
  reliable. See [SAFETY-MODEL.md](SAFETY-MODEL.md).

Stages 1–3 work with Amazon entirely unconfigured. That is deliberate: it means
a new installation can ingest a feed and produce every report before anybody
has created an SP-API application, which makes the cautious rollout path the
useful one rather than a chore to get through.

## Why the FTPS connection closes at stage 1

Fetching and reading are separate stages, and the FTP connection is closed
between them. This looks like an unnecessary split. It is not.

Parsing a million-row feed takes minutes. If parsing happens inside the
`with connect(...)` block, the control connection sits idle for exactly that
long, and the server closes it — correctly; it is their bandwidth. The next
download then dies with a raw `OSError` (`ConnectionResetError`).

The damage is not the failed download. It is that a raw `OSError` escaping the
per-file handler takes the **whole transaction** down with it, rolling back the
feed that had just been read successfully. The one file the system most depends
on is the one that gets discarded, every time, after all the work of reading it.

Two independent fixes, both kept:

1. `download()` wraps a lost connection in `VendorConnectionError`, so one file
   can be quarantined without destroying the run.
2. The fetch/parse split means the connection is not held during slow work at
   all.

The general rule, worth stating because it generalises well beyond FTP: **do
not hold a network connection while doing slow local work.**

## Why we compare against Amazon, not against the last feed

The single most important design decision in the system.

The obvious approach compares today's feed with yesterday's and pushes the
difference. It is correct for producing a *change report*, and wrong as the
basis for *pushing*:

```
Monday     supplier says 0. Change detected. Push. It FAILS.
Tuesday    supplier still says 0. Compare 0 with 0 → "no change".
Wednesday  no change. Thursday, no change. Forever.
```

One transient failure becomes permanent, and nothing records that it happened.

Comparing the *desired* quantity against *Amazon's own reported quantity* fixes
this structurally rather than by adding retry logic:

- a failed push leaves the difference in place, so the next run retries it
  automatically
- the operation is idempotent, so retrying is never dangerous
- a human editing a quantity in Seller Central shows up as drift and is
  corrected

The cost is needing an accurate picture of Amazon's state, which is why stage 4
exists and why catalogue freshness is itself a guardrail.

It also changes what the system can tell you. A feed-to-feed tool cannot see
listings that are selling with no stock behind them, because by its own measure
nothing changed. Comparing against Amazon makes that population visible, and it
is usually the finding that justifies the project.

## Barcode reconciliation

Fully documented in `app/core/barcode.py`. The summary:

Feeds carry barcodes as numbers somewhere upstream — a spreadsheet cell, a CSV
column, an integer column — and every one of those drops leading zeros. Amazon
SKUs are generated by something that padded them, so they keep the zeros. The
same product therefore arrives as `5319056434` and exists on Amazon as
`EXAMPLE-0005319056434`.

Building the SKU by naive concatenation matches only the products that never
had a zero to lose, and silently misses the rest.

The rule is: **pad to 13, always.** The module exists to make that impossible
to get wrong by accident, and to recover matches for records that do not fit
the 13-digit shape.

**The check digit does the hard part.** Stripping is not the only reason a
short barcode appears — feeds also contain genuine junk. The two need opposite
treatment, and the GTIN checksum separates them: a short code that becomes
valid once a zero is restored was almost certainly stripped; one that does not
is probably rubbish.

Validity is used as a **ranking signal, never a gate**. Real catalogues contain
listings whose barcode genuinely fails the checksum, and refusing to match them
would orphan real products. Matching runs through an ordered list of candidate
forms — canonical 13-digit first, then as-sent, then UPC-12, then GTIN-14, then
zero-stripped — and the first hit wins.

Because the GTIN weighting is anchored at the right-hand end, zero-padding does
not change a code's check digit. That is what lets padding and validation be
reasoned about independently.

## Scope as a safety boundary

Sellers who buy from several suppliers usually encode the source in the SKU
prefix. That makes the prefix a cheap scope filter — and, more importantly, a
safety boundary. Pushing a quantity derived from supplier A's feed onto a
listing sourced from supplier B overwrites a correct number with an unrelated
one, and nothing reports an error.

Two consequences:

**An empty prefix list means nothing is in scope, not everything.** This is the
opposite of the usual convention for empty filters, and it is deliberate: the
conventional reading would mean a fresh install claims every listing on the
account. A new installation touches nothing until somebody states what it owns.

**Partial matches are excluded, not half-included.** A prefix where 70–85% of
listings match the feed looks like it belongs to this supplier but is not
certain. Under a "missing means zero" rule, the unmatched remainder would be
taken off sale. A scope filter that is *mostly* right is worse than one that is
narrow, because its failures are silent and land on real listings.

The `CoverageReport` (`app/engine/mapping.py`) is how an operator decides which
prefixes are safe to include, before enabling writes.

## Transport selection

Two ways to write a quantity, chosen by batch size:

| | Listings Items API | Feeds API |
|---|---|---|
| Shape | one request per SKU | one document, whole batch |
| Good for | small incremental runs | large reconciles |
| Feedback | immediate, per SKU | a processing report, later |

The crossover is a setting. The reasoning: at a polite 2 requests/second, a few
thousand per-SKU calls take longer than the sync interval itself, so a large
batch must go as a single feed. But a feed's per-SKU outcome only arrives in a
processing report afterwards, so the immediate feedback of per-SKU calls is
worth having whenever the batch is small enough to afford it.

**The processing report is not optional.** A feed reports more rows processed
than succeeded and files the difference under "successful with other errors" —
real failures wearing a success label. `parse_processing_report` extracts the
per-SKU outcome so every rejection is recorded against its own push item.

## Two kinds of configuration

Confusing these is a documented way to give somebody wrong instructions.

**`.env` / `app/config.py`** — infrastructure. Database URL, master key, file
server host and credentials, SMTP, scheduler enable. Changes when the machine
or the account changes. Requires a restart. **Not editable from the dashboard**,
because putting credentials on a web page would be a security hole rather than
a convenience.

**`app/core/settings_store.py`** — 41 business settings, stored in the database
as key/JSON, edited on the Settings page, audited on every change.

Key/JSON rather than columns means adding a setting is three lines and no
migration. Reads go through `get`/`get_all`, which fall back to the declared
default when a row is missing, so a fresh database behaves correctly before
anybody visits the page.

Two rules keep that honest:

- **Something must read it.** `tests/test_settings_take_effect.py` asserts that
  every setting in `SPECS` is read somewhere outside `settings_store.py`. A
  setting that is accepted, stored and redisplayed but consulted by nothing
  behaves exactly like a hard-coded value, and produces no error to say so.
- **A change must take effect without a restart.** The pipeline re-reads
  settings at the top of every run; the scheduler re-reads its own on every
  fire.

The single exception is `never_send_price`, which is `locked=True` and read by
nothing on purpose — see [SAFETY-MODEL.md](SAFETY-MODEL.md).

## Scheduling

APScheduler, with four jobs: the sync, the catalogue refresh, a digest, and
hourly housekeeping.

Two things worth knowing, because both are easy to get wrong in the same way:

**Anchored intervals.** The schedule is anchored to a wall-clock offset (e.g.
10 minutes past the hour), not to whenever the process last started. Passing
`next_run_time=datetime.now()` to `add_job` defeats this completely: once a job
has a previous fire time, APScheduler computes `next = previous + interval` and
ignores the trigger's anchor — so every restart re-bases the whole timetable on
whatever minute the machine came back up, and fires an unscheduled run
immediately. Do not add it back.

**Settings that only apply on restart are broken settings.** A rescheduler that
compares only the interval will ignore a changed *minute*; a value read once in
`start()` never changes again. From the dashboard, a setting that takes effect
in two hours is indistinguishable from one that does not work — and neither
produces an error. `tests/test_schedule_anchor.py` holds this in place.

## Observability

The system runs unattended, so "what did it do?" has to be answerable after the
fact.

**Logging never goes only to stdout.** Under Docker, stdout *is* the log and
the platform captures it, which makes stdout-only logging look fine. Under a
service manager that does not capture it — a Windows Scheduled Task, a bare
systemd unit, a cron entry — every diagnostic is discarded by the operating
system, silently. `app/logging_setup.py` therefore installs a
`RotatingFileHandler` as well.

**There is no `print()` anywhere in `app/`.** A `print` bypasses the credential
redaction filter *and* goes to that possibly-discarded stream.

**Errors reach the dashboard, not just the log.** Catching one exception type
and alerting on it is error handling for the error you thought of; everything
else escapes into the scheduler, which logs it and moves on.

**A run row exists even when nothing changed**, because "we checked at 14:05
and everything already matched" is information.

There is one subtle case worth calling out: recording a failure that is *itself*
a database error. A DB error aborts the transaction, so the attempt to write
"this run failed" fails too, with `PendingRollbackError` — the run stays
"Running" for ever, no alert is queued, and the original cause is buried under
a rollback error that says nothing about it. `_recover_session()` does a
`rollback()` and re-fetches the run, and is called *first* in both exception
handlers.

## The dashboard

Server-rendered Jinja2 with hand-written Material Design 3 CSS. No framework,
no build step, no bundler, no `node_modules`.

The reasoning is maintenance rather than purity. This is a handful of pages
with no client-side state worth speaking of. A build step would mean a
toolchain to keep current, a lockfile to audit, and a failure mode where the
deployed asset does not match the source. The pages work without JavaScript;
the JS that exists progressively enhances.

Design tokens live in `md3.css`, components in `app.css`. Alignment in the
token blocks is deliberate, which is part of why `ruff format` is not run on
this project.

Destructive actions — undo, pause, approve — are POST-only with a session
cookie and, where it matters, a typed confirmation. A GET that changes state
can be fired by a prefetching browser or a link preview, and one of these
buttons sends thousands of quantity changes.

## Technology choices

| Choice | Over | Because |
|---|---|---|
| PostgreSQL in production | SQLite | The run lock is a `pg_try_advisory_lock`. On SQLite it is a no-op, so `config.py` **refuses to start** in production on SQLite rather than running without a lock it believes it has. |
| SQLite in tests | PostgreSQL | Tests must run with no services and no network. It is the only place SQLite is allowed. |
| stdlib CSV reader | a dataframe library | Comparable speed for flat delimited text at this shape, and it removes a large dependency from the deployed image. For one parser on a schedule, that trade favours the standard library. |
| Streaming parse | load then process | A million rows as dicts costs over a gigabyte. Peak memory is a function of batch size, not feed size. |
| XlsxWriter | CSV | Excel silently destroys leading zeros in a CSV on open. Someone then builds a SKU from the mangled value. `.xlsx` with columns declared as text cannot be mangled that way. |
| `certifi` for **both** HTTP and FTPS | the OS trust store | Two trust stores in one process means one side works and the other fails on the same machine. See below. |
| httpx with HTTP/2 | requests | Async-capable, HTTP/2, and a transport seam that makes the whole Amazon layer testable without a network. |
| Alembic | `create_all` | Schema changes have to be reviewable and ordered. |

### The two-trust-stores trap

Worth its own note because the symptom points everywhere except the cause.

`httpx` builds its default TLS context from `certifi`. `ftplib` uses the
operating system's store. On Windows that store is not fixed — it ships with a
small set of roots and fetches the rest on demand, and that fetch happens for
the *system* TLS stack, not for Python's.

The result: the Amazon half of the application works perfectly while FTPS fails
with `CERTIFICATE_VERIFY_FAILED` / "unable to get local issuer certificate",
against a server whose certificate is valid and whose chain is complete, on a
machine where a browser loads the same site happily. Nothing is wrong with the
server, the credentials or the code.

`_tls_context()` pins FTPS to `certifi` explicitly, so "is this certificate
trusted?" is a property of the release rather than of the machine it landed on.
`certifi` is pinned in `requirements.txt` for the same reason.

The tempting fix — turning verification off — must never be how this is
resolved on a system holding seller credentials. `tests/test_vendor_tls.py`
holds both the fix and its shape in place.

## Module map

### `app/engine/` — the brain

| File | What it does |
|---|---|
| `pipeline.py` | `execute_run` and the eight stages. The core. |
| `guardrails.py` | The safety checks. Any one halts a run. |
| `mapping.py` | Barcode → Amazon SKU, several strategies; the catalogue index |
| `decision.py` | What each quantity should become |
| `rollback.py` | Undo |
| `pusher.py` | Batch send plus read-back verification |
| `report_builder.py` | The five report files |

### `app/vendor/` — the supplier side

| File | What it does |
|---|---|
| `ftp_client.py` | FTPS explicit TLS, MLSD→NLST fallback, downloads |
| `parser.py` | Unzip, streaming delimited parse, row validation |
| `filename.py` | Parse feed filenames, decide what "today" means |

### `app/amazon/` — the SP-API side

| File | What it does |
|---|---|
| `client.py` | HTTP, retries, rate limits, **practice-mode enforcement** |
| `feeds.py` | Feeds API (bulk) |
| `listings.py` | Listings Items API (per SKU) |
| `reports.py` | All Listings Report download and parse |
| `lwa.py` | Login with Amazon tokens |
| `guard.py` | The final no-price check before transmission |

### `app/` — the frame

| File | What it does |
|---|---|
| `models.py` | The tables and enums — see [DATA-MODEL.md](DATA-MODEL.md) |
| `scheduler.py` | APScheduler jobs |
| `config.py` | Environment settings and start-up refusals |
| `notifier.py` | Email alerts, queued and flushed |
| `main.py` | FastAPI app, lifespan, security headers |
| `services.py` | Readiness checks for the dashboard |
| `db.py` | Session scope, `checkpoint()`, the advisory lock |
| `logging_setup.py` | Rotating file log plus credential redaction |
| `core/settings_store.py` | The 41 editable settings |
| `core/barcode.py` | EAN/UPC normalisation, check digits |
| `security/` | Login, 2FA, credential encryption, audit |
| `routers/` | Dashboard, settings, actions, read-only JSON |
