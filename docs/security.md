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
| OPS-0015 | `init_scheduler(app)` ignores its argument on every call after the first — the module-level singleton keeps its original app. Latent in production (one app per process) and wrong under test. Reported before Phase 8; deferred to Phase 9, which owns background execution | Medium | 9 |
| OPS-0016 | `SyncScheduler._busy` is one process-wide mutex, so one household's manual Refresh is refused with 409 while another household's sync is running. Invisible today with one household; a correctness-of-experience bug the moment there are two | Medium | 9 |
