# `dough/services/` — domain services

These modules hold the logic that used to live in closures inside
`create_app()`. Extracting them was a **pure move**: identical signatures,
identical SQL, identical return values, identical logging. Any behaviour change
found here is a bug, not an improvement.

## Why this directory exists

Before Phase 3 every helper was nested inside `create_app()`, closing over
`app` and `category_rules`. That had three costs:

- **Nothing could be imported.** A test, a CLI command, or the sync scheduler
  could not reach `_compute_net_worth` without building an app.
- **Every helper could reach everything.** A net-worth calculation had the
  request object, the template environment and the Anthropic client in scope,
  so nothing stopped it from using them, and nothing recorded that it didn't.
- **`app.py` was 3,161 lines**, and blueprint extraction (Phase 9) cannot start
  from that.

Making the dependencies explicit is the point of the move. A module that lists
what it may not import is a module whose coupling you can check mechanically.

## Dependency rules

Every module in this directory declares an `Allowed` / `Must not` block in its
docstring. The rules are the same for all of them:

**Allowed**

- `models` (the `db` handle and the model classes)
- SQLAlchemy — `func`, `and_`, `or_`, exceptions
- The framework-free domain modules: `recurring`, `rules`, `dashboard_intel`,
  `investments_intel`
- The standard library, `pandas`, `numpy`, `scikit-learn`
- Other modules in `dough/services/`, provided the import graph stays acyclic

**Must not**

- `app` — anything importing `app` reintroduces the cycle the move removed
- `flask.render_template`, `url_for`, `redirect`, `flash`, `jsonify` — a service
  returns data; turning data into a response is the route's job
- `anthropic`, or any LLM client — that belongs to `dough/ai/` (Phase 4)
- Blueprints, `route` decorators, `before_request`

**`request` and `session`** are the one deliberate exception, and only for
functions whose entire job is to read the request. Those are marked
`REQUEST-BOUND` in the module docstring, kept in a clearly separated section,
and never called by anything else in this directory. Everything else must be
callable from the sync scheduler thread, where there is no request at all.

## Modules

| Module | Contents | Flask context needed | Notes |
|---|---|---|---|
| `transactions.py` | `compute_anomaly_scores`, `build_transaction_query`, `sticky_filter` | app; `request`+`session` for `sticky_filter` | `sticky_filter` is REQUEST-BOUND and kept below its own separator |
| `recurring_service.py` | `dismissed_recurring_keys`, `detect_recurring_summary`, `detect_recurring_full` | app; `g` for `detect_recurring_full` | Named `recurring_service` so it cannot shadow the top-level `recurring` module |
| `networth.py` | `compute_net_worth`, `portfolio_snapshot`, `monthly_outgo`, `snapshot_history`, `wealth_snapshot` | app only | `wealth_snapshot` is the single derivation the Investments page, both copilot endpoints and the tests all read |
| `finance_context.py` | `build_finance_context`, `copilot_context`, `wealth_context`, `months_ago` | app; `g` transitively | Owns the `CHAT_RECENT_TXN_LIMIT` / `CHAT_TOP_MERCHANT_LIMIT` / `CHAT_ANOMALY_LIMIT` / `CHAT_TREND_MONTHS` sizing constants |
| `categorization.py` | `get_category_rules`, `reset_category_rules` | none | Process-wide cache. `finance_sync/repository.py` deliberately does **not** use it |
| `transfers.py` | `find_transfer_pairs`, `net_out_transfers`, `looks_like_transfer` | app only | Pairs a debit against an equal credit in **another** account and labels both `Transfer`. Runs *after* the rule pass, never instead of it: no description can prove money moved between two accounts the household owns, and every income total in the app already excluded the category nothing was writing |
| `identity.py` | `register_account`, `set_password`, `set_email`, `revoke_all_credentials`, `issue_token`, `redeem`, the validators | app only | **[Phase 10.5]** The account lifecycle, once, for the three surfaces that create or change one. Deliberately does *not* bump `session_version` — `dough/auth.py`'s `before_flush` listener owns that, so a caller who has never heard of this module still invalidates credentials |
| `email.py` | `build_backend`, `current_email`, the three backends | `current_app` in `current_email` only | **[Phase 10.5]** `console` / `memory` / `smtp` behind one `send()`. `ConsoleBackend` writes to **stdout, never `logging`** — the payload is a live credential and `_SECRETISH` cannot recognise it (SEC-0024) |
| `ratelimit.py` | `policy_for`, `build_backend`, `current_limiter` | `current_app` in `current_limiter` only | **[Phase 10.5]** SEC-0018's seam. `POLICIES` is the whole limit table in one place; four of the eight have call sites, the rest await a shared backend |

### Phase 11A — the analytics layer

Eight modules, added together because they are one layer: `analytics` holds the
queries and the other seven read their numbers from it rather than issuing their
own SQL. That is what makes "does the trend agree with the dashboard?" a
question with a single answer instead of a diff.

| Module | Contents | Flask context needed | Notes |
|---|---|---|---|
| `analytics.py` | `resolve_window`, `preceding_window`, `custom_window`, `lookback_window`, `period_summary`, `category_totals`, `merchant_totals`, `largest_purchases`, `monthly_series`, `monthly_category_series`, `coverage`, `pct_change`, `base_query` | app only | The aggregation primitives. Every rollup is a `GROUP BY`; `period_summary` is **one query**, asserted as one in `tests/test_analytics.py`. Owns the transfer rule in code, which `finance_context` states in prose to the model |
| `periods.py` | `compare`, `compare_kind`, `compare_ranges` | app only | Feature 2. Structured findings, never prose. A change must clear a percentage **and** a dollar floor — both imported from `dashboard_intel` so the attention centre cannot disagree about what "material" means. `pct: None` for a category with no baseline |
| `trends.py` | `category_trends`, `merchant_trends`, `unit_cost_trend` | app only | Feature 3. **Theil–Sen, not least squares** — OLS reported one birthday dinner as a rising trend at R²=0.42; see `_describe`. Leading zeros are trimmed so a category first seen last month cannot be fitted through four invented ones |
| `anomalies.py` | `detect`, `summary`, `open_flagged`, and the five detectors | app only | Feature 6. Median-and-MAD throughout: household spending has a long right tail and one annual premium hides every later outlier from a standard deviation. Falls back to a multiple-of-median when the MAD is zero, which is the *common* case for a fixed-price merchant. Writes nothing |
| `health.py` | `score`, `cash_flow_stability`, `debt_burden`, `improvements` | app only | Feature 4. Gathers inputs and hands them to `dashboard_intel.health_score` — deliberately **not** a second scorer. The methodology table is in the module docstring, including why investment consistency is not scored |
| `finsearch.py` | `search`, `parse` | app only | Feature 7. Parse → query → answer, so the model chooses the question and never does the arithmetic. Matches categories against the household's *real* category names, not a fixed taxonomy. `matched: False` is a valid result |
| `proactive.py` | `insights`, `digest` | app only | Feature 11. The only module that decides what is worth saying unprompted, so it is a scoring function and a hard cap. An empty list is the common case and is correct |
| `ai_context.py` | `build`, `estimated_tokens` | app only | Feature 12. Conclusions plus their figures, not the rows behind them — ~17% of `finance_context`'s detailed context on 3,300 transactions, and it does not grow with the ledger |

Every function that reads a window takes an optional `anchor`, defaulting to
today — `category_trends`, `merchant_trends`, `unit_cost_trend`,
`anomalies.detect`, `health.score`, `proactive.insights`, `ai_context.build`,
`FinancialCopilot`. That is what lets a test pin the clock through the public
signature instead of patching one, and it is why none of these files
monkeypatch `date.today`.

The import graph stays a line: `ai_context` → {`proactive`, `health`, `periods`,
`trends`, `anomalies`, `budgets`, `networth`} → `analytics` → `models`. Nothing
imports upward, and `analytics` imports no sibling at all.

**Nothing here talks to a model.** The orchestrator that coordinates these and
then calls one is `dough/ai/copilot.py`, and it lives there rather than here
precisely because of the "no LLM client" rule above — `dough/ai/` is the only
package allowed to reach in both directions. Routes should generally call
`FinancialCopilot` rather than these modules directly: several of the expensive
calls are needed by more than one surface, and the orchestrator is what makes
them run once. `dough/blueprints/insights.py` is the worked example.

The functions that accept an optional precomputed `findings` or `comparison`
(`proactive.insights`, `anomalies.summary`, `ai_context.build`) exist for that
coordination. Left as None they compute their own, so a direct caller needs to
know nothing about it.

**None of these take a household argument**, which is deliberate: there is no
parameter a caller could pass the wrong value to. Scoping comes from
`TenantScopedQuery` exactly as it does everywhere else, and
`test_analytics.py`, `test_finsearch.py` and `test_ai_context.py` each assert it
behaviourally from two households with different figures.

The last three of the earlier modules are per-application services rather than
plain function modules:
each has an `init_app` and lives on `app.extensions`, exactly as `AIService`
does, so the suite's many `create_app()` calls cannot share one instance's mail,
counters or budget.

The import graph is a line, not a web:
`finance_context` → {`networth`, `recurring_service`} → `models`. Nothing imports
`transactions` or `categorization`, and nothing imports upward.

`tests/test_services.py` asserts all of this structurally — the function
inventory, the forbidden imports, the presence of these dependency blocks, that
the services import in a bare interpreter with no Flask app, and that
`finance_sync` still builds its own `CategoryRules`.

## Conventions for future additions

1. **No leading underscore on the public functions.** They were private because
   they were closures; the module name is the namespace now.
2. **App context, not app object.** These functions run inside an app context
   (`Model.query` and `db.session` need one) but never receive `app` and never
   read `current_app.config`. A service that needs configuration takes it as an
   argument, so it is testable without an app and honest about what it depends
   on.
3. **Commit where the caller expects it.** Functions that wrote to the database
   before the move still write, and still commit at the same point. Moving a
   commit boundary is a behaviour change even when the final rows match.
4. **Add the dependency block before the code.** It is a decision, not
   documentation of one.
5. **When Phase 5 lands tenancy**, the queries here inherit household scoping
   from the `do_orm_execute` event without editing this directory — but per the
   tenancy constraint, that filter is defence-in-depth. Any function here that
   resolves a caller-supplied id must also carry an explicit `household_id`
   predicate.
