# Phase 11B — financial planning

Phase 11A built the intelligence layer and needed no schema change, because
everything in it was *derived*: a trend, a health score and an anomaly are all
opinions about transactions, recomputable from the ledger and owning no state.

A goal is the opposite, and that is the whole reason this phase exists
separately. It is a statement of intent that no amount of transaction history
can infer — nobody's spending reveals whether they are saving for a wedding or a
deposit, and guessing would be exactly the fabrication the rest of Phase 11
exists to prevent. So it has to be stored, and storing it is a schema change.

---

## What was built

| Piece | Where |
|---|---|
| `Goal`, `GoalContribution` | `models.py` |
| Migration | `migrations/versions/20260802_09_goals.py` |
| Tracking, momentum, projection | `dough/services/goals.py` |
| Page and routes | `dough/blueprints/goals.py`, `templates/goals.html` |
| Styling | `static/css/goals.css` |
| Tests | `tests/test_goals.py` — 40 |

---

## Design decisions worth knowing about

### Two tables, not one column

`goals` holds the target and the running total; `goal_contributions` holds the
deposits behind it. A single `saved_amount` would answer "how much have I
saved?" and neither of the two questions that make goal tracking worth having:
*how much did I put aside last month*, and *is my momentum improving*.

### `saved_amount` is stored, not derived from an account balance

The tempting design links a goal to a savings account and reads its balance. It
breaks on the common case: people save for several goals in one account, so a
balance cannot be divided between them. The first time somebody adds a second
goal, every figure silently becomes the same number.

### Momentum is the recent rate, not the lifetime average

A goal funded hard for three months and then abandoned has a healthy lifetime
average and no momentum at all — and the average would keep projecting a
completion date that is not coming.
`test_an_abandoned_goal_has_no_momentum_despite_a_healthy_total` is that case.

### A projection returns `None` rather than a distant date

"At this rate, never" and "in 84 months" are different statements, and only the
first is true of a stalled goal. Where there is no rate to project from, there
is no date — and where the rate is real but glacial, the answer is "more than
ten years at $100 a month" rather than a date in 2041, which is arithmetic
rather than information.

Every projection carries the basis it rests on and a `confidence`, and the page
prints them next to the date. A date with no stated basis reads as a promise.

### `target_date` is nullable

"Pay off the card" and "$20,000 by June" are both goals and only one has a date.
Requiring one would make the app ask for a number the user has not decided, and
any default it offered would become a deadline they never set. The service
projects a completion date from momentum instead, clearly labelled as a
projection, and compares against a user-set deadline only when there is one.

### Withdrawals are recorded, not refused

`GoalContribution.amount` may be negative. Money comes back out of a holiday
fund, and recording it keeps the history and the balance honest — refusing it
would push people to edit the total directly, which loses the record entirely.
`saved_amount` is floored at zero, because a negative progress bar renders as
nonsense rather than as the data-entry slip it is.

### Achievement settles in both directions

Reaching the target marks a goal `achieved`; raising the target afterwards puts
it back to `active`. Without the second half, extending a goal you had just met
would quietly stop tracking it.

---

## Migration

`20260802_09_goals` is additive — two `CREATE TABLE`s, no existing table
touched, nothing to backfill (a household with no goals correctly has no rows).
Both tables are tenant-scoped with `household_id` NOT NULL, a foreign key, and a
standalone index, and the unique index on goal name leads with the household so
two families may both have an "Emergency fund".

Verified against a copy of the live database before applying:

| Check | Result |
|---|---|
| Row counts | **23 tables unchanged**, 2 added at 0 rows, nothing lost |
| `tools/verify_tenancy.py` | **161 invariants hold** (up from 145; sixteen new checks cover the two tables) |
| Migration chain | one head, round-trips, matches `db.metadata` |

---

## UI

`/goals` — a card per goal with its progress bar, remaining amount, recent pace,
projected finish, and a deadline comparison when one is set. Contributing is a
two-field form on the card itself; editing and deleting live behind a collapsed
`<details>` so the common action stays the prominent one.

The primary nav goes back to seven links. Phase 11A took it from seven to six by
folding Anomalies and Recurring into the Insights hub; Goals spends one of the
freed slots. That is the intended trade rather than a regression — the same
width now carries two destinations people visit instead of two single-table
pages they rarely did, and the bar is only revealed at 1024px, where seven fit.

---

## Not built

The brief's Phase 11B list also names **savings projections** and **long-term
planning** as separate items. What exists is the per-goal projection described
above. A household-level projection — "at your current rate, here is your net
worth in ten years" — is a different feature: it needs assumptions about
returns, inflation and contributions that this application does not currently
ask for, and inventing them would produce a confident number resting on nothing.
`investments_intel` already has a projection with stated assumptions for the
portfolio specifically; extending that to the whole household is the natural
next step and wants its own decisions about what to assume.

The copilot has no `goals()` surface yet. `goals.summary()` is shaped for one —
it already reports stalled and behind-plan goals — but wiring it means adding a
context section and a prompt, which belongs with the pass that puts the other
generated surfaces behind routes.
