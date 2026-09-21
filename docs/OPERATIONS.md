# Operations

Running this day to day, and — more importantly — getting to the point where
running it automatically is a reasonable thing to do.

## The rollout

**Do not skip stages.** Each one answers a question you cannot answer from the
previous one, and the whole point of the design is that you can take as long as
you like at each step without any risk.

| Stage | Mode | Scope | What it answers |
|---|---|---|---|
| 0 · Measure | — | none | How bad is the problem? |
| 1 · Ingest | practice | supplier only | Can we read the feed reliably? |
| 2 · Propose | practice | full catalogue | What *would* it change? |
| 3 · Pilot | ask first | 25–50 whitelisted SKUs | Does it work on real listings? |
| 4 · Reviewed | ask first | full catalogue | Is a human happy with every batch? |
| 5 · Automatic | automatic | full catalogue | — |

### Stage 0 — measure

Run the coverage tool. No credentials, no network:

```bash
python scripts/stage0_coverage.py --feed FULL_FEED.zip --listings report.txt
```

This is worth doing even if you never deploy the system. It tells you how many
of your listings can be matched to a supplier row, and how many are currently
showing a quantity that disagrees with what the supplier has.

The number that usually decides the project is **listings selling with stock
the supplier does not have**. Each one is a potential cancelled order. A
feed-to-feed tool cannot see that population at all, by construction.

### Stage 1 — ingest

Set `sync_mode = dry_run`, configure the supplier connection, leave Amazon
unconfigured if you like — stages 1 to 3 of the pipeline run without it.

Watch it for a few days. You are checking that feeds arrive when expected, that
the full/delta distinction is being read correctly, and that the rejected-row
count is small and stable.

**If the newest feed is being skipped every day, the timezone is wrong.** Set
it to the *supplier's* timezone. Nothing will report an error; a run that skips
the only file that matters looks completely healthy.

### Stage 2 — propose

Configure Amazon. Still `dry_run`. Now every stage runs and nothing is sent.

Expect guardrails to halt the first full run. That is correct behaviour, not a
fault: the defaults are calibrated for steady state, and an account that has
never been synced starts with a backlog of every disagreement at once. Look at
what it wanted to do, satisfy yourself it is right, then widen the threshold.

**Stay here until you stop being surprised.** This stage costs nothing and is
the cheapest possible place to discover a misunderstanding.

### Stage 3 — does it agree with a human?

The test that actually matters, and the one most often skipped.

Take a sample of proposals from the reports and work out by hand what *you*
would have done for each. Compare. If the system disagrees with you, find out
why before going further — either it is wrong, or your mental model is, and
both are worth knowing now rather than later.

Then switch to `needs_approval` with 25–50 whitelisted SKUs, and let it
actually write to those.

### Stage 4 — full catalogue, reviewed

`needs_approval`, full scope. Every batch waits for a human.

Read the batches. When you find yourself approving without looking, you have
learned something about whether you are ready for stage 5 — though not
necessarily that you *are*.

### Stage 5 — automatic

Only after stage 4 has been boring for a while.

---

## Test Undo on purpose. This is not optional.

Before you reach stage 5 — ideally during stage 3, on a pilot SKU — **press
Undo deliberately and watch the quantity go back.**

Nobody should trust an undo button that has never been pressed, and the moment
you need it is the worst possible time to discover it does not work.

This is a specific rather than a general worry. An early version of the
rollback code compared the wrong fields and **silently did nothing while
reporting success** — the worst possible failure for a feature that is only
ever exercised in a crisis. There are tests for that shape of bug now, but a
test is a claim about the code and pressing the button is a fact about your
deployment.

1. Note a pilot SKU's current quantity in Seller Central.
2. Let a run change it.
3. Press Undo on that batch.
4. Confirm in Seller Central that it went back.

Do it once. Then you know.

---

## Day to day

Most days there is nothing to do. The dashboard exists so that "is it working?"
is answerable in a glance rather than an investigation.

**Worth a look daily**, taking under a minute:

- did runs happen on schedule, and did they succeed?
- anything halted by a guardrail?
- is the unmapped count stable?

**Worth a look weekly:**

- the unmatched list — new products worth listing, or a mapping that broke
- rejected rows — a jump means the supplier changed their format
- disk

**The failure that produces no alert is "no runs at all."** Everything else
reports itself. Watch for it from outside, with an uptime check against
`/api/status`.

---

## Things that will happen, and what they mean

### A guardrail halted the run

Working as designed. The message says which one and by how much.

Ask **why** before raising the threshold. A halt that turns out to be correct
has just saved you real money; a halt you raise the limit for without
understanding is one you have disabled rather than resolved.

Common legitimate causes: the first full run after setup, a supplier catalogue
change, a genuinely unusual day. Common illegitimate cause: a truncated feed,
which is exactly what the check is for.

### Feeds have stopped arriving

Check the supplier connection first, and use "Test the vendor connection" — if
it connects but finds nothing, it lists what the folder does contain.

If files are arriving but being skipped, check the timezone setting before
anything else.

### The catalogue refresh is failing

This matters more than it looks: every decision compares against Amazon's
reported quantity, so a stale catalogue means deciding from an old picture. The
freshness guardrail will eventually halt runs rather than let that continue.

Usually a credential problem. Press "Test Amazon" for a specific answer.

If the refresh has been failing and producing nothing visible, check
`data/logs/app.log` — under a service manager that discards stdout, the log
file is the only record.

### Items stuck on "accepted, not yet confirmed"

Normal for a while. A large batch verifies a sample immediately and settles the
rest from the next catalogue refresh, which costs no extra API calls.

If they stay unconfirmed across several refreshes, that is worth investigating:
either the refresh is failing, or those SKUs genuinely did not take the change.

### A quantity is wrong on Amazon

1. Find the batch that set it (search the SKU).
2. Read what the system thought it was doing — the reason is recorded per item.
3. If it was wrong, **Undo the batch**, then work out why.
4. If the input was wrong, blacklist the SKU until it is sorted.

The per-item reason is there precisely so this does not require reading code.

---

## The controls, in order of bluntness

| Control | Effect |
|---|---|
| Blacklist a SKU | Never touch that one product again |
| Narrow the scope | Stop touching a whole prefix |
| `needs_approval` | Nothing goes out without a human |
| `paused` | Stop all activity; nothing is lost, work resumes when unpaused |
| `dry_run` | Read and decide, write nothing |
| Stop the service | Everything stops |

`paused` is checked before every run, so a paused system stays paused even if a
cycle was already queued.

---

## Housekeeping

Automatic, on an hourly job: expired reports are deleted, old history rows
pruned, queued alerts flushed.

Retention is configurable. If disk is tight, `keep_vendor_history_days` is the
one that matters — it is normally the largest table by a wide margin.

Unconfirmed `push_items` are **deliberately never pruned.** They are the undo
trail, and pruning one because it is old would mean silently discarding the
ability to reverse a change that was never confirmed in the first place.

---

## Before you change a setting

Three questions, in this order:

1. **What does this protect against?** Particularly for a guardrail. If you
   cannot say, do not widen it yet.
2. **Which direction is safe if I am wrong?** Prefer the setting that fails
   towards "stop and ask" over the one that fails towards "write it anyway".
3. **Will I notice if this is wrong?** Most settings here fail silently. That
   is the property the help text is written to counteract, so read it.

Every change is audited with who, when, and what the old value was.
