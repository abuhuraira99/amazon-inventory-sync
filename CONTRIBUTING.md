# Contributing

Thanks for looking. This document covers setup, the standards a change is held
to, and how to propose one.

## Setup

Python 3.12 or newer. PostgreSQL is needed to *run* the application but not to
develop against it — the test suite uses in-memory SQLite.

```bash
git clone https://github.com/abuhuraira99/amazon-inventory-sync
cd amazon-inventory-sync

python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

That is enough to run every test. You do not need Amazon credentials, an FTPS
server, or network access, and you should not need them to review a change
either — if a contribution cannot be verified without a live account, that is a
problem with the contribution.

## The three gates

All three must pass before a change is ready. CI runs the same three on every
push and pull request.

```bash
pytest                    # 377 tests, ~50s
ruff check app tests
mypy app                  # 42 source files, strict
```

A few notes:

- **Use `ruff check`, not `ruff format`.** Formatting the whole tree buries a
  real change in thousands of lines of noise. Match the surrounding style
  instead.
- Tests never touch the network or a real database. Anything that needs a live
  service is marked `@pytest.mark.integration` and excluded by default.
- `mypy` runs in strict mode. Annotations that are *wrong* are worse than
  missing ones: an over-narrow annotation makes the type checker report genuine
  defensive code as dead, and the temptation is then to delete the guard.

If you change the test count, update it in `README.md` (two places, including
the badge) and here.

## Standards a change is held to

This system writes to accounts that take real orders, and its characteristic
failure mode is silence. Most of what follows exists because the obvious
version of the code fails without telling anybody.

### Prove the bug before you fix it

The standing practice: write the regression test, run it against the
**unfixed** code and watch it fail, then fix it and watch it pass.

```bash
git stash push -- app/
pytest tests/test_the_thing.py     # must FAIL
git stash pop
pytest tests/test_the_thing.py     # must PASS
```

A test that passes both before and after proves nothing, and it is much easier
to write one by accident than it sounds.

### Three properties that must not regress

If a change touches any of these, say so explicitly in the pull request.

**1. The durability barrier.** `push_items` rows carrying `previous_quantity`
are committed *before* anything is sent to Amazon. If you find yourself moving,
batching or deferring that commit — or wrapping a run in a single
transaction — stop. A crash during sending would then roll back the very rows
undo needs, and the changes would reach Amazon while the record of them did
not.

**2. Quantity only, never a price.** Enforced in three independent places, and
none of them is a setting. `Decision` has no price field; a guardrail scans
decisions; `assert_quantity_only()` walks the outgoing payload. Do not add a
price field "for completeness" — there is a locked, deliberately-unread
`never_send_price` setting specifically so that nobody can later introduce one
that pretends to turn the rule off.

**3. Undo must always work.** An early version of the rollback code compared
the wrong fields and **silently did nothing while reporting success** — the
worst possible failure for this feature. There are tests specifically for that
shape of bug. If you touch `app/engine/rollback.py`, read them first.

### Settings, not constants

Anything an operator might reasonably want to change belongs in
`app/core/settings_store.py`, not in a constant. Two rules go with that:

- Something must actually **read** it. `tests/test_settings_take_effect.py`
  asserts that every setting in `SPECS` is read somewhere outside
  `settings_store.py`, because a setting that is accepted, stored and
  redisplayed but never consulted behaves exactly like a hard-coded value and
  produces no error to say so.
- A change must take effect **without a restart**. A schedule setting that only
  applies on restart is, from the dashboard, indistinguishable from one that
  does not work.

Write help text for the person choosing a value, not for a developer: say what
it does, then give the trade-off in *both* directions.

### Fallbacks must not be silent

A fallback that hides a configuration error is worse than a crash. A mistyped
timezone that silently falls back to a default moves the day boundary, makes
the newest feed look like yesterday's, and skips it — with every component
reporting success. Validate at the single point of entry, and when a fallback
does fire, say which one fired.

### Sanitise at the edge

One NUL byte in a million-row feed kills an entire catalogue load, because
PostgreSQL text columns cannot contain NUL. Clean data where it enters the
system, not in the middle.

## Proposing a change

1. **Open an issue first** for anything beyond a small fix, especially if it
   touches the pipeline, the guardrails or the send path. It is worth agreeing
   the approach before either of us spends real time on it.
2. Branch from `main`.
3. Keep the change to one thing. A change that also reformats, renames and
   tidies is much harder to review, and the interesting part gets lost.
4. Make sure the three gates are green.
5. Open a pull request using the template. Explain **why**, not what — the diff
   already says what. The reasoning is the part a reviewer cannot reconstruct.

Commit messages follow the same principle: a short subject line, then prose
explaining the reasoning where it is not obvious.

### Testing style

Tests are named for the behaviour they protect, not the function they call —
`test_a_catalogue_wide_wipe_is_halted`, not `test_check_zeroing_limit`. The
name should tell somebody reading a failure what has broken and why it matters.

Fixture data is synthetic but reproduces the awkward properties of real feeds:
stripped leading zeros, mixed barcode widths, unreadable rows, SKUs belonging
to another supplier. Tidy fixtures test the happy path and nothing else, and
every bug that matters in this system lives in the untidy cases. Synthetic
barcodes carry correct GTIN check digits, because the code uses the checksum to
distinguish a stripped zero from rubbish.

Fake at the HTTP transport boundary, never at the `SpApiClient` level — faking
higher up bypasses the rate limiter, the practice-mode enforcement and the
no-price guard, which are three of the things most worth testing.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Security

Please do not open a public issue for a security problem. See
[SECURITY.md](SECURITY.md).

## Licence

Contributions are accepted under the [Apache-2.0](LICENSE) licence.
