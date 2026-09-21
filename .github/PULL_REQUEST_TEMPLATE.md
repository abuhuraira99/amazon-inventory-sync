<!--
Explain WHY, not what. The diff already says what.
The reasoning is the part a reviewer cannot reconstruct from the code.
-->

## What this changes

## Why

<!-- What problem does this solve? If it fixes a bug, what caused it? -->

Closes #

---

## Safety properties

These three are the reason this project is defensible to point at a real seller
account. Tick the ones this change **does not** affect; if you cannot tick one,
explain below.

- [ ] **The durability barrier** is intact — `push_items` carrying
      `previous_quantity` are still committed *before* anything is sent, and a
      run is still not a single transaction.
- [ ] **Quantity only** — no new price field, and the three independent guards
      are untouched.
- [ ] **Undo still works** — and if `rollback.py` changed, I read its tests
      first and they still pass.

<!-- If any box is unticked, say what changed and why it is safe: -->

Also consider, and mention if relevant:

- [ ] A new setting is actually **read** by something (`test_settings_take_effect.py`)
      and takes effect **without a restart**.
- [ ] No new silent fallback — a configuration error still announces itself.
- [ ] The scope filter still fails safe (an empty prefix list means *nothing*
      is in scope).

## Testing

- [ ] I proved the bug before fixing it: the new test **failed** against the
      unfixed code and passes now.

```
# git stash push -- app/ && pytest tests/test_x.py   -> FAILED
# git stash pop          && pytest tests/test_x.py   -> PASSED
```

<!-- Or, for a change that is not a bug fix, say what you tested and how. -->

## The three gates

- [ ] `pytest`
- [ ] `ruff check app tests` — and I did **not** run `ruff format`
- [ ] `mypy app`

<!-- If the test count changed, update it in README.md (badge + dev section)
     and CONTRIBUTING.md. -->

## Anything a reviewer should look at closely

<!-- A decision you were unsure about, a trade-off you made, something you
     would like a second opinion on. This section is worth filling in. -->
