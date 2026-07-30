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
