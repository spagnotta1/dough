# ADR-0008: The household is the unit of isolation, and the ORM is not the authority

- **Status:** Accepted
- **Date:** 2026-07-26
- **Phase:** 5

## Context

Every row in this database belonged to whoever was logged in, because there was
only ever one of them. Nothing recorded whose money a transaction described, and
six unique constraints were global when what they actually meant was "unique per
family": two households could not both link Chase, both dismiss `NETFLIX`, both
record a manual checking balance, or both import the same `$15.99` statement
line — and the last of those would have looked like the CSV dedupe feature
working correctly rather than like data loss.

Authentication (Phase 6) answers *who are you*. This phase answers *which rows
may you see*, which is a different question and a larger one: authentication
touches a handful of routes, and tenancy touches every query in the application.

Three constraints shaped the design, in this order of importance.

**1. Failing closed has to be louder than failing open.** The obvious failure is
"no tenant context means all rows", which is a disclosure. The non-obvious one is
"no tenant context means no rows", which is worse in practice: a broken tenant
context would be indistinguishable from an empty account, so it would be
reported as missing data, investigated as a data bug, and never recognised as a
security failure at all.

**2. ORM-level filtering is defense in depth, never the authorization
mechanism.** This was written down as an operating constraint before any code
existed, and `tests/test_tenancy_boundary.py` was written in Phase 0 to enforce
it — deliberately, because if the isolation tests had been written alongside the
implementation, every one of them would have passed on the strength of the ORM
event and nobody would ever have found out that the route layer checked nothing.

**3. Not every entry point is a request.** The sync scheduler's daemon thread,
its startup-due poll, Flask CLI commands and Alembic all reach the database with
no request in scope. A tenancy mechanism that only works inside a request is a
tenancy mechanism with four holes in it.

## Decision

**A `household` owns every row that describes somebody's money, and the
application refuses to touch such a row without knowing which household it is
acting as.**

### 1. `ContextVar`, not `flask.g`

`flask.g` is bound to an application context, so anything reading it outside a
request either crashes or silently sees nothing. `ContextVar` works in requests,
CLI commands, background threads, scheduled jobs and any future async code.

It also has a property that matters more than portability: **a new thread starts
with an empty context.** `threading.Thread` does not inherit the parent's. A
worker spawned by a request therefore cannot keep serving whichever household
that request happened to belong to — it gets no household at all, and constraint
1 turns that into an exception. `finance_sync/scheduler.py` re-binds one
deliberately: from the calling request for on-demand syncs, and by iterating
households for scheduled ones.

### 2. Two independent halves, and the second is the load-bearing one

- **The backstop.** A `do_orm_execute` handler adds `household_id = <current>`
  to every statement touching a tenant-scoped entity; a `before_flush` handler
  stamps inserts and refuses cross-tenant writes.
- **Explicit authorization.** Every route that resolves a caller-supplied id
  goes through `get_owned` / `find_owned`, which run their own household
  predicate *inside `unscoped()`* — with the backstop switched off — so a route
  cannot pass on the strength of the safety net.

`test_routes_deny_foreign_ids_without_the_orm_backstop` runs each such route
against another household's row with the backstop bypassed. A route that only
passes with it installed is relying on defense in depth as its primary defense,
which is the exact thing constraint 2 forbids.

### 3. `unscoped()` relaxes reads only

This is the correction the implementation forced, and it is worth recording
because the bug is invisible on inspection.

`unscoped()` initially disabled the write guard as well, on the theory that it
meant "the caller has taken responsibility". But **SQLAlchemy autoflushes before
every query.** So a pending insert followed by any read inside an `unscoped()`
block — concretely, `db.session.add(msg)` then `find_owned(Conversation, id)` in
the chat-stream route — flushed that insert with the guard off, the row was never
stamped, and it reached the database with a NULL `household_id`. Precisely the
state `nullable=False` exists to make impossible.

Nothing needs writes unscoped. Reads are the part that occasionally has to span
households; a write that genuinely targets another household is
`tenant_scope(that_household)`, said out loud.

### 4. `Query.count()` needed its own fix

`with_loader_criteria` does not reach it. `count()` goes through
`_legacy_from_self`: it freezes the query into a subquery and runs
`SELECT count(*) FROM (<frozen>) AS anon_1`, so by the time `do_orm_execute`
fires the only entity left is the count column, `all_mappers` comes back
**empty**, and the handler concludes no tenant data is involved.

The result was that `Transaction.query.filter_by(...).all()` returned one
household's rows while `.count()` on the same query counted everybody's.
`models.TenantScopedQuery` applies the predicate itself, before the subquery is
frozen.

This was found by `test_both_households_can_import_the_identical_csv_row` — the
only test that had two households holding a row matching the same filter. Every
other test in the suite would have counted the right number by accident. It is
now asserted directly.

### 5. Thirteen scoped tables, one identity table, one global table

`AppUser` carries a plain `household_id` rather than the mixin, because login has
to find a user *before* a household is known; filtering it through the backstop
would make signing in impossible. `MarketPrice` is not scoped at all — the
closing price of VTI is not private to anyone, one household's sync warms the
cache for all, and scoping it would store an identical row per tenant.

Membership is a foreign key, not a join table. A join table models a user in
several households, which is a product decision nobody has made; adding it later
is a migration, whereas removing an unused one that authorization code has
already learned to consult is not.

### 6. `tools/verify_tenancy.py` runs against the data, not the code

`tests/test_tenancy_boundary.py` proves the code isolates households. The
verifier proves the *data* is in a state where that isolation means something.
They fail in different ways and neither substitutes for the other: a perfect ORM
filter over rows with a NULL household serves nothing to nobody, and perfectly
partitioned data behind a route that forgot its check still leaks.

Its checks are SQL against the file rather than ORM queries, on purpose. Asking
the ORM whether the data is correctly partitioned means asking the tenant filter
whether the tenant filter works, and it will always say yes.

## Consequences

**Good.** SEC-0003 is closed: AI cache keys are namespaced by household, so the
generated paragraph about one family's spending is unreachable from another's
dashboard. The six household-blind unique constraints are gone. Adding a model
to the tenancy boundary is inheriting one mixin, and `tools/verify_tenancy.py`
derives its sweep from the mapper registry so a new model is covered without
anyone editing a list.

**Bad.** Every entry point now has to bind a household, and forgetting one
produces an exception rather than a subtly wrong page — which is the intended
trade, but it does mean the failure surfaces at runtime rather than in review.
`grep -rn 'unscoped()'` is the audit of everywhere the net is off, and that list
has to stay short.

The test suite needed an ambient default household, which is a real cost: the
~180 tests that predate tenancy run inside one scope, so they cannot prove
isolation. `tests/test_tenancy_boundary.py` therefore builds its own app with no
ambient household — with one bound, every assertion in that file would pass for
the wrong reason.

**Accepted risk.** The backstop depends on SQLAlchemy internals that have no
stability guarantee: `all_mappers`, lambda-closure rebinding in
`with_loader_criteria`, and the `_legacy_from_self` behaviour of `count()`. A
SQLAlchemy upgrade is now a change that must re-run
`tests/test_tenancy_boundary.py` and read its failures carefully, because two of
the three ways this can break are silent. The pin in `requirements.txt` is
load-bearing for more than reproducibility.
