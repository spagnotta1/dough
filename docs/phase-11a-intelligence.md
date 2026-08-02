# Phase 11A — the intelligence layer

Ask Dough could already answer questions. What it could not do was *reason over
derived numbers*: every figure it discussed had to be found by the model in a
pile of transactions, which is slow, expensive in tokens, and — the part that
matters — unverifiable. A total the model computed in prose looks exactly like a
total the database computed, and only one of them is reliably right.

Phase 11A is the layer that does the deriving: eight modules in
`dough/services/`, plus a `FinancialCopilot` orchestrator in `dough/ai/` that
coordinates them and is the single thing any surface talks to. No schema change,
no new dependency, and no change to authentication, Plaid synchronisation,
household isolation or the data model.

---

## The one idea

> The model receives **conclusions and the figures behind them**, never the rows
> those conclusions came from.

Everything else here follows from that. A trend is a direction, a slope and a
confidence — not twenty-four months of history for the model to eyeball. An
anomaly is a finding with its evidence attached — not the transactions that
triggered it. The model's job is to *explain*, and the analytics layer's job is
to be *right*.

This is also the security property. `dough/services/ai_context.py` builds every
figure from this household's rows through `TenantScopedQuery`, so there is no
path by which a prompt could carry another household's data — asserted in
`tests/test_ai_context.py::test_the_context_never_carries_another_households_figures`,
which serialises a real context and greps it.

---

## What was built

| Feature | Module | Entry point |
|---|---|---|
| 2 — What changed? | `periods.py` | `compare`, `compare_kind`, `compare_ranges` |
| 3 — Spending trends | `trends.py` | `category_trends`, `merchant_trends`, `unit_cost_trend` |
| 4 — Financial health | `health.py` | `score` |
| 6 — Anomaly detection | `anomalies.py` | `detect`, `summary` |
| 7 — Financial search | `finsearch.py` | `search`, `parse` |
| 11 — Proactive insights | `proactive.py` | `insights`, `digest` |
| 12 — AI context builder | `ai_context.py` | `build` |
| 14 — Performance | `analytics.py` | every rollup is a `GROUP BY` |
| **Orchestration** | `dough/ai/copilot.py` | `FinancialCopilot` |
| 1 — Monthly review | `dough/ai/copilot.py` | `monthly_review()` |
| 5 — Budget coaching | `dough/ai/copilot.py` | `budget_coaching()` |
| 13 — Prompts | `dough/ai/persona.py` | `COPILOT_GROUNDING` + two formats |

Features 8 and 9 (investment intelligence, affordability analysis) are the two
AI surfaces still to build; both are now a method on `FinancialCopilot` rather
than a new integration. Feature 10 (goals) is Phase 11B, because it is the only
part that needs a table.

---

## The orchestrator

```
Route / API / scheduler
    ↓
FinancialCopilot            dough/ai/copilot.py
    ↓
dough/services/*            analytics · periods · trends · health · anomalies
    ↓                       budgets · insights · finsearch
ai_context.build()          the summarised snapshot
    ↓
AIService → adapter → Anthropic
```

### Why it lives in `dough/ai/` and not `dough/services/`

`dough/services/README.md` forbids a service from importing an LLM client. An
orchestrator has to reach both the analytics layer and the model, and `dough/ai/`
is the only package permitted both directions — `service.py` already imports
`dough.services.audit` and `dough.services.ratelimit`. Putting it in
`dough/services/copilot.py` would have inverted a documented rule.

Its `dough.services.*` imports are function-local, matching `service.py`, so
`import dough.ai` does not drag in the analytics layer and `models` at package
import time.

### What it actually does

Not a pass-through. Three concrete jobs:

**1. One coordinated pass.** `analytics()` calls each expensive service exactly
once and threads the result through everything downstream. `anomalies.detect()`
is the costliest call in the layer and *three* things need it — the list, the
counts, and `proactive.insights()`. `periods.compare()` is four aggregate
queries and two things need it. Both now run once.

`tests/test_copilot.py::test_one_pass_calls_each_expensive_service_once` counts
the calls, so a surface added later cannot quietly reintroduce the duplication.

**2. Two distinct caches.** The analytics cache holds *computed figures* with a
120s TTL — short, because a freshly imported statement should appear on the next
page load, not in an hour. `AIService.cached` continues to hold *written prose*.
Both are household-scoped through `TenantScopedTTLCache`, which resolves the
household on every operation and raises when there is none.
`test_the_analytics_cache_is_scoped_to_a_household` proves A's cached figures are
invisible from B. `invalidate()` exists for post-import/post-sync.

**3. One place to decide.** `SURFACE_SECTIONS` maps each surface to the context
sections it needs. A budget coach shipping the portfolio pays tokens to make its
own answer slower, and that decision was previously made — differently — at each
call site.

### Measured effect

On ~3,300 transactions, rendering everything the Insights hub needs:

| Path | Time |
|---|---|
| Uncoordinated (each service called independently) | 286.1 ms |
| Orchestrated, cold | **185.1 ms** |
| Orchestrated, warm cache | **~0 ms** |

### Retrieval before generation

`answer()` runs `finsearch.search()` on the question *first* and puts the
retrieved figures in the system prompt above the general snapshot, labelled as
computed for that exact question. When the parse matches nothing the block is
omitted entirely rather than sent empty — an empty result presented as an answer
is how "you spent $0 on that" gets said to somebody who spent plenty.

### The availability asymmetry, preserved

Briefings (`brief`, `monthly_review`, `budget_coaching`) degrade to
`{'available': False}`; `answer()` raises. That is the contract
`dough/api/v1/copilot.py` established: a briefing is optional furniture and a
card can be omitted, but somebody who typed a question and pressed send has made
a request that cannot be satisfied.

### Budget projections are arithmetic, not generation

`_project_budgets()` computes month-end outcomes — spend so far ÷ fraction of
month elapsed — and *gives* them to the model, along with a `confidence` derived
from how much of the month has actually happened. "You are on track to finish at
$612" is precisely the kind of number a language model produces plausibly and
wrongly, so it is never asked to.

### New prompts (Feature 13)

Three constants in `persona.py`, where every generated word in the app lives:
`COPILOT_GROUNDING` (the null-means-cannot-see contract, the never-add-up-a-list
rule, and how to hedge on a low-confidence trend), `MONTHLY_REVIEW_FORMAT`
(Feature 1), and `BUDGET_COACH_FORMAT` (Feature 5).

---

## Statistical decisions worth knowing about

Three of these were found by tests failing against a first implementation that
looked reasonable. They are recorded because the wrong version is the intuitive
one.

### Theil–Sen, not least squares

`trends.py` estimates slope as the **median of pairwise slopes**, not by OLS.

Five months of $300 dining and one $1,200 birthday dinner fits an OLS line
rising $129/month at R² = 0.42 — over any sane weak-fit threshold, so it would
have been reported as *"your dining spending is rising."* No threshold tuning
fixes it: OLS minimises squared error, so the furthest point is by construction
the one steering the line. Theil–Sen returns zero on that series.

Cost is O(n²) — 276 pairs at the twenty-four-month ceiling.
`tests/test_trends.py::test_one_expensive_month_does_not_make_a_rising_trend`.

### Median absolute deviation, and what to do when it is zero

`anomalies.py` uses median-and-MAD rather than mean-and-standard-deviation,
because household spending is a pile of small charges with a long right tail: one
$6,000 annual insurance premium inflates a standard deviation enough to hide
every genuine outlier beneath it for the rest of the year.

The subtlety is that **the MAD is frequently zero**. A merchant charging the same
amount every visit — twelve $20 shops, eight identical rent payments — makes more
than half the absolute deviations exactly zero, and the median of those is zero.
The first implementation treated that as "no spread, skip the category", which
silenced the detector on precisely the histories it should fire on. It now falls
back to a multiple of the median (`POINT_MASS_MULTIPLE`), with the dollar floor
still applied.

### A subscription is a cadence *and* a fixed price

`bill_increases` originally called any monthly-cadence charge that rose a
"subscription hike". Somebody eating at the same restaurant once a month and
spending more each time matches that perfectly and is not subscribed to
anything. The prior charges must also be near-identical
(`FIXED_PRICE_TOLERANCE`), which is what separates *"Netflix went from $15.99 to
$22.99"* from *"your restaurant bills have been climbing"* — two true findings
that deserve different words.

---

## The health score

Six dimensions, weighted mean, weights renormalised over whatever was measurable.

| Dimension | Weight | Measured from | Full marks at |
|---|---|---|---|
| Savings rate | 35 | `(income − spending) / income` | ≥ 20% kept |
| Cash runway | 25 | cash ÷ typical monthly outgo | ≥ 6 months |
| Budget adherence | 25 | budgets within their monthly pace | all of them |
| Spending trend | 15 | this window's outgo vs the previous | ≥ 20% down |
| Cash-flow stability | 10 | std ÷ mean of monthly net flow | identical months |
| Debt burden | 10 | revolving balance ÷ monthly income | no balance |

Bands: **80+ strong**, **62+ steady**, **40+ needs attention**, else
**strained**.

The arithmetic lives in `dashboard_intel.health_score`, which the dashboard has
rendered since Phase 2. Phase 11A added the last two dimensions to that same
function as optional arguments defaulting to `None` — so **the dashboard's
number did not move**, asserted by
`tests/test_health.py::test_the_dashboards_four_factor_score_is_unchanged`.
`dough/services/health.py` supplies the full set for the copilot.

### What is not scored, and why

- **Investment consistency.** `PortfolioSnapshotRow` records what a portfolio was
  *worth* each day, not what was *paid into* it, and a rising balance in a rising
  market is indistinguishable from a contribution. Scoring it would mean
  inferring deposits from value changes. It is omitted rather than estimated.
- **Unknown debt.** A household with no credit account linked has *unknown* debt,
  not no debt. Scoring the unknown as debt-free would award the best rating to
  the least-visible situation, so the factor is dropped and named in
  `not_measured`.

### An inconsistency this surfaced

`FinancialAccount` carries `account_type == 'credit'`, so a revolving balance is
measurable — but `SyncRepository.compute_totals()`, and therefore **net worth
across the whole application, does not subtract it**. The debt factor is
consequently the only place in Dough where a card balance affects a headline
number.

That is a real gap in the product rather than in this module. It is written down
rather than quietly patched, because changing what net worth means is not a
change that belongs in an analytics feature. It wants a decision.

---

## Performance

Measured on a generated household of **~3,300 transactions over 3.5 years**
(2023-01 to 2026-08), SQLite, in-memory, best of three:

| Call | Time |
|---|---|
| `analytics.period_summary` | 3.5 ms |
| `trends.category_trends(6)` | 3.9 ms |
| `periods.compare` | 5.5 ms |
| `finsearch.search(...)` | 7.1 ms |
| `proactive.insights(findings=…)` | 14.3 ms |
| `health.score()` | 25.2 ms |
| `anomalies.detect()` | 32.7 ms |
| `ai_context.build()` | **191.4 ms** |
| `finance_context.build_finance_context(detail=True)` | 142.1 ms |

Token cost of the context sent to the model:

| Context | Tokens (est.) |
|---|---|
| `finance_context.build_finance_context(detail=True)` | 11,010 |
| `ai_context.build()` | **1,876** |
| | **17.0%** |

### The summarised context is slower to build, and that is fine

`ai_context.build()` costs **more** server time than the detailed context it
replaces — 191 ms against 142 ms — because it runs the whole analytics stack
(detection, scoring, trends, recurring, net worth) where the other one runs a
handful of `GROUP BY`s and dumps rows. That is worth stating plainly rather than
burying, because it is the opposite of what "summarised" suggests.

It is still the right trade. The 50 ms is paid once on the server; the ~9,100
tokens it saves are paid on every turn, in prompt-processing latency that is far
larger than 50 ms, and in money. End to end the summarised context is faster and
much cheaper.

### Caching — the seam already exists, so none was added

The obvious response to a 191 ms build is to cache it, and the cache is already
in the right place. `AIService.cached(surface, producer)` is household-scoped
(`dough/services/cache.py`), lives on `app.extensions` so two apps in one process
cannot share it, and — critically — takes a **producer** rather than a value, so
a cache hit skips building the context as well as the model call. The briefings
in `dough/api/v1/copilot.py` already assemble their context *inside* that
producer for exactly this reason.

So when a surface is wired to `ai_context.build()`, it lands inside an existing
household-scoped cache for free. Adding a second cache here would mean either a
module-level global — which would leak between the many apps the test suite
builds in one process, the precise bug `dough/services/cache.py` exists to
prevent — or a second `init_app` seam duplicating one that already works.

What *was* fixed is real duplicated work rather than uncached work: the Insights
hub originally ran `anomalies.detect()` three times per page (for the list, the
counts, and the insights), and `proactive` ran `periods.compare()` twice.
`insights()` and `summary()` now take an optional `findings` argument, and the
route detects once. `tests/test_insights_hub.py::test_the_hub_runs_the_anomaly_detector_only_once`
counts the calls.

The ratio is guarded by
`tests/test_ai_context.py::test_the_summarised_context_is_much_smaller_than_the_detailed_one`
at a loose 50% threshold, and the property behind it — that the context does not
grow with the ledger — by
`test_the_context_does_not_grow_with_the_ledger`.

### The index that was not added

`transactions` has `idx_transaction_unique(household_id, account_name, date,
description, amount)`. A date-range scan within a household can use the
`household_id` prefix but not seek on `date`, because `account_name` sits between
them. A dedicated `(household_id, date)` index would help every query in this
layer.

It was **not** added, because Phase 11A is explicitly a no-schema-change phase
and an `alembic` revision — even an additive `CREATE INDEX` — is a schema change.
At the measured volumes nothing needs it. It is the first thing to reach for if a
household with tens of thousands of transactions turns out to be slow, and it
belongs in 11B alongside the goals table.

---

## Testing

185 tests added across nine files, all green, with the existing suite unchanged.

```
tests/test_analytics.py       25   aggregation, windows, one-query guarantee, tenancy
tests/test_periods.py         17   thresholds, no-baseline handling, ordering
tests/test_trends.py          19   mostly refusals: too few months, outliers, noise
tests/test_anomalies.py       28   each detector, plus staying quiet on normal spending
tests/test_health.py          15   inputs, unmeasurable dimensions, no dashboard drift
tests/test_finsearch.py       21   the seven questions from the brief, literally
tests/test_ai_context.py      22   context shape, provenance, size, tenancy
tests/test_insights_hub.py    13   the consolidated page, and what it did not remove
tests/test_copilot.py         25   coordination, cache scoping, retrieval, projections
```

Most of the trend and anomaly tests assert a **negative** — that nothing is
reported. That is deliberate: a detector that fires on ordinary spending is worse
than no detector, because the user learns to dismiss the whole surface rather
than the one rule.

---

## UI — the Insights hub

`/insights` consolidates the health score, the proactive insights, the spending
trends, and a **collapsed** unusual-activity section into one page.

The primary nav went from seven links to six: "Anomalies" and "Recurring" were
narrow one-table pages that each held a permanent slot in a bar that already did
not fit tablets in portrait (see the comment above `#primary-nav` in
`base.html`). Both pages are **still served, still linked** from the hub and from
the touch menu, and their dismiss/restore endpoints are untouched — retiring a
nav entry is a reversible product decision, losing a workflow is not.

The collapsed section is a native `<details>`: it works without JavaScript, is
keyboard- and screen-reader-native, and survives the SPA navigation in
`base.html` without re-hydration. `/insights?open=unusual` opens it, so an
insight elsewhere can deep-link into the review list.

---

## Verification

| Check | Result |
|---|---|
| Full suite | **1787 passed, 1 skipped, 0 failed** (baseline before this work: 1593 passed, 1 skipped). Browser tests included in that run |
| Browser suite | Green — 233 passed, run separately five further times. See the note below |
| Household isolation | Asserted behaviourally in `test_analytics.py`, `test_finsearch.py`, `test_ai_context.py` — two households, deliberately different figures, read back under each scope |
| No other household's data in a prompt | `test_the_context_never_carries_another_households_figures` serialises a real context and asserts the other household's payee and amount are absent |
| Analytics derived from records | Every module is registered in `tests/test_services.py::SERVICE_FUNCTIONS`, which enforces the forbidden-import and dependency-block rules. No figure in this layer has a default or an estimate |
| API surface | Unchanged — no endpoints added, `docs/api/openapi.yaml` untouched |
| Schema | Unchanged — no migration, no model edit |
| Benchmark | Above, on ~3,300 transactions over 3.5 years |

**"No hallucinated figures" is structural here, not tested end-to-end**, and the
distinction matters. Every number in `ai_context.build()` is computed by a
service and carried with its evidence, and the provenance note tells the model
that `null` means "cannot see" rather than zero — but no AI surface consumes this
context yet, so there is no generated sentence to check a figure against. That
verification belongs to the pass that wires the surfaces up.

### One intermittent browser error

Across seven runs of the browser suite on this branch, two runs reported a single
`ERROR` in `tests/browser/test_chat.py` — once on
`test_a_chart_ships_the_numbers_behind_it`, once on
`test_the_composer_is_composer_sized_after_a_soft_navigation`. Five runs were
clean, including three consecutive. Both tests pass in isolation and both
deliberately provoke a stylesheet-load race through route interception; the
failure surfaces in the shared `PageHealth` fixture, not in an assertion of
either test.

A clean tree was also run and passed (225 tests, no error), so this is not
conclusively pre-existing. The likely mechanism is that the hub adds ~9
parameterised cases and lengthens the run, widening an existing timing window. It
is recorded here rather than dismissed, and it is worth a look if it recurs.

---

## What this does not do yet

- **`/chat` and `/api/v1/copilot` still use their original contexts.** The
  orchestrator is wired into the Insights hub only. Moving the existing AI
  surfaces onto `FinancialCopilot` changes what long-standing prompts see, which
  deserves its own pass and its own before/after comparison rather than being
  bundled here.
- **`brief()`, `monthly_review()` and `budget_coaching()` have no route yet.**
  They are tested and callable; nothing serves them. That is deliberate — adding
  endpoints means `docs/api/openapi.yaml` entries in the same commit, and the
  question of which surface each belongs on is a product decision.
- No API endpoints were added, so `docs/api/openapi.yaml` is unchanged.
- Investment intelligence (F8) and affordability (F9) are unbuilt.
- Goals, projections and long-term planning are Phase 11B.
