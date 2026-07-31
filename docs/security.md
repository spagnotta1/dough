# Security

Living document. The threat model, data-at-rest inventory and OWASP alignment
review land in Phase 10; this file starts now because findings are being turned
up during the phased work and they need somewhere to go the day they are found,
not the day the documentation phase begins.

## Findings log

Findings are recorded whether or not they are fixed, and stay in the log after
they are closed. A closed finding with its detection method written down is what
stops the same class of bug returning.

---

### SEC-0001 — Session cookie shipped with no SameSite attribute

- **Severity:** High
- **Status:** Fixed — Phase 0 addendum, 2026-07-26
- **Found by:** `tests/test_route_guard.py::test_session_cookie_samesite_is_lax`,
  written as a forward spec in Phase 0 and expected to xfail. It failed for a
  reason that was not the expected one, which is how the defect surfaced.

**What was wrong.** `app.py` configured the session cookie with:

```python
app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
```

Flask ships `SESSION_COOKIE_SAMESITE` in its own default config, already present
with the value `None`. `setdefault` only assigns when a key is *absent*, so this
call never did anything. Werkzeug omits the attribute from `Set-Cookie` entirely
when the value is `None`, so the cookie went out as:

```
Set-Cookie: session=...; HttpOnly; Path=/
```

**Why it mattered.** The application has no CSRF tokens anywhere — no
flask-wtf, no double-submit cookie, no `Origin` checking. `SameSite=Lax` was
therefore not a defence-in-depth layer, it was the *only* thing preventing a
cross-site request from a signed-in browser carrying its session cookie. With
the attribute absent, any page a signed-in user visited could issue a
state-changing cross-site POST — `/transactions/bulk_delete`, `/rules/reorder`,
`/api/sync/all`, `/logout`, or the AI endpoints that spend API credits.

The failure mode is worth naming, because it recurs: the source read as though
the mitigation were present. Reviewing this file would not have found the bug.
Only observing the built application's config or the emitted header would.

**Fix.** Explicit assignment, preserving deliberate overrides:

```python
app.config['SESSION_COOKIE_SAMESITE'] = (
    app.config.get('SESSION_COOKIE_SAMESITE') or 'Lax')
```

`or` rather than a bare assignment so a value supplied by `Config` or by
`test_config` still wins; only Flask's `None` is corrected. A string value of
`'None'` — the deliberate opt-out for cross-site embedding, which also requires
`Secure` — survives, because only Python's `None` is falsy here.

**Verified.** `Set-Cookie` after sign-in, both transport modes:

| `APP_HTTPS` | Emitted attributes |
|---|---|
| `0` | `HttpOnly; Path=/; SameSite=Lax` |
| `1` | `Secure; HttpOnly; Path=/; SameSite=Lax` |

**Regression cover.** Two tests, deliberately not one:

- `test_session_cookie_samesite_is_configured` — asserts the config value.
- `test_session_cookie_header_carries_samesite` — asserts the real `Set-Cookie`
  header. This is the one that actually distinguishes fixed from broken; a
  config-only assertion would have passed even with the bug present had Flask's
  default happened to be `'Lax'`.

**Residual risk.** `SameSite=Lax` does not protect top-level `GET` navigations,
and this application has state-changing routes reachable by `GET` —
`/clear_filters` and `/export`. It is also not a substitute for CSRF tokens:
Lax permits same-site requests from any subdomain, and older browsers ignore the
attribute. Real CSRF protection (double-submit, session-bound, plus `Origin` /
`Sec-Fetch-Site` checking) is Phase 6 and is tracked in the plan, not here.

---

### SEC-0003 — AI response caches were process-global, keyed only by time

- **Severity:** High
- **Status:** Fixed — Phase 5, 2026-07-26
- **Found by:** the Phase 4 audit, recorded as an open finding rather than fixed
  at the time because there was no tenant to key on yet.

**What was wrong.** `app.py` held three module-level dicts:

```python
_insight_cache = {'text': None, 'expires': 0}
_brief_cache   = {'data': None, 'expires': 0, 'key': None}
_wealth_cache  = {'data': None, 'expires': 0}
```

Their contents are generated English prose about a specific family's finances —
"you spent $412 on dining, mostly at weekends". With one owner that is untidy.
The moment households existed it would have been a disclosure of one family's
finances to another, **served from cache, with no query ever reaching the
database to be filtered** — so every other control in Phase 5 would have been
bypassed rather than defeated.

**Fix, in two phases on purpose.** Phase 4 moved the storage behind
`dough/ai/cache.py`, whose `CacheKey` has a required, non-defaulted `scope`
field, and had `AIService` fill it with a constant. That made the eventual fix a
one-line change rather than an archaeology exercise, and made anything that
failed to be updated a type error rather than a silent leak.

Phase 5 supplied the value: `create_app` passes
`dough.services.cache.household_scope` as the service's `scope_provider`, and
every key becomes `household:<id>:<surface>:<variant>`.

The household is read **per operation, never stored on the cache instance**. A
cache outlives any one request, so a household captured at construction is a
household that is wrong for every later caller.

**Verified.** `tests/test_tenancy_boundary.py::test_ai_caches_do_not_leak_between_households`,
asserted behaviourally — a value stored under household A must be invisible from
household B — rather than by inspecting key shapes, so that any correct
implementation satisfies it and an empty cache cannot make it pass vacuously.

**Residual risk.** `household_scope()` raises when no household is bound rather
than falling back to a shared scope. That is deliberate: a fallback would be
reached only in situations nobody anticipated, which are exactly the situations
where two callers would collide. It does mean a missing tenant context surfaces
as a 500 on an AI endpoint rather than a degraded card.

---

### SEC-0009 — `Query.count()` was not covered by the ORM tenant filter

- **Severity:** High
- **Status:** Fixed — Phase 5, 2026-07-26
- **Found by:** `tests/test_tenancy_boundary.py::test_both_households_can_import_the_identical_csv_row`,
  which failed for a reason that was not the one it was written to catch.

**What was wrong.** The `do_orm_execute` backstop adds `household_id = <current>`
to any statement whose `all_mappers` includes a tenant-scoped entity. That covers
SELECT, bulk UPDATE, bulk DELETE, relationship loads and column expressions such
as `func.sum(Transaction.amount)`.

It does not cover `Query.count()`. That method does not execute the query it is
called on — it goes through `_legacy_from_self`, freezing the query into a
subquery and running `SELECT count(*) FROM (<frozen>) AS anon_1`. By the time the
event fires, the only entity left in the statement is the count column,
`all_mappers` is **empty**, the handler concludes no tenant data is involved, and
the frozen subquery is beyond the reach of `with_loader_criteria` anyway.

So `Transaction.query.filter_by(...).all()` returned one household's rows while
`.count()` on the identical query counted every household's.

**Why it nearly escaped.** The failing test was the only one in the suite with
two households each holding a row that matched the same filter. Every other test
would have counted the right number for the wrong reason. A hand-written probe
run earlier during implementation had also "passed" this case — vacuously, because
only one row existed at that point in the scenario.

**Fix.** `models.TenantScopedQuery.count()` applies the household predicate
itself, before the subquery is frozen, and is installed as Flask-SQLAlchemy's
`query_class` — which covers both `Model.query` and `db.session.query(...)`.

**Regression cover.** `test_count_is_scoped_like_the_query_it_counts`, which
asserts the property directly, including that `.count()` and `len(.all())` agree.

**Residual risk.** This is a class of bug, not an instance: any SQLAlchemy path
that rewrites a statement before `do_orm_execute` sees it is invisible to the
backstop. `Session.get` is the other known one, which is why `get_owned` and
`find_owned` query instead. A SQLAlchemy upgrade must re-run
`tests/test_tenancy_boundary.py` and read the failures rather than the summary —
see ADR-0008's accepted risk.

---

### SEC-0002 — No CSRF protection on any state-changing route

- **Severity:** High
- **Status:** Fixed — Phase 6, 2026-07-27
- **Found by:** the Phase 0 audit. Recorded and deliberately left open: the fix
  belonged with authentication, and a CSRF layer written before there was a
  session worth forging would have been guesswork.

**What was wrong.** Nothing. Literally — no tokens, no double-submit cookie, no
`Origin` checking, no `flask-wtf`. Thirty-four routes changed state on an
unsafe method and every one of them would act on a request that a page on
another site caused a signed-in browser to send.

`SameSite=Lax` (SEC-0001) was the only thing in the way, and it was never a
mechanism: it does not cover top-level `GET` navigation, and this application
has state-changing routes reachable by `GET` (`/clear_filters`, `/export`); it
treats every subdomain as same-site; and older browsers ignore it.

**Why the timing mattered.** Before Phase 6 the worst outcome was a signed-in
user's own data being corrupted. Invitations change that: a forged
`POST /household/invites` puts an attacker inside a household, and a forged
`POST /join/<token>` signs a victim's browser into the *attacker's* household,
where anything they subsequently upload or connect lands in a ledger somebody
else can read. So this had to close before membership shipped, not alongside it.

**Fix.** `dough/auth.py`. A session-bound token, checked in a `before_request`
for every unsafe method unless the view carries `@csrf_exempt` (nothing does).
Session-bound rather than double-submit because a double-submit cookie is
defeated by any subdomain that can set a cookie on the parent domain; forging a
token in the signed session means forging the session, which everything else
already depends on.

`Origin` and `Sec-Fetch-Site` are checked as a second signal. A *missing* header
proves nothing — several browsers omit `Origin` on same-origin requests and
`Sec-Fetch-Site` predates no browser still in use only recently — so absence
falls through to the token check. A *wrong* one is refused outright.

Delivery is two paths, because there are two kinds of caller:

- `base.html` patches `window.fetch` once, inline and first in `<head>`, so all
  ~40 existing call sites and every future one attach the header without
  knowing about it. Deferring that script would have let handlers that fire on
  `DOMContentLoaded` send untokenised requests — a 403 that only reproduces on
  a fast connection.
- `{{ csrf_field() }}` in each of the fourteen POST forms, since a plain form
  submission never reaches the wrapper.

**Verified.** `tests/test_csrf.py`, 22 tests. The ones that matter most:

- `test_another_sessions_token_is_refused` — a valid-looking token from a
  different session is rejected. Without it, a check that merely tested the
  field was non-empty would pass.
- `test_no_unsafe_route_accepts_a_missing_token` — enumerates the URL map, so a
  route added in a later phase is covered the day it exists.
- `test_every_post_form_in_the_templates_carries_the_field` — reads the
  templates. No import-time check can catch a template, so without this a
  forgotten field fails as a 403 the first time a person submits the form.
- `test_login_itself_requires_a_token` — `@public` exempts a route from needing
  a session, never from needing a token.

**Residual risk.** `CSRF_ENABLED` is False under `TestingConfig`, so the ~180
tests predating this phase can keep posting bare forms. That is a hole in
coverage rather than in the product — `tests/test_csrf.py` and
`tests/test_route_guard.py` switch it on — but it does mean a route whose CSRF
behaviour is unusual would not be noticed by the rest of the suite. There is
deliberately no environment variable to disable it in production.

---

### SEC-0004 — Login throttle keyed only on `request.remote_addr`

- **Severity:** Medium
- **Status:** Narrowed — Phase 6, 2026-07-27
- **Found by:** the Phase 0 audit.

**What was wrong.** One bucket, keyed on `request.remote_addr`, five failures
per fifteen minutes. Two problems. Behind any reverse proxy `remote_addr` is the
proxy, so every caller in the world shares one bucket — the throttle either
never fires or locks out everybody at once. And with no per-account limit,
credential stuffing from many addresses against one known username was
completely unthrottled, which is what the attack actually looks like.

**Fix.** `dough.auth.LoginThrottle` keeps two buckets. The address bucket stops
one host walking a password list (5 / 15 min). The account bucket stops the
distributed version (10 / 5 min).

The two must not produce the same message, and the reasoning is the interesting
part. "Too many failed attempts" is right for an *address* lock: whoever reads
it almost always caused it, and the counter is keyed on where the request came
from, so it reveals nothing about which accounts exist. An *account* lock is
told the same thing a wrong password is told, because saying anything specific
confirms the username exists — which is exactly what somebody walking a list of
usernames is trying to learn.

`X-Forwarded-For` is read only when `TRUSTED_PROXIES` states how many hops are
ours, and only that many entries from the right-hand end. Reading it
unconditionally would be worse than the original bug: the header is
attacker-controlled, so a fresh forged address per attempt is a fresh bucket per
attempt, and the throttle becomes a no-op that looks like a control.

**Residual risk, and it is real.** The account bucket is a denial-of-service
surface by construction — anyone who knows a username can fill it. Threshold and
window are set to bound that rather than remove it, and a successful sign-in
clears both buckets, so a legitimate user with the password can always end a
lockout somebody else provoked.

Both buckets are in-memory: they reset on restart and do not span processes. A
shared store would fix it and is a dependency this application does not
otherwise have, so it is recorded here rather than built.

---

### SEC-0005 — Passwords hashed with pbkdf2 rather than a memory-hard KDF

- **Severity:** Medium
- **Status:** Closed as already-satisfied, and pinned — Phase 6, 2026-07-27
- **Found by:** the Phase 0 audit.

**What was actually true.** The finding assumed werkzeug's default was pbkdf2.
Werkzeug 3 changed it to `scrypt`, which is memory-hard, and the one account in
the live database was already stored under `scrypt:32768:8:1`. The finding was
stale when this phase reached it.

That is worth writing down rather than quietly deleting, because "it happens to
be right today" is not "it stays right" — the default has moved once already,
and a dependency upgrade that moved it again would change how every password in
the database is protected with no diff to review.

**Fix.** `dough.auth.PASSWORD_METHOD` names the method explicitly, so a change
of default shows up as a change to that file.
`tests/test_csrf.py::test_passwords_are_stored_under_a_memory_hard_kdf` asserts
the stored prefix. `needs_rehash` upgrades an older hash on the next successful
sign-in — the only moment the plaintext exists to re-derive from, which is why
this could not have been a migration.

**Residual risk.** An account that never signs in again keeps whatever hash it
has. There is no way around that short of forcing a password reset.

---

### SEC-0006 — Mutation routes redirected to HTML instead of returning 401

- **Severity:** Low
- **Status:** Fixed — Phase 6, 2026-07-27
- **Found by:** `tests/test_route_guard.py::test_non_api_mutations_return_401_to_fetch`,
  written as a Phase 0 forward spec and expected to xfail.

**What was wrong.** The guard returned 401 only for paths starting with `/api/`.
The other 17 mutating routes are called from `fetch()` in the templates, so an
expired session produced a 302, which `fetch` follows, yielding a 200 full of
login-page HTML that the caller handed to `.json()`. The user saw "saving failed"
rather than "you are signed out".

**Fix.** `dough.auth.wants_json` negotiates. `/api/*` is machine-facing whatever
it claims to accept; `Sec-Fetch-Mode: navigate` is a real page load and keeps
its redirect; an explicit `Accept` preference is honoured only when it expresses
one — `Accept: */*`, which is what a bare test client and curl send, is not a
preference, and treating it as one would turn every unadorned request into a
401.

**Regression cover.** Both sides are pinned:
`test_non_api_mutations_return_401_to_fetch` (17 routes) and
`test_browser_navigation_still_redirects`, which was written in Phase 0
specifically so that this change could not turn a user's form submission into a
raw JSON 401.

---

### OPS-0012 — The scheduler is started per process, not per deployment

- **Severity:** Medium (operational correctness, not a confidentiality boundary)
- **Status:** Open — mitigated by configuration, not by code
- **Found by:** review of `app.py` while assessing SEC-0010

**What is wrong.** `app.py` registers a `before_request` hook that lazily calls
`init_scheduler()` on the first request. That is correct for one process and
wrong for two. Under any multi-worker server each worker handles its own first
request and starts its own scheduler, so *N* workers run *N* copies of every
periodic job:

```
worker 1 ──► scheduler ──► sync item X
worker 2 ──► scheduler ──► sync item X    (concurrently, same rows)
```

The consequences are worse than the throttle duplication in SEC-0010, which is
the same shape in a less damaging place. A weakened throttle is a control
operating below its intended strength. Two schedulers is a *correctness* problem:
duplicated provider API calls against per-item rate limits, overlapping writes to
the same accounts and holdings, and `sync_history` rows that misreport what
happened because two runs interleave in one table.

It is also invisible in every environment where it would be caught. The
development server is one process. The test suite sets `TESTING`, which takes the
`autostart=False` branch. Nothing fails until a deployment adds a second worker
for throughput, and the symptom then appears as provider errors rather than as
anything pointing at process count.

**Decision — Option A, single worker.** The alternatives were an external worker
process (correct for a SaaS, disproportionate now) and a distributed lock (needs
a store this application does not otherwise have; a filesystem lock would work on
one host and quietly stop working on two). Option A is chosen deliberately for
the current stage, with the other two left available.

**What that costs, stated plainly.** The constraint is documented in the
README's Deployment section and enforced by nothing. A future operator adding
`--workers 4` for throughput gets no error, and the failure surfaces at the
provider rather than in this application. The honest fix when scale demands it is
to move scheduled work out of the request-serving process entirely (Option B),
not to add a lock so the extra schedulers can be tolerated.

**Interim mitigation.** `SYNC_AUTO_ENABLED=0` in `.env` and `.env.example` while
the process model is being settled. Manual Refresh and Refresh All are unaffected,
so no capability is lost — only the unattended 12-hour cycle.

---

### OPS-0013 — The audit log grows without bound and has no retention policy

- **Severity:** Low (availability and privacy over a long horizon, not a
  disclosure today)
- **Status:** Open — accepted deliberately for this phase
- **Found by:** writing `dough/services/audit.py` in Phase 8

**What is wrong.** `audit_events` is append-only by design, and nothing deletes
from it. On the busiest event by far — `ai.requested`, one row per model call —
an active household generates on the order of hundreds of rows a day. That is
nothing for years on SQLite. It is still an unbounded table with no policy, and
two consequences follow from that rather than from the size:

1. **Nobody has decided how long this data lives.** An audit log is the one
   table that deliberately retains a record of what people did after the
   underlying rows are gone. "Forever, because we never wrote the deletion
   code" is a retention decision made by omission.
2. **`AUDIT_PAGE_SIZE` is a display bound, not a storage bound.** The activity
   view shows the most recent 100 events. That makes unbounded growth invisible
   in the product, which is exactly the condition under which it stays
   unnoticed.

**Why it is accepted now.** A retention policy is a product decision (how long
should a household be able to see its own history?) and a compliance one, and
both are premature for a self-hosted single-household install. Guessing at 90
days now would be a number nobody chose.

**What would close it.** A documented retention window, a periodic delete that
honours it, and — importantly — a stated exception to the append-only guard for
that one job, since `dough/services/audit.py::_audit_is_append_only` currently
refuses every delete through the ORM. The guard is the reason this cannot be
fixed casually, which is the intended effect.

**What is already true.** Nothing sensitive accumulates: `redact()` strips
credential-shaped values and denied key names on the way in, values are capped
at 200 characters, and no prompt or completion text is ever stored. So the risk
of unbounded growth is disk and privacy-horizon, not a widening disclosure.

---

### SEC-0014 — Werkzeug's debugger could be enabled on a non-loopback bind

- **Severity:** High if reached (remote code execution), previously prevented
  only by a comment
- **Status:** **Closed, Phase 8**
- **Found by:** review of `app.py`'s `__main__` block before Phase 8

**What was wrong.** `app.run(..., debug=_truthy('APP_DEBUG', '1'))` defaulted
the dev-server debugger *on*, and `APP_HOST` is routinely set to `0.0.0.0` so a
phone on the LAN can reach the app. Werkzeug's interactive debugger is an
arbitrary-code-execution console; the PIN is a speed bump, not a security
boundary, and it is printed to the same terminal an attacker on the LAN does
not need. `DevelopmentConfig.DEBUG` already defaulted to False, but the
`__main__` block read the environment variable directly and bypassed it.

**The fix.** The combination now refuses to start:

```
Refusing to start: APP_DEBUG is on and APP_HOST is '0.0.0.0'.
```

A `SystemExit` rather than a warning, and rather than silently forcing debug
off. Silently downgrading would leave an operator wondering why their debugger
stopped working; refusing states the conflict and makes them choose.

---

### BUG-0016 — A `Date` column compared against a `datetime` dropped a day

- **Severity:** Medium (silent data-correctness, not a confidentiality boundary)
- **Status:** Fixed — Phase 10, 2026-07-30
- **Found by:** `tests/test_api_v1.py::test_filters_are_spelled_the_same_way_across_the_api`,
  which asked for a two-day window and got one day back. The second instance was
  found by rendering the budgets page with a transaction dated the 1st, once the
  first had been understood.

Recorded here despite not being a security finding, because the *class* is one
this log should carry: a defect that produces a plausible wrong number and
nothing else. Nothing fails, nothing is logged, and the value is wrong in a
direction nobody investigates.

**What was wrong.** `Transaction.date` is a `Date` column. Comparing it against
a Python `datetime` makes SQLAlchemy bind `'2026-07-02 00:00:00.000000'`, which
SQLite compares as a **string** against the stored `'2026-07-02'`. The stored
value is shorter, so it sorts first, so `date >= start` was False on the start
date itself.

Two places, both shipping for a long time:

| Site | Effect |
|---|---|
| `services/transactions.py::build_transaction_query` | Every filtered transaction list and CSV export dropped its first day |
| `services/budgets.py::spend_by_category` | **The 1st of every month was missing from every budget's spend** |

The second is the one worth dwelling on. A budget genuinely at 135% of its limit
reported 90% and rendered `warn` instead of `danger` — so the product actively
reassured somebody who had overspent. Nobody reports a budget for being too
optimistic.

**Why the end boundary appeared to work,** which is why this survived: the same
string rule makes `date <= end` *true* for the padded value, so only the start
was affected. A test checking one boundary would have passed.

**Fix.** `.date()` on both bounds in both functions, so neither depends on which
side of the comparison the padding lands. Both were shared services by the time
the fix landed, so the pages and the API inherited it together.

**Regression cover.** Two tests, each asserting the boundary is inclusive with a
row placed deliberately on it —
`test_filters_are_spelled_the_same_way_across_the_api` and
`test_budget_spend_includes_the_first_of_the_month`. A fixture using arbitrary
dates passes either way, which is how this went unnoticed.

**Residual risk.** This is a class, not an instance. Any other `Date` column
compared against a `datetime` has the same defect, and PostgreSQL would not
reproduce it — it compares dates as dates — so a future migration would silently
*fix* these while masking any that remain. Worth a grep for `strptime` and
`datetime.now()` reaching a `Date` filter whenever one is added.

---

### SEC-0017 — A bearer token is a long-lived credential with no second factor

- **Severity:** Medium
- **Status:** Open — bounded by design, recorded rather than solved
- **Found by:** writing `dough/services/api_tokens.py` in Phase 10

**What is true.** `/api/v1` accepts an API token as full proof of identity.
Whoever holds the string acts as its user, for as long as the token lives — 90
days by default, or forever if it was issued with `ttl_days: null`. There is no
second factor, no device binding, and no IP restriction.

That is the same shape as SEC-0011 (an invitation link is a bearer credential)
and it is deliberate for the same reason: a native client has nowhere to put a
second factor that an attacker holding the first would not also have.

**What bounds it, and these are real rather than decorative:**

- **Never stored in recoverable form.** Only SHA-256 of the token is persisted,
  so a database file, a backup, or a stray `SELECT *` in a log yields nothing
  usable. The plaintext exists for the duration of one response.
- **Revocable individually and immediately.** `authenticate()` reads
  `revoked_at` on every request, so revocation takes effect on the next call —
  not at the next expiry. Losing a phone does not sign the household out.
- **Scoped.** A `read` token cannot write. A client that only displays data
  should hold one, and `/api/v1/settings` plus `/auth/me` give it what it needs
  to know it.
- **Bounded by the user, continuously.** The `AppUser` is re-read on every
  request rather than trusted from the token row, so a demoted owner's tokens
  lose owner powers and a removed member's stop working, with no token
  bookkeeping at all.
- **Audited.** Issue, revocation and *rejection* are all recorded. A rejected
  credential lands as a NULL-household row visible only to an operator, which is
  the same shape a failed login takes and for the same reason.
- **Refused identically whatever is wrong with it.** Unknown, revoked, expired,
  malformed and orphaned all answer the same 401 with the same body. Telling a
  holder that a token was *revoked* confirms it was once real, which is the one
  fact somebody working through guesses wants. The reason goes to the audit log.

**Residual risk, stated plainly.** A token exfiltrated from a device — an
unencrypted backup, a shared machine, a client that logs its own headers — is a
working credential to a household's complete financial history until somebody
notices and revokes it. `last_used_at` is the only signal that would show it
being used, it is written at minute resolution, and nothing alerts on it.

**What would close it.** Device-bound keys (a token usable only with a private
key held in the platform keystore), or short-lived access tokens with a rotation
scheme. Both are larger than this phase and neither is worth building before
there is a native client to hold the key.

---

### SEC-0018 — `/api/v1` has no rate limiting beyond the login throttle

- **Severity:** Low (availability and cost, not disclosure)
- **Status:** Open — accepted for this phase
- **Found by:** review of the Phase 10 surface

**What is wrong.** `POST /api/v1/auth/login` is throttled — it shares
`LoginThrottle` with the web login, deliberately, so alternating between the two
surfaces cannot halve the counter. Nothing else is. A valid token may call
`/api/v1/transactions` as fast as the process will answer.

Two consequences, and the second is the one that costs money:

1. A single client can saturate a single-worker deployment (OPS-0012), which is
   the deployment this application is documented to run.
2. `/api/v1/copilot/*` and `/api/v1/chat/…/messages` spend AI credits per call
   and are reachable by any `read`-scoped token. There is no per-household
   ceiling, no daily cap, and no accounting beyond the `ai.requested` audit
   event.

**Why it is accepted now.** The throttle this application already has is
in-memory and per-process (SEC-0010), so extending it to the API would extend a
control that does not span workers and resets on restart — the appearance of a
limit rather than one. Doing it properly needs a shared store this application
does not otherwise have, which is the same conclusion SEC-0010 reached.

**What is already true.** Every caller is authenticated, so this is not an
anonymous surface: abuse is attributable to a token, and that token can be
revoked. The exposure is a household spending its own AI budget or its own
process's time, not an outsider spending them.

**What would close it.** A shared rate-limit store, and per-household AI quotas.
The second is Phase 16 (AI governance) in the roadmap, and the two should
probably land together since they need the same counter.

---

### SEC-0019 — No credential could be invalidated by a change to the account

- **Severity:** Medium (latent — becomes High the moment a password can change)
- **Status:** Closed, Phase 10.5
- **Found by:** review of the Phase 10 surface, after the API shipped

**What was wrong.** The application had two kinds of credential — a signed
session cookie and an opaque bearer token — and neither could be withdrawn in
response to a change to the account behind it. The only revocation that existed
was per-object: revoke *this* token, clear *this* browser's session. There was no
"and everything else this account has issued".

Concretely, none of these worked:

- Change a password → every device that already has a token keeps its access,
  indefinitely, using a credential obtained with the password that was just
  replaced.
- Sign out on a lost phone from another device → nothing to press.
- Suspect a session cookie was captured → rotating `SECRET_KEY` signs the whole
  installation out, which is the only lever and is not one anybody pulls.

**Why it was latent rather than exploited.** There is no password-change route in
this application. Nothing could change a password, so nothing could leave a
credential stranded behind one. That is a property of the current feature set,
not of the design, and it inverts the moment somebody adds the route — at which
point the missing invalidation is a silent default rather than a visible gap.

That inversion is the reason this was fixed before the next phase rather than as
part of building password changes. The mechanism being absent is a gap somebody
can see; the mechanism being present but not called is a gap that looks like
working code.

**What was done.** `AppUser.session_version`, a generation counter every
credential is stamped with: the session cookie carries the value it was signed
in under (`dough.auth.SESSION_VERSION_KEY`), `api_tokens.session_version` stores
the value it was issued under, and both are compared against the account's
current value on every request. Raising the counter invalidates everything at
once, on both surfaces, with one write.

Three decisions inside that are worth stating, because each has a plausible
alternative that is worse:

1. **The bump is a `before_flush` listener, not a function callers invoke.**
   `dough/auth.py::_bump_session_version_on_password_change` raises the counter
   whenever `AppUser.password_hash` changes, in any session this process opens —
   route, CLI, shell, or a `tools/` script. An explicit `invalidate(user)` call
   would be a convention, and a convention that is forgotten fails silently and
   in the unsafe direction: the password changes, and the old credentials keep
   working. This is the same default-deny shape as `@public` and the tenancy
   write guard.

2. **Invalidation is lazy, not a sweep.** Nothing stamps `revoked_at` across
   `api_tokens` on a password change. A sweep is a second write that can be
   lost, so a token could survive an invalidation by having been missed; a
   comparison against a row already loaded cannot be missed. The cost is that a
   superseded token still looks issued in the table, which `ApiToken.state()`
   answers by reporting it as `'stale'` — pinned against the OpenAPI enum by
   `tests/test_openapi.py`.

3. **A session with no version recorded is refused.** Fail-closed, which costs
   one sign-in per browser when this deploys. Accepting an absent value instead
   would exempt every session minted before this shipped, permanently — and
   those are the long-lived ones, so "they will age out" does not cover the
   session anybody would actually be worried about.

The one exemption is the sign-in rehash (`upgrade_password_hash`), which
replaces a stale hash without the password having changed. It is marked at that
single call site. It cannot be inferred instead: a rehash goes old-KDF →
new-KDF, and so does a password *change* made by somebody whose stored hash was
stale, so the two are indistinguishable from the values alone — and guessing
would be wrong in the direction that leaves credentials alive.

**What is still not closed.** The mechanism has no user-facing trigger, because
there is still no password-change route and no "sign out everywhere" control.
Building either is now a matter of writing the route: the invalidation is
automatic for a password change and is one assignment for a deliberate mass
sign-out. Until then the counter is reachable only by an operator writing to the
model. Session-invalidating events other than a password change — role changes
and household removal — were reviewed and deliberately left alone: they are
already handled by `api_tokens.authenticate` re-reading the user on every
request, which is what makes a demoted owner's token lose owner powers rather
than stop working.

`tests/test_session_version.py` covers both surfaces, the rehash exemption, and
the fail-closed cases.

---

### SEC-0020 — `@public` skipped the session-lifetime check

- **Severity:** High (would have been, had it shipped)
- **Status:** Fixed — Phase 10.5, in the same commit that introduced it
- **Found by:** writing `tests/test_landing.py::test_an_expired_session_is_cleared_rather_than_honoured`
  *before* the landing page, on the reasoning that a marker whose meaning is
  changing deserves a test for the meaning it is not supposed to acquire.

**What was wrong.** `@public` has always meant "may run without a session", and
`_require_login` implemented it as an early `return None` before any session
handling. That was exactly right for every view that carried the marker until
this phase: `/login`, `/setup`, `/join`, the health probes and
`/api/v1/auth/login` are all views a signed-in browser has no reason to load and
none of which render anything belonging to an account.

`/` broke that assumption. It is `@public` because a stranger must be able to
load it, and it renders **the dashboard** for a signed-in one. So the early
return meant a browser whose session had passed its absolute lifetime, or whose
credentials had been invalidated by a password change, was still handed the
dashboard — with a household bound and real balances on it — because
`_enforce_session_lifetime` never ran.

The marker's *meaning* ("may run without a session") and its *effect* ("no
session check runs") had been identical for four phases, and they came apart the
moment a public view also served signed-in content.

**What was done.** `_require_login` now runs `_enforce_session_lifetime()` for a
public view too, whenever the request carries a `user_id`, and **discards the
response**. A public route must not redirect somebody to a login page for
failing a check it does not require; the lifetime function has already cleared
the cookie, so the request simply proceeds as what it now is — anonymous.

The generalisation matters more than the specific fix: `@public` now suppresses
the login *redirect* and nothing else. It does not disable tenancy (an anonymous
request binds no household, so a scoped query on that path raises rather than
leaking), it does not disable CSRF, and it no longer skips session validation.
`tests/test_route_guard.py::test_public_endpoint_set_is_minimal` states all
three, so the next view to carry the marker inherits the answer.

---

### SEC-0021 — The bearer actor outlived the request that set it

- **Severity:** Medium (latent in production; live wherever an app context is reused)
- **Status:** Fixed — Phase 10.5
- **Found by:** `tests/test_identity.py::test_a_reset_invalidates_every_session_and_every_api_token`,
  which mixes an API-authenticated request and a session-authenticated one in
  one context because that is what a real deployment does across two requests.
  Pinned since by `tests/test_api_auth.py::test_the_actor_does_not_survive_into_the_next_request`.

**What was wrong.** `dough/api/guard.py::authenticate_bearer` only ever *set*
`g._dough_api_actor`, and returned early — writing nothing — for a non-`/api`
path and for a request offering no credential. `bearer_actor()` claims to name
who *this* request authenticated as; on those two paths it named whoever the
previous request that reached the assignment had.

The consequence is not confined to the API. `dough.auth.current_user` reads the
bearer actor **first**, deliberately, so that role checks work identically on
both surfaces. A session request following an API request therefore returned the
token's user and **skipped `session_is_current` entirely** — leaving a browser
signed in after a password change had invalidated it, which is precisely the
guarantee SEC-0019 exists to provide.

**Why it was latent in production.** `g` is per-app-context and Flask pushes a
fresh one per request, so a served request never inherits another's. It leaks
wherever an app context outlives a request: every test fixture in this
repository, and any future CLI command or background worker that pushes a
context and handles more than one thing inside it. The second of those is the
reason this is a defect rather than a test artefact — the property "the actor
describes this request" is one the rest of the application relies on, and it was
only true by accident of the request lifecycle.

**What was done.** The negative answer is written down rather than left implied:
both keys are set to `None` at the top of the hook, before any early return, and
a valid credential overwrites them. Default-deny in the shape `dough/auth.py`
already uses twice.

---

### SEC-0022 — Account enumeration through password recovery

- **Severity:** Medium
- **Status:** Addressed by design — Phase 10.5, at the point the routes were written
- **Found by:** design review of the recovery flow, before implementation

**What the risk is.** `/forgot-password` is reachable by anybody and takes an
email address. Any observable difference between "this address has an account"
and "it does not" turns it into a free membership oracle for this application —
and because the input is an address rather than a username, a positive answer is
a fact about a *person* ("this individual banks with Dough") rather than about
an account name they chose.

**The three parts, because the third is the one that gets missed.**

1. **Wording and shape.** Both outcomes render the same template, with the same
   copy — "If there's a Dough account for that address…" rather than "We've sent
   you an email", which would be a lie half the time and the lie is the tell.
   Same status, same body, no redirect in one case only.
   `tests/test_identity.py::test_forgot_password_says_the_same_thing_for_both_outcomes`
   compares the response bytes; the browser test compares the rendered text,
   because a difference introduced by a template conditional would pass a byte
   comparison of two identical templates and fail on screen.

2. **The rate limiter must not become the oracle.** This is the failure a
   control *introduces*: "you are being rate limited" for one address and "check
   your inbox" for another is the same signal wearing a different hat. Both
   limiter policies refuse silently, into the identical response.

3. **Timing.** Sending mail takes tens of milliseconds; not sending it takes
   none. Over a few hundred requests that gap answers the question the wording
   refuses to. `UNIFORM_RESPONSE_SECONDS` (350ms) is a **floor**, not a trailing
   `sleep` — a pause added after the work would make the real path 350ms slower
   than the fake one, which is the same leak with the sign flipped.

   It is not constant-time in the cryptographic sense and does not need to be:
   it hides a difference measured in tens of milliseconds behind a wait measured
   in hundreds, against a remote attacker whose measurements already carry
   network jitter of the same order.

**What is deliberately *not* covered.** `/register` says out loud that a
**username** is taken, and `/join` has done so since Phase 6. Somebody choosing
a username has to be told, or they retype the same value; usernames are chosen
here rather than held elsewhere, and they are already enumerable through the
invitation flow. A taken **address** is never confirmed — the refusal names both
fields precisely so that neither is.

The fact itself is not lost, only relocated: `auth.password.reset.requested` is
recorded for both outcomes with `user_exists` in its metadata, exactly as
`auth.login.failed` has been since Phase 8. An operator can see somebody walking
a list of addresses; the walker learns nothing.

---

### SEC-0023 — A password-reset token is the strongest bearer credential here

- **Severity:** Medium
- **Status:** Bounded by design — Phase 10.5
- **Found by:** design review, alongside SEC-0011 (invitations) and SEC-0017
  (API tokens), which are the same shape of risk

**What the risk is.** Holding a reset link *is* the authorization, and it is
worse to leak than either of its predecessors: using it also **locks the real
owner out**, so the victim's first signal is that their own password stopped
working.

**What bounds it.**

- **Stored as an unsalted SHA-256, never in recoverable form.** The input is 256
  bits from `secrets.token_urlsafe`, so there is no dictionary to precompute and
  nothing a work factor would protect against — the same reasoning as
  `household_invites` and `api_tokens`.
- **One hour, not seventy-two.** The verification token gets 48 hours because
  nothing is blocked while it is unspent; the reset window is exactly how long a
  stolen mail is worth stealing.
- **Spent by loading the form, not by submitting it.** `identity.redeem` stamps
  `used_at` in the transaction that resolves the row, so no code path — an early
  return, a validation failure, an exception — leaves a redeemable token behind
  after somebody has been let through. The cost is real: a user who fails the
  password rules has to request a new link. The alternative keeps the link alive
  across a window the application does not control, which is the window somebody
  with the victim's mail is waiting in.
- **Requesting a new one retires the old.** So a reset requested by an attacker
  who has read the inbox is cancelled the moment the victim requests their own.
- **Changing the address retires links already in flight.** `sent_to` is
  recorded at issue and compared back at redemption, so a link cannot be
  redeemed against an address its holder never proved they control.
- **The grant that survives the token expires too.** Spending the token on the
  GET leaves a session key saying "may set this account's password, no current
  password required" — and unbounded, that would sit in the cookie for the whole
  session lifetime if somebody loaded the form and wandered off. A stolen
  session cookie would then be a full takeover, including lockout, on the one
  operation that otherwise always demands the current password. It lasts fifteen
  minutes (`RESET_GRANT_SECONDS`), which is the life of the *form* rather than
  of the link, and a stale one is dropped rather than left to be retried.
- **Redeeming it invalidates everything.** Every session and every API token,
  through `session_version` — which is the point: the premise of needing a reset
  is that somebody else may hold the old credential.
- **The flow does not end in a session.** Completing a reset redirects to
  `/login` rather than signing the resetter in, so a stolen link is a password
  change somebody notices rather than a session somebody uses.
- **The token never reaches the log.** See SEC-0024.

**Residual.** An attacker with live access to the victim's mailbox can reset the
password and take the account, and no amount of token hygiene changes that —
mailbox access is a superset of this application's authentication. That is the
accepted floor of any email-based recovery, and the reason
`REQUIRE_EMAIL_VERIFICATION` and a second factor stay on the roadmap.

---

### SEC-0024 — The log redactor cannot recognise this phase's credentials

- **Severity:** Medium
- **Status:** Addressed by call-site discipline — Phase 10.5
- **Found by:** reviewing what `dough/logging.py` would actually catch, rather
  than assuming the mechanism generalises

**What is wrong with assuming redaction covers it.** `dough/logging.py` protects
the log two ways: field *names* on a deny-list, and a `_SECRETISH` pattern over
rendered messages. The pattern recognises `sk-…` keys, Plaid `access-…` tokens
and card-length digit runs.

Every credential this phase introduced is a high-entropy string that looks like
**nothing in particular**: a reset token, a verification token, an API token
(past its `dgh_` prefix) and a Fernet key are all base64-ish blobs. `_SECRETISH`
does not match them and no reasonable pattern could — the pattern that did would
also redact every hash, id and base64 payload in the logs.

So for these, "the redactor will catch it" is **false**, and the protection has
to be that the value is never handed to the logger at all. That is a property of
call sites, not of a mechanism, which is the kind of property that decays.

**What was done.**

- `ConsoleBackend` writes to **stdout directly**, never through `logging`. It is
  the one place in the application that deliberately renders a live credential to
  a stream, and confining it to the operator's terminal — rather than the log
  stream, which is shipped, indexed and retained — is what makes that acceptable.
- What *is* logged, by every backend, is the recipient and the purpose: the
  operational facts (delivery works, this flow was reached). Never the body.
- `EmailMessage.__repr__` omits the body, because a repr is what ends up in a
  traceback, a debugger transcript and a pytest assertion diff.
- `SmtpBackend` raises with the host and the exception *type* only. An SMTP
  error can quote the message it was carrying, and that message holds the token.
- `ProductionConfig.validate` names variables and never values. It runs at boot
  and its exception goes to the log, so a validator that quoted what it found
  invalid would put a real secret into the line reporting that a secret was
  wrong.
- `TokenCipher`'s decrypt failure says the key does not match, and quotes
  neither the key nor the ciphertext.

`tests/test_secret_hygiene.py` holds all of it, sweeping the log *and* the audit
table for each credential the flows actually produce — rather than asserting
that a redactor exists.

---

## Secrets

What this deployment needs, why, and what happens without it. The table is
`config.REQUIRED_SECRETS`; `.env.example` documents each one at its declaration
and `tests/test_secret_hygiene.py` asserts the three do not drift.

| Variable | Required in production | What it protects | Losing it | Leaking it |
|---|---|---|---|---|
| `SECRET_KEY` | **Yes** | Session cookie and CSRF token signatures | Everyone is signed out | Session cookies can be forged — full account takeover |
| `ENCRYPTION_KEY` | **Yes** | `connected_accounts.auth_blob` — the Plaid and Coinbase access tokens | Every institution must be linked again | The stored tokens read the accounts directly |
| `DATABASE_URL` | No (defaults to SQLite) | Where the data is | — | Database access |
| `ANTHROPIC_API_KEY` | No — feature off without it | Dough, the AI assistant | AI surfaces report themselves unconfigured | Billable usage on your account |
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | No — sandbox without them | Account synchronization | Sync falls back to the local sandbox | Provider-side access, per Plaid's model |

**Validation is at startup, and reports everything at once.** `APP_ENV=production`
refuses to start without the two required values, listing *every* missing one
with what it is for and the command that produces it. An `if` chain would report
the first, the operator would set it and redeploy, and be told the second — one
five-minute fix turned into three deploys.

**`ENCRYPTION_KEY` is the one worth being careful about**, and production refuses
to generate it. Generating a key is silent, succeeds, and makes every
already-stored institution token unreadable — and the failure does not arrive at
boot. It arrives at the next sync, as "stored credentials cannot be decrypted",
pointing at the encryption layer rather than at the missing file that caused it.
On a container filesystem that starts empty on every deploy, that is every
connection breaking on every deploy with nothing naming the cause.

**Two names each, older wins.** `SECRET_KEY` also reads `SESSION_SECRET`;
`ENCRYPTION_KEY` also reads `SYNC_ENCRYPTION_KEY`. In both cases the name
existing installations already have takes precedence, so nobody has to rename
anything and a machine that sets both cannot change behaviour under itself.

**Warnings that are not errors.** `ProductionConfig.warnings()` reports
`MAIL_BACKEND=console` (reset links printed to stdout, where nobody locked out
can reach them), `RATELIMIT_BACKEND=memory` (SEC-0010), and an unset
`PUBLIC_BASE_URL` (links in outbound mail built from the client-controlled `Host`
header). Each has a legitimate use, and refusing to boot over a judgement call is
how operators learn to set an override variable — which then hides the checks
that were not judgement calls.

**Rotation.** `ANTHROPIC_API_KEY` is in the README. For `SECRET_KEY`, replacing
it signs everyone out and is otherwise safe. For `ENCRYPTION_KEY` there is no
in-place rotation today: the stored blobs are encrypted under one key and nothing
re-encrypts them, so rotating means every household re-links. That is a real gap
and it is recorded as OPS-0025 below rather than implied by its absence.

---

## Open findings carried into later phases

| ID | Finding | Severity | Phase |
|---|---|---|---|
| SEC-0002 | ~~No CSRF protection on any state-changing route~~ | High | **Closed, Phase 6** |
| SEC-0003 | ~~AI response caches are process globals keyed only by time~~ | High | **Closed, Phase 5** |
| SEC-0004 | ~~Login throttle keyed only on `request.remote_addr`~~ — two buckets now; the residual in-memory and account-DoS risks are recorded above | Medium | **Narrowed, Phase 6** |
| SEC-0005 | ~~Passwords hashed with pbkdf2~~ — was already scrypt; now named and pinned | Medium | **Closed, Phase 6** |
| SEC-0006 | ~~17 non-`/api` mutation routes redirect to an HTML login page instead of returning 401~~ | Low | **Closed, Phase 6** |
| SEC-0007 | Tailwind is loaded from `cdn.tailwindcss.com` at runtime — a third-party script with full DOM access on pages rendering real financial data, and a build-time dependency on an external host staying uncompromised | Medium | — |
| SEC-0008 | ~~No `Cache-Control: no-store` on authenticated responses; a shared browser can restore the dashboard from bfcache after logout~~ — applied by session rather than per route, so a route added later is covered by default; static assets stay cacheable | Medium | **Closed, Phase 8** |
| SEC-0009 | ~~`Query.count()` was not covered by the ORM tenant filter~~ | High | **Closed, Phase 5** |
| SEC-0010 | Login and CSRF throttling state is per-process and in-memory, so it resets on restart and does not span workers. Bounded by design (see SEC-0004) but not durable | Low | — |
| SEC-0011 | An invitation link is a bearer credential with no second factor: whoever holds it joins the household. Mitigated by single use, a 72-hour default expiry, revocation, and the fact that it is shown once and stored only as a hash — but a link forwarded to the wrong person is a full disclosure until it is revoked | Medium | — |
| OPS-0012 | The sync scheduler is started per process, so a multi-worker deployment runs one scheduler per worker against the same connections. Resolved by decision (single worker) and documented in the README; **not enforced in code**, and no environment in which it would be caught runs more than one process | Medium | — |
| OPS-0013 | `audit_events` is append-only with no retention policy and no deletion path. Not a disclosure — nothing sensitive is stored — but an unbounded table whose growth the product cannot see, and a retention decision made by omission | Low | — |
| SEC-0014 | ~~`APP_DEBUG` defaulted on in `app.py`'s `__main__`, so a `0.0.0.0` bind could expose Werkzeug's debugger (RCE) on the LAN~~ | High | **Closed, Phase 8** |
| BUG-0016 | ~~A `Date` column compared against a `datetime` excluded the first day of every filtered window — transaction lists, CSV exports, and the 1st of every month from every budget's spend~~ | Medium | **Closed, Phase 10** |
| SEC-0017 | An API token is a bearer credential with no second factor: whoever holds it acts as its user until it expires or is revoked. Bounded by hash-at-rest, individual revocation, scopes, continuous re-resolution of the user, and audit on issue/revoke/reject — but an exfiltrated token is working access until somebody notices | Medium | — |
| SEC-0018 | `/api/v1` has no rate limit beyond the shared login throttle. **Narrowed, Phase 10.5:** the abstraction exists (`dough/services/ratelimit.py`), the policies are declared in one reviewable table, and the four routes this phase added — registration, both halves of password reset, and verification re-send — are enforced by it. **The `api`, `api_write`, `ai` and `ai_daily` policies are declared and deliberately *not* wired**, because the only backend is in-memory: an AI budget a restart clears and a second worker doubles is the appearance of a cost control rather than one, on the surface where the cost is real money. `RATELIMIT_BACKEND=redis` is named and raises rather than falling back, and `tests/test_ratelimit.py::test_the_declared_policies_match_their_call_sites` pins which policies are in which state so the distinction cannot blur | Low | **Narrowed, Phase 10.5** |
| SEC-0019 | ~~No credential — session or API token — could be invalidated by a change to the account behind it~~ — `session_version` now covers both surfaces, raised automatically whenever a password hash changes. **The residual noted here is closed in Phase 10.5:** `/settings/password` and `/settings/sessions/revoke` are the controls that trigger it, and a completed password reset does too | Medium | **Closed, Phase 10.5** |
| SEC-0020 | ~~`@public` returned before the session-lifetime check, so a public view that also renders signed-in content (`/`) would serve the dashboard to an expired or invalidated session~~ — the marker now suppresses the login redirect and nothing else | High | **Closed, Phase 10.5** |
| SEC-0021 | ~~`authenticate_bearer` only ever *set* the bearer actor, so `bearer_actor()` named the previous request's actor wherever an app context outlived a request — and `current_user` reads it first, skipping the session check~~ | Medium | **Closed, Phase 10.5** |
| SEC-0022 | ~~Password recovery could distinguish a registered address from an unregistered one~~ — identical wording, shape and elapsed time, and the rate limiter refuses into the same response rather than becoming the oracle itself | Medium | **Closed, Phase 10.5** |
| SEC-0023 | A password-reset link is a bearer credential with no second factor, and leaking one is worse than leaking a session because using it locks the real owner out. Bounded by hash-at-rest, a one-hour expiry, single use spent on *load*, retirement on re-request and on address change, and full credential invalidation on redemption — but an attacker with live mailbox access takes the account, which is the accepted floor of email-based recovery | Medium | — |
| SEC-0024 | ~~The log redactor's pattern cannot recognise a reset token, a verification token or a Fernet key — they are high-entropy strings that look like nothing in particular, so "the redactor will catch it" was false for every credential this phase added~~ — closed by call-site discipline (console mail bypasses `logging` entirely; reprs, SMTP errors, config validation and cipher errors all omit values) and swept by `tests/test_secret_hygiene.py` | Medium | **Closed, Phase 10.5** |
| OPS-0025 | `ENCRYPTION_KEY` has no in-place rotation: the stored `auth_blob` values are encrypted under one key and nothing re-encrypts them, so rotating the key means every household re-links every institution. Acceptable while the key is generated once and backed up; a real gap the first time one has to be rotated under suspicion of compromise, which is exactly when re-linking everything is least welcome | Medium | — |
| OPS-0015 | `init_scheduler(app)` ignores its argument on every call after the first — the module-level singleton keeps its original app. Latent in production (one app per process) and wrong under test. Reported before Phase 8; deferred to Phase 9, which owns background execution | Medium | 9 |
| OPS-0016 | `SyncScheduler._busy` is one process-wide mutex, so one household's manual Refresh is refused with 409 while another household's sync is running. Invisible today with one household; a correctness-of-experience bug the moment there are two | Medium | 9 |
