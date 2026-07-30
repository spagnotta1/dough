# ADR-0010: Routes become blueprints, and the endpoint name is allowed to change

- **Status:** Accepted
- **Date:** 2026-07-27
- **Phase:** 7

## Context

`create_app` was 2,470 lines containing 51 route closures. Every phase since
Phase 3 has taken something out of it — query helpers, the model provider, the
tenancy rules, the membership rules — and every one of them left the routes
themselves behind, because moving a route is the change that can break a URL.

The line count was never the problem. The problem is that a function that long
has no boundaries inside it: there is no such thing as "the code that owns
budgets", so every change is a change to the same file, and two people working
on unrelated features edit the same 2,000 lines. The `tests/test_services.py`
line-count guard had already been raised once, in Phase 6, and the note left on
it then said the next raise would not be honest.

## Decision

### 1. Ten blueprints, grouped by what they are responsible for

`core`, `transactions`, `insights`, `budgets`, `rules`, `log`, `chat`,
`investments`, `auth`, `household`. The grouping is by ownership, not by size:
`log` is 100 lines and `chat` is 430, and splitting `chat` further would put the
streaming route in a different file from the non-streaming one that shares its
prompt assembly.

`insights` holds anomalies and recurring together because neither owns any data
— both are opinions about transactions — and a module each would have been two
files that always change for the same reason.

`models.py` was **not** split, and that is a deliberate refusal rather than an
oversight. Moving routes is mechanical and the URL map proves it; splitting a
declarative model file is not. Fourteen classes that reference each other by
string name in relationships, resolved at mapper-configure time, fail as an
import cycle that surfaces the first time a relationship is traversed — not at
import, and not in a test that touches one model. A 900-line file of
declarations that rarely change is not the bulk that made `app.py` hard to work
in.

### 2. What stays in `app.py` is the wiring, and the test says so

Configuration, the database, the `before_request` chain (authentication, CSRF,
tenancy), the error handlers, the template filters. `app.py` is 520 lines.

The request hooks specifically did *not* move onto the blueprints they came from.
A `before_request` registered on a blueprint runs only for that blueprint's
traffic, which is exactly backwards for a default-deny rule: the route that has
not been thought about is the one that needs the check, and by construction it
would be in some other blueprint. `_require_login` therefore stays application-
wide, and `dough/blueprints/auth.py` says so in its docstring so the next person
does not "tidy" it.

Ordering still matters and is now explicit: blueprints are registered after all
three hook groups, because Flask runs `before_request` handlers in registration
order and a view must not be reachable before authentication is in place.

### 3. Endpoint names change; the URL surface does not

`url_for('transactions')` is now `url_for('transactions.index')`. That is a
breaking change to 57 call sites and no change at all to any URL a browser has
ever seen.

`tests/test_url_map_snapshot.py` was written in Phase 0 for this exact moment and
pins the set of `(rule, methods)` pairs while deliberately **not** asserting
endpoint names. It passed unchanged through the extraction, which is the whole
evidence that no path or method moved. A test that had pinned endpoint names
would have failed 51 times here and proved nothing.

### 4. Per-app state goes on `app.extensions`, not module scope

The login throttle was a closure over `create_app`, which made it per-application
for free. A module-level instance in `dough/blueprints/auth.py` would not be: the
suite builds many applications in one process, and they would have shared a
counter, so a test that exhausted the limit would fail an unrelated test that ran
later. It is installed in `bp.record_once` and read through `current_app`.

This is the general shape for anything a closure used to hold, and `AIService`
already worked this way for the same reason.

## Consequences

**Good.** `app.py` went from 2,703 lines to 520, and the guard in
`tests/test_services.py` came *down* from 2,900 to 800 — the first time that
number has moved in the useful direction. Four new structural tests hold the
boundary: no blueprint may import `app`, each declares exactly one blueprint
called `bp`, every module in the package is actually registered, and `app.py`
defines no routes. Each of those catches a failure that is otherwise silent — a
module nobody registers serves 404s with no error anywhere.

`_md_to_html` moved to `dough/ai/formatting.py`, where it belongs: it renders
model output, and `app.py` had no other reason to carry a markdown parser.

**Bad, and worth stating.** The 57 `url_for` rewrites were done by script. The
snapshot test proves no *rule* changed but cannot prove a template link points
where it did before — a mistyped rename inside a Jinja expression is a
`BuildError` at render time, not at import. The mitigation is that every page was
rendered against a copy of the live database with authentication and CSRF on, in
the configuration the suite does not run in.

**Accepted risk.** `tests/test_ai_adapter.py` patched
`app.build_finance_context` to count calls; the target had to become
`dough.blueprints.chat.build_finance_context`, because the route now resolves the
name in its own module. Any other test that monkeypatches a name in `app.py` and
does not assert on the effect would now silently patch nothing. One was found and
fixed; the class of problem is why that test asserts a count rather than just
running.
