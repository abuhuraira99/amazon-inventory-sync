# The safety model

This system writes to accounts that take real orders. A mistake does not
produce a failing test — it produces oversold orders, cancellations, and damage
to the account health metrics that decide whether a seller can sell at all.

Worse, every one of the characteristic failures here is **silent**. Nothing
crashes. No run goes red. The dashboard says everything is fine while part of
the catalogue quietly stops being synced, or products go off sale that should
not have.

So the design assumption throughout is: *the failure you have to guard against
is the one that reports success.*

## Contents

- [The durability barrier](#the-durability-barrier)
- [Read-back verification](#read-back-verification)
- [Prices are structurally impossible](#prices-are-structurally-impossible)
- [Why a missing feed cannot zero a catalogue](#why-a-missing-feed-cannot-zero-a-catalogue)
- [The guardrails](#the-guardrails)
- [The three modes](#the-three-modes)
- [The run lock](#the-run-lock)
- [Undo](#undo)
- [Failure handling](#failure-handling)
- [What is deliberately not protected](#what-is-deliberately-not-protected)

---

## The durability barrier

> `push_items` rows, each carrying `previous_quantity`, are **committed** via
> `checkpoint()` **before** `send_batch` transmits anything to Amazon.

This is the single most important property in the system. Everything else rests
on it.

**Why it matters.** If the process is killed mid-send — power cut, deploy, OOM,
restart, a service manager stopping the job — the record of what the old
quantity *was* has already survived. Undo therefore always has something to
restore.

**Why it is easy to get wrong.** Wrapping a run in one transaction (ingest →
decide → push, all or nothing) is the obvious, tidy design. It is exactly wrong
here. A crash during sending rolls back the very rows undo needs, so the
changes reach Amazon and the record of them does not. You are left with a live
account in a state you cannot describe, let alone reverse.

That is the asymmetry: the *changes* are not transactional, because they are
already on somebody else's server. Only the record can be, and it has to be
committed first.

If you find yourself moving, batching or deferring that commit for performance,
stop.

## Read-back verification

Amazon reports success for writes it silently discards. An update addressed to
a SKU that does not exist is accepted and ignored; a bulk feed can finish
`DONE` with rejected rows inside it. Nothing in a successful response
distinguishes "applied" from "accepted and thrown away".

So after every push, the affected SKUs are re-read and compared against what
was intended. What did not stick is recorded and retried, not assumed.

### Honest sampling

Verifying item by item costs one GET per SKU. Above a few thousand SKUs, at a
polite 2 requests/second, that takes longer than the sync interval itself. So
batches above `VERIFY_SAMPLE_THRESHOLD` verify a random sample, and the
remainder is settled from the next catalogue refresh.

Two things make this honest rather than a corner cut:

**The fallback is actually built.** A docstring promising "the rest will be
settled by the daily refresh" is easy to write and easy to never implement —
and its absence produces no error. Every unsampled item simply stays `ACCEPTED`
for ever, a large run reports a handful confirmed and never moves, and
"Refresh catalogue" appears to do nothing because for those rows it did
nothing. `confirm_from_catalogue()` is that fallback, and it costs **no extra
Amazon requests at all**: the catalogue refresh already downloaded every
listing's quantity, so confirming from that dictionary is free.

**The dashboard says so.** A batch that was sampled rather than fully verified
is labelled as such. Pretending otherwise would be worse than admitting it.

A SKU missing from the report is left alone rather than marked failed. The
report covers in-scope listings; absence is not evidence, and calling it a
failure is how a system teaches its operator to ignore alarms.

## Prices are structurally impossible

Not "the system does not send prices". Not a setting that defaults to off.
Three independent layers, none of which is configurable:

1. **`Decision` has no price field.** There is nothing to populate. A price
   cannot be represented in the type that describes a change.
2. **A guardrail** (`check_no_price_in_decisions`) scans every decision for
   price-shaped data before the send path is entered.
3. **`assert_quantity_only()`** walks the outgoing payload immediately before
   transmission and raises `PriceFieldRefused` on any price-related key,
   however deeply nested. The match list is deliberately broad — `price`,
   `pricing`, `currency`, `msrp`, `cost`, `amount`, `money`, `tax`, `map`.

There is also a `never_send_price` row in the settings table marked
`locked=True`. It is **read by no code, by design.** It exists purely so that
nobody can later add a setting that appears to turn the rule off. A test
asserts that every *other* setting is genuinely read by something, so this one
exception is explicit rather than an oversight.

**Why the paranoia is proportionate.** The asymmetry is the whole argument. A
false positive costs one confused developer five minutes and a stack trace. A
false negative overwrites a price somebody calculated deliberately — with
shipping, tax and margin in it — produces no error anywhere, and may not be
noticed for weeks.

The risk is structural rather than hypothetical, because Amazon's own
interfaces put quantity and price side by side. The flat-file template used for
quantity updates carries both in adjacent columns. Sending a price by accident
is a plausible mistake, not an exotic one.

**And there is an outer lock that is not ours.** The SP-API application should
be granted the Product Listing role and **not** Pricing. Then even a total
compromise of this software cannot change a price, because Amazon refuses. See
[AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md).

## Why a missing feed cannot zero a catalogue

This looks dangerous and is not, but the reasoning is worth following because
it is the place a reasonable change could do the most damage.

The hazard: suppliers signal "discontinued" by silently dropping the row. There
is no explicit signal, so absence is the only thing to read — and a truncated
download looks *exactly* like "everything went out of stock".

Four independent things prevent it:

**1. Only a full feed counts.** `VendorProduct.missing_from_full_feeds` is
incremented in exactly one place, `_mark_missing_from_full_feed`, and only when
a full feed was actually processed. Absence from a *delta* feed means
"unchanged", never "gone". The dropped-product logic sits behind `if reconcile:`,
which is only true when a full feed was seen.

**2. A day with no full feed increments nothing.** The counter moves on
processed feeds, not on elapsed time. A supplier outage cannot age products out.

**3. The feed-size guardrail compares against the median.** A full feed
carrying materially fewer rows than the median of previous feeds is
quarantined, not processed. A *median* rather than the last feed, specifically
so that one anomalous feed cannot drag the baseline with it — which is the
failure this check exists to catch.

**4. The zeroing limit caps the damage** even if everything above were wrong.

The default for `missing_full_feeds_before_zero` is **2** rather than 1. One
absence is not proof of discontinuation, and the trade is asymmetric: waiting a
day costs a day of stale availability, while acting on a bad feed takes real
products off sale.

Zeroing never deletes. The listing, its reviews and its ranking all survive;
only the quantity goes to 0, so it returns the moment the supplier restocks.
That asymmetry is why the threshold can be set aggressively without much
regret — a wrongly-zeroed product costs a day of sales, a wrongly-deleted
listing costs everything attached to it.

## The guardrails

`app/engine/guardrails.py`. Each returns a result; any halting result stops the
run before anything is sent.

| Guardrail | Catches | Default |
|---|---|---|
| % of catalogue changed | a feed that disagrees with reality wholesale | 25% |
| products being zeroed | a catalogue-wide wipe | 2,000 |
| feed rows vs **median** of previous | a truncated download | 50% |
| feed header matches known columns | a changed or wrong file | on |
| **no price in any decision** | the rule that is not configurable | always |
| unmapped-barcode spike | supplier changed their barcode format | warn |
| catalogue freshness | our picture of Amazon is too old to trust | on |
| per-run change limit | any run that looks like a runaway | 2,000 |

Three things about how they are designed:

**Messages are written for the operator.** A halt has to explain itself to the
person deciding whether to override it — "Would set 2,431 products to zero; the
limit is 500", not a traceback.

**Warnings and halts are distinguished carefully.** The unreadable-row check
warns rather than halts: every real feed carries a tail of junk rows, and
losing a whole cycle because a few more than usual arrived is a bad trade. A
*jump*, though, means the supplier changed something. Halting on things that
are routinely fine is how a system teaches its operator to click through
warnings.

**The change limit interacts with the percentage check.** `apply_change_limit`
runs first, so a large backlog arrives at the percentage guardrail already
trimmed. Zeros are prioritised ahead of increases, so a truncated run is always
truncated on its least harmful end: taking a product off sale prevents an order
that cannot be fulfilled, while raising a quantity only recovers a sale, and a
sale deferred one cycle is not damage.

**They will fire on a new installation.** The defaults are calibrated for
steady state, not for an account with a backlog of every disagreement at once.
A first full feed halting is the intended experience: look at the proposals,
satisfy yourself they are right, then widen the threshold.

## The three modes

| Mode | What it does |
|---|---|
| `dry_run` | Reads everything, decides everything, **writes nothing**. The default. |
| `needs_approval` | Prepares a batch and waits for a human to approve it |
| `automatic` | Sends, within the guardrails |

Practice mode is enforced in `app/amazon/client.py`, at the HTTP layer — the
same code path, the same payload, no side effects. Enforcing it at the
transport rather than by branching earlier means practice mode cannot silently
diverge from the real path, which would defeat the point of a rehearsal.

One subtlety: `READ_ONLY_WRITE_OPERATIONS = frozenset({"reports.create"})`.
Creating a report is an HTTP POST that changes nothing on the account, so
practice mode must allow it. Blocking it means practice mode can never fetch
the catalogue, so it can never compare anything, so every run honestly reports
"nothing to change" — a cautious-looking mode that is simply broken, and that
reports success while being so.

The approval flow shows every item with its reason in words. An approval step
that does not let the approver see what they are approving is theatre rather
than a control.

## The run lock

A PostgreSQL advisory lock (`pg_try_advisory_lock`), so two runs can never
overlap. Overlapping runs would read the same catalogue picture, decide the
same changes, and send them twice.

On SQLite it is a no-op. Rather than run without a lock it believes it has,
`app/config.py` **refuses to start in production on SQLite**. Tests use SQLite
and that is the only place it is permitted.

A guard that is silently absent is worse than no guard, because the code around
it is written as though it were there.

## Undo

`app/engine/rollback.py`. One batch, several batches, or back to a whole past
day. Reachable from the dashboard.

Undo works because of the durability barrier: every changed quantity was
recorded with its `previous_quantity` committed before the change was sent.

**The failure mode that matters most.** An early version of this code compared
the wrong fields and **silently did nothing while reporting success** — the
worst possible failure for this feature, because it is only exercised in a
crisis, and it tells you it worked. There are tests specifically for that shape
of bug. If you touch rollback, read them first.

Which is also why the advice in [OPERATIONS.md](OPERATIONS.md) to press Undo
deliberately, once, before you need it is not optional. Nobody should trust an
undo button that has never been pressed, and the moment you need it is the
worst possible time to find out it does not work.

An undo is itself a write to the account, and gets the same review as the
change it reverses.

## Failure handling

**Interrupted runs.** `close_interrupted_runs()` runs at start-up and marks any
run still `RUNNING` as `FAILED`. Without it, a restart mid-run leaves a run
showing "Running" for ever.

**Stranded batches.** `send_batch` commits the `SENDING` status before its
first call to Amazon, deliberately. So a crash mid-send leaves a batch marked
`SENDING` with some items `ACCEPTED` and the rest `PENDING`. Stage 0 settles
those on the next run by reading Amazon's actual state, rather than assuming
either outcome.

**Recording a failure that is itself a database error.** A database error
aborts the transaction, so the attempt to write "this run failed" fails too,
with `PendingRollbackError`. The run then stays "Running" for ever, no alert is
queued, and the original cause is buried under a rollback error that says
nothing about it. `_recover_session()` does a `session.rollback()` then
re-fetches the run, and is called **first** in both exception handlers in
`execute_run`.

**Per-file quarantine.** One malformed archive is quarantined and the run
continues. A lost connection is wrapped in `VendorConnectionError` so it does
not take down the whole transaction along with feeds that were already read
successfully.

**Per-SKU failures do not abort a batch.** One broken listing must not stop
every good one behind it. Systemic failures — authentication, permissions — do
abort, because continuing would produce thousands of identical errors and an
audit trail nobody can read.

**Sanitise at the edge.** A single NUL byte anywhere in a million rows raises
`psycopg.DataError`, because PostgreSQL text columns cannot contain NUL, and
kills the entire catalogue load. Control characters are stripped at the vendor
boundary in `app/vendor/parser.py`, not somewhere in the middle.

## What is deliberately not protected

Being explicit about the edges of the model.

- **Amazon's own behaviour.** If Amazon accepts a write, applies it, and later
  reverts it, this system sees drift on the next run and re-applies. It cannot
  distinguish that from a human edit, and does not try.
- **A wrong supplier feed that is well-formed.** If a supplier sends accurate-
  looking but incorrect stock figures, the guardrails will pass them. Nothing
  here can second-guess the data source; it can only notice that it changed
  shape or size.
- **Prices, listings, orders, fulfilment.** Out of scope permanently. This
  syncs quantities.
- **A compromised host.** If somebody has the master key and the database, they
  have the credentials. The encryption protects a database dump, not a rooted
  machine.
- **Exposing the dashboard.** Binding to `127.0.0.1` is most of its security. A
  deployment that publishes port 8000 has removed a control the design depends
  on.
