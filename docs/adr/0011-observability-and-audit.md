# ADR-0011 — Observability: an audit trail, structured logs, and health

- **Status:** Accepted
- **Date:** 2026-07-27
- **Phase:** 8
- **Supersedes:** nothing. Extends ADR-0008 (tenancy) with one documented exception.

## Context

Phases 0–7 made the application *correct*: tenancy is enforced by the ORM,
authentication and CSRF are boundaries rather than conventions, and the routes
live in blueprints. None of that helps when something goes wrong in production,
where the questions are different:

- Who did this, and when?
- Show me everything for the request the user is complaining about.
- Is this process healthy, and should traffic be sent to it?
- Why did the user see a stack trace?

Before this phase the answers were, in order: nothing recorded it; 34 free-text
log lines with no correlation; no endpoint; and because there was no error
handler at all. This ADR records four decisions taken to close that.

---

## Decision 1 — `audit_events.household_id` is nullable, and that is a
## deliberate exception to ADR-0008

Every tenant table in this application carries a **NOT NULL** `household_id`
and inherits `TenantScopedMixin`, which puts it behind the ORM read filter and
the write guard. `audit_events` does neither.

**Why.** The most important events an audit log records are the ones that
happen when nobody is signed in. A failed login has no household — there is no
tenant yet, and the username typed may not correspond to any account. The
options were:

| Option | Cost |
| --- | --- |
| Two tables (`household_events` + `security_events`) | Two schemas, two services, two read paths, and every cross-cutting question ("everything around 14:02") becomes a UNION. The split is on a field, not on a concept. |
| One table, `household_id` NOT NULL, attribute anonymous events to household 1 | Inventing an association that does not exist, in the one table meant to be the record of what actually happened. Household 1 is somebody's real data. |
| **One table, nullable `household_id`** ✅ | One documented exception to the tenancy invariant, and isolation that is enforced by a function rather than by the ORM. |

**What it costs, precisely.** `AuditEvent` is not a `TenantScopedMixin`, so the
ORM backstop that catches a forgotten filter everywhere else does not apply
here. The isolation is `dough/services/audit.py::recent()` — one function, which
filters on `require_household()` and never returns rows with a NULL household.
That is a boundary a reviewer can read in full; "remember to filter" spread
across call sites is not.

This is the second such exception. The first is
`household_invites.token_hash`, which is unique installation-wide because the
redemption lookup runs before any household is known. Both are recorded in
`tools/verify_tenancy.py` **positively** — as assertions that the exception is
still shaped the way it was approved — rather than by absence from a list, since
"absent from a list" and "somebody forgot" are indistinguishable.

## Decision 2 — Append-only is enforced by a `before_flush` hook, not a trigger

`dough/services/audit.py` registers a SQLAlchemy `before_flush` listener that
raises `AuditImmutableError` on any UPDATE or DELETE of an `AuditEvent`.

**Why not a database trigger,** which would be the stronger boundary: SQLite
triggers do not survive the `batch_alter_table` rebuilds this schema has already
needed twice, so the guarantee would be silently dropped by a future migration
— the worst possible failure mode for a control whose whole value is that it
cannot be bypassed.

**Why not documentation:** a documented-only append-only table is not a
property, it is a hope.

**The honest boundary,** stated here because it is asserted in
`tests/test_audit.py` rather than glossed:

- An operator with a SQL prompt can edit the table. True of any
  application-level control.
- `Query.delete()` emits DELETE directly and never populates `session.deleted`,
  so the hook does not see it. A test records this explicitly rather than
  leaving a reader to assume otherwise.

What the guard does guarantee is that the *application* cannot rewrite its own
history, including by accident — which is the failure that actually happens.

## Decision 3 — Recording must never fail the operation it describes

`record()` returns `None` on failure and logs. It does not raise, ever, except
for one case: an unknown event type, which is a programming error caught on the
first test run.

The reasoning is that a member removal which succeeded and *then* raised
because the audit insert hit a constraint leaves the caller with a traceback
for an operation that actually happened. That is worse than a missing audit
row and much harder to reason about afterwards.

The guard covers the whole body, not just the write. Resolving the context can
fail on its own — `current_household()` needs an application context, and
`dough/ai/service.py` calls `record()` from code that unit tests exercise with
no Flask app at all. An audit helper that raises while working out *who* to
attribute an event to has failed in exactly the way it promises it cannot.

## Decision 4 — The trace id is a `ContextVar`, not `flask.g`

Same reasoning as `dough/tenancy.py`, and it is the reason worth writing down:
**a new thread starts with an empty ContextVar.** The sync engine and the
scheduler run on worker threads. With a thread-local or a global they would
inherit whatever the last request left behind and file their log lines under a
request they have nothing to do with — confidently wrong, which is worse than
uncorrelated. Background work calls `bind_trace(kind='sync')` and gets its own
id under the same field name, so one filter finds either.

Two supporting choices:

- **Context is attached by a `logging.Filter`, not by call sites.** All 34
  existing `logger.warning(...)` calls gained household, user, path and trace id
  without being touched. Asking every call site to remember is how logs end up
  with the context on the lines that did not need it.
- **Redaction runs at the formatter.** A log line is written by whoever is
  nearest the failure, often in a hurry, and `logger.info('token=%s', tok)` is
  a completely reasonable thing to type. The formatter is the last place that
  can still say no. It is a second implementation of the same idea as
  `audit.redact()`, kept deliberately separate so the two can diverge without
  one silently loosening the other.

## Decision 5 — Liveness and readiness are different endpoints

`/health/live` touches nothing. `/health/ready` checks the database, the
migration head and the required configuration, and returns 503 when any fails.

A liveness probe that also checked the database would restart a perfectly
healthy application every time the database blinked — taking down the thing
that was still serving cached pages in order to punish it for a dependency's
outage.

Both are `@public`, which is deliberate: a health check behind a login is not a
health check, and a readiness endpoint that returns 302-to-login tells a load
balancer everything is fine when it is not. That makes the response body an
**unauthenticated disclosure surface**, so it carries check names and booleans
and nothing else — no revision identifiers, no configuration keys, no versions,
no error text. An operator who needs detail has the log line, which carries the
same trace id. `tests/test_observability.py` asserts the body shape for exactly
this reason.

---

## Consequences

**Good.**

- Every security-relevant event has a row: who, when, from where, and against
  what. It survives the deletion of the thing it describes.
- Any failure a user reports can be found by one string, which the error page
  tells them to quote.
- `tools/verify_tenancy.py` grew from 60 to 133 invariants over the tenancy
  work and now asserts the audit exception positively.

**Costs, stated.**

- One table is outside the ORM tenancy backstop. Its isolation is one function.
- `audit_events` grows without bound and has no retention policy — OPS-0013.
- `dough/ai/service.py` now imports `dough.services.audit`, which is the one
  dependency in that module pointing at the domain rather than away from it.
  The alternative was an `audit.record` call in each of the eight AI surfaces —
  the same duplication that module exists to prevent, on the one path where
  forgetting means an AI request nothing recorded. The contract block in that
  file names the exception and the reason.
- Two redaction implementations that must both be maintained.

**Not done here, deliberately.** `init_scheduler` ignoring its argument
(OPS-0015) and the process-wide sync mutex (OPS-0016) were both found during
this phase's review and both belong to background-execution ownership, which is
Phase 9. Fixing them inside an observability phase would have meant changing
how work is scheduled while also changing how it is observed — and then having
no reliable way to tell which change caused what.
