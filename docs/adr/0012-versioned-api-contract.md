# ADR-0012 — A versioned API, and one service layer under both clients

- **Status:** Accepted
- **Date:** 2026-07-30
- **Phase:** 10
- **Extends:** ADR-0009 (authentication) with a second credential type;
  ADR-0010 (routes as blueprints) with a parallel package for resources.
- **Supersedes:** nothing. The unversioned endpoints keep working.

## Context

Every phase before this one served exactly one client: a set of Jinja templates
written alongside the routes they call. That client could absorb any amount of
inconsistency, because the same person wrote both ends in the same afternoon and
shipped them together.

The inconsistency is real and worth writing down, because it is the evidence for
this phase rather than an aesthetic complaint:

| Endpoint | Success shape | Failure shape |
| --- | --- | --- |
| `/api/holdings` | bare object | `{"error": "…"}`, 400 |
| `/api/log/entries` | bare array | `{"error": "…"}`, 400 |
| `/update_category` | `{"success": true}` | `{"success": false, "error": …}`, **200** |
| `/api/copilot/brief` | `{...}` | `{"available": false}`, 200 |
| `/api/chat_stream` | SSE | SSE `error` event, or JSON, or 503 |

Each is defensible alone. Together they mean a client cannot write one function
that answers "did this work, and if not, why" — it needs a branch per endpoint,
and every endpoint added later is another branch.

Pagination was in the same state: `/transactions` reads `page` and `per_page`
and silently rewrites `per_page` unless it is one of four values it happens to
like; `/api/log/entries` returns everything unpaged forever; `/api/conversations`
returns everything ordered by a column the caller cannot change.

None of this mattered until a second client existed. A native client ships on a
different schedule, cannot be updated in lockstep with the server, and turns
every inconsistency into a permanent branch in code nobody can reach.

---

## Decision 1 — A versioned namespace, additive, with the old endpoints intact

`/api/v1` is a new surface. The 49 endpoints under it are a contract. The
unversioned endpoints are unchanged and still serve the web UI.

**Why additive.** The alternative — moving the existing endpoints under `/api/v1`
and updating the templates — is a larger diff, a bigger regression surface, and
it makes this phase break the thing it exists to stabilise. `docs/api/README.md`
records which unversioned endpoint each v1 route supersedes, so the migration is
documented rather than forced.

**Why the version is in the path** and not a header or a query parameter. A path
version is visible in a log, in a bookmark, in a `curl` somebody pastes into an
issue, and it is what makes `tests/test_url_map_snapshot.py` able to freeze the
surface. A header version is invisible in all of those and gets omitted by the
one client that most needed to send it.

**What v1 does not include.** `/api/sync/*`, `/api/connections*` and the Plaid
endpoints, because connecting an institution is an OAuth flow with a browser
redirect in the middle and a JSON API cannot usefully wrap it.
`/api/v1/accounts/connections` reports state read-only.
`/api/log/entries*` (the manual check register) is a surface the product itself
has retired; versioning it would be promising to keep it.

### Cost

The URL surface roughly doubled, and every one of those 49 rules is a public
contract from the moment it exists.
`tests/test_url_map_snapshot.py` and `tests/test_openapi.py` are what keep that
from being an accident, and both had to be updated in the same commit.

---

## Decision 2 — Opaque bearer tokens, not JWT

A new `api_tokens` table. 256 bits from `secrets.token_urlsafe`, prefixed
`dgh_`, stored as an unsalted SHA-256, presented as
`Authorization: Bearer dgh_…`.

| Option | Why not |
| --- | --- |
| **Session cookie only** | A native client would have to hold a cookie jar and scrape a CSRF token out of an HTML page. That is a browser emulator, not an API client. Worse, the session has no revocation short of rotating `SECRET_KEY`, so "the phone was stolen" would sign the family's desktop out too. |
| **JWT access + refresh** | Adds PyJWT. Statelessness is the selling point and it is the part that does not survive contact with the requirement: revoking a leaked token needs a denylist table, so the database round trip comes back *and* the token stays valid until it expires. A refresh-token rotation scheme is then a second credential lifecycle to get right. |
| **Opaque tokens** ✔ | One index lookup per request against a table this application already knows how to scope. Immediate revocation. Per-token scopes. No new dependency. |

**Unsalted SHA-256, no work factor** — the opposite of the advice for passwords,
and right for the same reason it is wrong there: the input is 256 bits nobody
chose, so there is no dictionary to precompute and nothing for a slow KDF to
protect against. A work factor would tax every authenticated request, which is
the one place in this application where a per-request cost is genuinely paid per
request. Identical reasoning to `HouseholdInvite.token_hash`.

**The user is re-read on every request** rather than trusted from the token row.
Three guarantees fall out of that with no extra machinery: a removed member's
tokens stop working, a demoted owner's tokens lose owner powers, and a token
whose user was deleted fails closed instead of authenticating as a dangling
foreign key.

**`last_used_at` is written at minute resolution.** Writing per request would
make every authenticated read take SQLite's single writer lock, turning the
busiest path in the application into the one that serializes against itself. The
column answers "is this credential still in use?", which does not need seconds.

### Cost, stated plainly

Every authenticated API request is one indexed SELECT plus, at most once a
minute per token, one UPDATE. A stateless JWT would have been zero. That is the
price of revocation and it is the right trade for an application fronting bank
data — but it is a real per-request cost and it is on the hot path.

---

## Decision 3 — `api_tokens` is not `TenantScopedMixin`

It carries a plain indexed `household_id` foreign key, like `app_users`.

**Why it has to be.** The lookup runs *before* any household is bound — the
token is what says which household the request is for. Routing this table
through the ORM read filter would make authenticating impossible in exactly the
way scoping `app_users.username` would make signing in impossible.

**What replaces the guarantee.** `dough/services/api_tokens.py` is the only
module that reads the table, and every query in it states the household
predicate explicitly. `tools/verify_tenancy.py` lists the exception with that
reasoning attached, so it is reviewed rather than assumed.

This is the third such exception, after `household_invites.token_hash` and
`audit_events.household_id`. Three is enough that the pattern should be named:
**a table whose lookup decides the tenant cannot itself be tenant-scoped**, and
the price is always the same — one module owns every read, and a script checks
that the exception is written down.

---

## Decision 4 — CSRF is waived for the credential, never for the path

`app.py`'s `_verify_csrf` skips the check when `api.bearer_actor()` is set. It
does **not** skip on `request.path.startswith('/api/v1')`.

The distinction is the whole of the security argument. CSRF exists because a
browser attaches cookies to cross-site requests *automatically* — the credential
travels without the attacking page having to know it. An `Authorization` header
is never attached automatically, and a cross-origin page cannot set one without
a CORS preflight this application never grants. So a request carrying a valid
token is, by construction, one whose sender knew the credential, and there is
nothing left for a token to prove.

A *path* exemption would have exempted the same routes when a browser reaches
them with a session cookie — and the web UI does call `/api/v1` with a cookie.
That would reopen SEC-0002 on the newest routes in the application.

`tests/test_api_auth.py::test_session_authenticated_api_calls_still_need_a_token`
pins it from the side that would fail if somebody took the shortcut.

### The one `@csrf_exempt`

`api_v1_auth.login`, and it is the first in the application's history —
`tests/test_csrf.py` pinned the exempt set at zero from Phase 6 to Phase 9 so
that spending it would be deliberate.

It qualifies because it *cannot* participate: there is no session yet to bind a
token to, since obtaining a credential is what the endpoint is for. What makes
it safe is narrower than "it is an API endpoint" — it accepts a **password**,
which no browser attaches automatically, so a cross-site forgery would have to
already know the password, at which point the forgery buys nothing.

---

## Decision 5 — No business logic in a resource module

The rule: `dough/api/v1/*` reads the request, calls `dough/services/*`, and
shapes a response. The same services `dough/blueprints/*` calls.

This is the decision the phase actually turns on. An API that reimplemented
"am I over budget" or "may this holding be edited" would be a second answer to a
question about a household's money, and the two would diverge the first time
either changed — silently, because both would keep returning numbers.

Four services were extracted from the web blueprints to make it true, and the
web blueprints were repointed onto them in the same commit:

| Service | Extracted from | Now called by |
| --- | --- | --- |
| `ledger` | `blueprints/transactions.py` | the page and `/api/v1/transactions` |
| `budgets` | `blueprints/budgets.py` | the page and `/api/v1/budgets` |
| `holdings` | `blueprints/investments.py` | the page and `/api/v1/investments` |
| `accounts` | `blueprints/log.py` + `investments.py` | the pages and `/api/v1/accounts` |

Enforced by `tests/test_services.py::test_api_resource_holds_no_business_logic`,
which rejects any `db.session.add/delete/commit` in a resource module.
`dough/api/v1/chat.py` is the one exception and it is named rather than
pattern-matched: persisting a message mid-stream has to happen inside the
generator, after `teardown_request` has released the tenant scope, so it cannot
be delegated to a service that would run in the wrong frame.

### What the extraction cost, and what it found

The refactor introduced one regression and surfaced one latent bug, both caught
by tests that already existed. Both are worth recording because they are the
argument for having written those tests:

1. **A swallowed 404.** Moving `get_owned` inside a `try/except Exception` in
   the transaction routes turned "another household's row" into a 200 with
   `{"success": false}` — an authorization failure demoted to a message in a
   field nothing checks. Caught by
   `test_routes_deny_foreign_ids_without_the_orm_backstop`, which is the test
   ADR-0008 calls the load-bearing half. The three routes now re-raise
   `HTTPException` explicitly, with the reasoning at the call site.

2. **A date comparison off by one day, in two places.** A `Date` column
   compared against a `datetime` makes SQLAlchemy bind
   `'2026-07-02 00:00:00.000000'`, which SQLite compares as a *string* against
   the stored `'2026-07-02'`. The stored value is shorter and sorts first, so
   the window's first day was excluded.

   - `build_transaction_query` — every filtered transaction list and every CSV
     export silently dropped its first day. Found because the API asked for a
     two-day window and got one day back.
   - `budgets.spend_by_category` — **the 1st of every month was missing from
     every budget's spend**. A budget genuinely at 135% of its limit reported
     90% and rendered as `warn` rather than `danger`. Found by rendering the
     budgets page with a transaction deliberately dated the 1st.

   Both pre-existing and both invisible: nothing failed, the numbers were
   simply wrong, and the second was wrong in the direction that reassures.
   Fixed in the shared functions, so the pages inherit the fix along with the
   API. Regression tests assert the boundary is inclusive on both.

   This is the strongest argument the phase produced for the extraction. The
   budget bug had been shipping since budgets existed; it surfaced only because
   the same class of defect had just been understood two files away, and both
   are now one function each rather than two.

---

## Decision 6 — The specification is hand-written

`docs/api/openapi.yaml` is written by hand. `tests/test_openapi.py` asserts that
every `(path, method)` the application serves under `/api/v1` appears in it, and
that every documented one is served.

**Why not generate it.** A generated spec always agrees with the code and can
therefore never disagree with it — which sounds like the goal and is the
opposite of one. The value of a specification is that it states what was
*intended*, so a route drifting from it produces a conflict somebody resolves. A
spec derived from the implementation cannot be wrong about the implementation,
and so can never catch it changing.

The drift test supplies the half a generator would have given, without the half
it would have taken away. It also checks the error-code enum against
`ErrorCode`'s attributes and the page-size limits against `pagination.py`, so
the three things a client actually hardcodes cannot drift.

---

## Consequences

**Good.**

- One envelope, one error vocabulary, one pagination convention across 49
  endpoints. A client writes one response handler and one error handler.
- The web UI and any future client share a service layer, so they cannot answer
  differently.
- Credentials are revocable per device, scoped, audited on issue, revocation and
  rejection, and never stored in recoverable form.
- The extraction found two real defects in code that had been shipping.

**Bad, or at least owed.**

- The URL surface roughly doubled, and every rule is now a public promise.
- One indexed SELECT per authenticated API request, plus a coarse UPDATE. A
  stateless credential would have been free.
- Two constants (`CHAT_HISTORY_LIMIT`, `CHAT_MAX_TOKENS`) are duplicated between
  `dough/api/v1/chat.py` and `dough/blueprints/chat.py`, because a blueprint may
  not import another blueprint. Pinned by a test; still duplication.
- `/api/v1` has no rate limiting of its own beyond the login throttle. A token
  holder can call `/api/v1/transactions` as fast as the process will serve it.
  Recorded as an open finding rather than solved here — the existing throttle is
  in-memory and per-process (SEC-0010), and fixing that properly needs a shared
  store this application does not otherwise have.
- The AI endpoints under `/api/v1/copilot` spend API credits and are reachable
  by any `read`-scoped token. Cost controls are Phase 16 in the roadmap.
