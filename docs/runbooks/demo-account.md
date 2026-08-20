# The demo account

`RankParsley` is the account shown to prospects and used for marketing
screenshots. It holds no real data, and it is meant to be **regenerated**, not
maintained — everything in it is produced by `tools/seed_demo.py` from a fixed
seed, and every date in it is computed from the day you run the command.

That last point is the reason this runbook exists. A demo whose newest
transaction is four months old reads as an abandoned account, which is the
opposite of what it is for. **The fix is to re-run the seeder, never to edit
data by hand.**

---

## Running it

Always look before you write:

```
railway ssh -- python tools/seed_demo.py --household RankParsley --dry-run
```

That prints the target household and a row count per table, and touches
nothing. Then:

```
railway ssh -- python tools/seed_demo.py --household RankParsley --yes
```

Locally, against your own copy:

```
python tools/seed_demo.py --household RankParsley --dry-run
python tools/seed_demo.py --household RankParsley --yes
```

`--yes` is required. Without it the command explains what it would delete and
exits 1, which is deliberate: this deletes every row the household owns before
it writes anything.

Useful flags:

| flag | what it does |
| --- | --- |
| `--dry-run` | report the target and its current contents; write nothing |
| `--months N` | history length (default 24) |
| `--seed N` | RNG seed; same seed + same day = same household |
| `--household X` | a username, or a household id |

---

## What stops it hitting a real household

Three checks, all before the first delete:

1. The `--household` value must resolve to a household that exists.
2. Every user in that household must be in `demo_seed.DEMO_USERNAMES`.
   **Every**, not any — a household containing one real person is a real
   household even if the demo account was invited into it.
3. `--yes` must be passed.

Failing any of them prints `REFUSED:` with the reason and exits 1. There is no
override flag, and adding one would defeat the point. To make a *new* account
seedable, add its username to `DEMO_USERNAMES` in
`dough/services/demo_seed.py` — a code change, reviewable in a diff.

`market_prices` is the one table the seeder does not clear. It is a shared,
un-tenanted cache of public quotes (see the model docstring), so wiping it
would blank the price cache for every real household in the installation. The
demo's symbols are upserted instead.

---

## What gets generated

24 months of history for a two-income household with about $285k of net worth:

- **~1,220 transactions** across Checking and Savings — two payroll cadences,
  nine fixed bills, five fixed-price subscriptions, and variable spending in
  six categories.
- **7 accounts** across 5 institution connections (Chase, Capital One,
  Vanguard, Fidelity, Coinbase), plus 15 days of sync history including one
  failure that recovered.
- **8 holdings** with cost basis, two of them at a loss.
- **730 daily net-worth snapshots** — the series the Investments chart reads.
- **9 budgets, 5 goals** with contribution history, **16 category rules**.

### The findings are planted, not hoped for

The Insights and Anomalies pages are the demo's best moment and the easiest to
get wrong, because random spending only *sometimes* throws an outlier. These
are generated deliberately and asserted in `tests/test_demo_seed.py`:

- a **large purchase** (BEST BUY, ~$1,899, far above the Shopping median);
- a **duplicate charge** (Trader Joe's, same amount, two days apart);
- a **bill increase** (Beacon Internet, $79.99 → $89.99 six months ago);
- a **category spike** (travel, seasonal);
- **six flagged transactions** carrying `anomaly_score = -1.0`, which is what
  the `/anomalies` *review queue* filters on — a different thing from the live
  statistical findings, and empty unless written.

### The health score lands around 69, on purpose

| factor | value |
| --- | --- |
| Savings rate | ~18% |
| Cash runway | ~6.2 months |
| Budget adherence | 6 of 9 on track |
| Spending trend | poor — up ~21%, seasonal travel plus the planted large purchase |
| Cash flow stability | poor — the CV of a thin monthly surplus is always large |
| Debt burden | ~0.2 months of income |

Two of the six read "poor", and that is the point: a household scoring 95 gives
the Insights page nothing to recommend, and the recommendations are the feature.
If you want a cleaner score for a particular screenshot, raise the budgets in
`BUDGETS` rather than editing rows after the fact.

### Known: the Anomalies page is noisy

The demo shows roughly a dozen `bill_increase` findings on variable merchants
("Amazon costs more than it used to"). This is not a seeding artifact —
`anomalies.bill_increases` compares a merchant's single most recent charge
against the median of its priors with no dispersion test on the non-monthly
branch, so any merchant with varied prices fires about half the time. The real
production database produces *more* of it than the demo does. Worth fixing in
the detector; not something to work around in the demo data.

---

## After running

Sign in as the demo account and look at the dashboard, Investments, and
Insights. `tests/test_demo_seed.py` already asserts every navigation route
renders with generated content in it, so a page that looks wrong is a finding
worth a test, not a reason to hand-edit rows.
