# ADR-0014 — A public surface, and an identity lifecycle behind it

- **Status:** Accepted
- **Date:** 2026-07-30
- **Phase:** 10.5
- **Extends:** ADR-0009 (authentication and membership) with the three entry
  points it left out — registering, recovering and managing an account; ADR-0013
  (credential generations) with the controls that actually raise the counter.
- **Supersedes:** nothing.

## Context

After Phase 10 this application was multi-user and had no way for a user to come
into existence except two: `/setup`, which runs once and makes the first owner,
and `/join`, which needs somebody already inside to issue a link. There was no
password recovery at all, no way to change a password, no page on which an
account could see what could reach it, and nothing at `/` for anybody who was
not already signed in.

That is a coherent design for the thing this was — a self-hosted ledger for one
household. It is not one for a product with a sign-up button, and the gap is not
a list of missing pages. It is that four of the five things above are *security
surfaces*: recovery is the classic enumeration oracle, registration is the only
unauthenticated route that can grow the database, a password change is the
operation that must invalidate everything, and a public `/` is the first route in
the application that renders different content to different callers.

ADR-0013 built the invalidation mechanism deliberately ahead of the feature that
needed it, on the reasoning that a mechanism which exists but is never called is
a gap that looks like working code. This phase is the other half of that bet:
the controls that call it.

## Decision

### 1. `/` branches on the session; the dashboard does not move

`/` renders the marketing page for a caller with no session and the dashboard for
one with a session. The URL surface is unchanged.

The alternative — `/` becomes the landing page outright, the dashboard moves to
`/dashboard` — is what most products do and it was rejected on cost rather than
on taste. `redirect('/')` ends sign-in, setup and invitation redemption;
`url_for('core.dashboard')` resolves to `/` in every template and test;
`tests/test_url_map_snapshot.py` freezes the URL surface precisely so a change
like that is deliberate. Every one of those is a place the move could be got
subtly wrong, and the benefit is a tidier routing table.

The branch is two lines at the top of the view and it happens **before any
query**. That is load-bearing rather than tidy: the view is now `@public`, so an
anonymous request reaches it with no household bound, and every `Transaction.query`
below it would raise `TenantContextMissing`. The landing page touches no tenant
data at all, and the early return is what guarantees it never gets the chance to.

One exception, and it is about the first five minutes rather than about
security: an installation with **no accounts** redirects to `/setup` instead. An
empty installation is an unfinished install, not a stranger's visit, and a
marketing page whose "Sign in" leads to `/login` leads to `/setup` anyway.

### 2. `@public` suppresses the login redirect and nothing else

`/` is the first view where "may run without a session" and "no session check
runs" came apart, and the second reading was a hole: a browser past its absolute
lifetime, or whose credentials had been invalidated, was still handed the
dashboard. See SEC-0020.

So the marker's meaning is now stated in full and holds for every view that
carries it:

- It does **not** disable tenancy. An anonymous request binds no household, so a
  scoped query on that path raises rather than leaking.
- It does **not** disable CSRF. `/register`, `/forgot-password` and
  `/reset-password` all POST and all carry a token. An unprotected `/register`
  in particular lets an attacker sign a victim's browser into the *attacker's*
  account, where anything the victim then uploads lands in a ledger the attacker
  can read — the same argument that has protected `/login` since Phase 6.
- It does **not** skip session validation. `_require_login` runs
  `_enforce_session_lifetime` for a public view too and discards the response, so
  a failed check clears the cookie and the request proceeds as what it now is:
  anonymous.

### 3. The account rules live in one service, not in three routes

`dough/services/identity.py` owns validation, registration, password changes,
address changes, token issue and token redemption. `/register`, `/settings` and
the reset flow all call it.

The duplication this prevents is specific. `/setup` and `/join` each carried
their own copy of "make a household, make an owner, hash the password", and
adding `/register` as a third is how the three drift — on validation, because
that is the part each author rewrites from memory. The third copy is the one that
accepts a six-character password because whoever wrote it was reading the branch
that checks for eight, and nothing fails.

### 4. One table for both link types, told apart by `purpose`

`email_verifications` carries the address-verification token and the
password-reset token. The listed fields are identical for both and so is every
rule about them: hashed, expiring, single-use.

Two tables would mean two implementations of "is this token still redeemable?",
and the failure mode of the duplicate is not aesthetic — the second copy is the
one that gets the expiry right and the single-use check subtly wrong, because it
was written by someone reading the first copy rather than the requirement.

`purpose` is what stops a link that proves an address is reachable from also
being a link that sets a password. It is checked in `redeem()`, so presenting a
verification token at the reset route is a lookup that returns nothing rather
than a comparison somebody has to remember to write.

### 5. Redemption spends the token, and it happens on the GET

`redeem()` stamps `used_at` in the same transaction that resolves the row and
returns the user. A caller cannot obtain the user without having spent the token,
so no code path — an early return, a validation failure on the form the token
unlocked, an exception — leaves a redeemable token behind after somebody has been
let through.

For password reset that means **loading the form spends the link**. This costs
something real: a person who then fails the password rules has to request a new
one. It is the right trade. Validate-on-GET/spend-on-POST leaves a live token
across a window the application does not control, and that window is exactly the
one somebody holding the victim's mail is waiting in.

The redeemed user is carried across the POST in the session — not the token,
which is spent, and not a user id in a hidden field, which would let anybody POST
a chosen id and set that account's password.

### 6. No response reveals whether an account exists

Stated as one rule with three parts because the third is the one that gets
missed: wording and shape, *and* elapsed time. `UNIFORM_RESPONSE_SECONDS` is a
floor rather than a trailing pause — a `sleep` after the work would make the
real path slower than the fake one, which is the same leak with the sign
flipped. The rate limiter refuses into the identical response, because "you are
being rate limited" for one address and "check your inbox" for another is the
oracle again, arriving through the control added to prevent it.

Usernames are the deliberate exception, at `/register` and `/join`. Somebody
choosing a username has to be told it is taken; addresses are identifiers other
people hold, and confirming one is registered is a fact about that person.

Full reasoning, including what is *not* covered, is SEC-0022.

### 7. Two abstractions with one backend each, and the seam is the point

`EmailService` and `RateLimiter` both ship with a real implementation and an
interface written for the one that comes later.

That is not speculative generality. For mail, two of the three useful backends
are not SMTP at all — `console` is the only one under which the flow is usable
before anything is configured, and `memory` is what lets a test assert a reset
mail went to the right address with no network. For rate limiting, SEC-0018
already recorded *why* the API is unlimited, and the reason was about the
**backend** (in-memory limits do not span workers) rather than about the
interface. Building the interface now means the call sites exist, the policies
are declared in one reviewable table, and Redis is a config change plus one
class — rather than an archaeology exercise under deadline.

`RATELIMIT_BACKEND=redis` raises rather than falling back to memory. An operator
who sets it has decided they want a shared limiter; silently giving them a
per-process one is the failure they were trying to avoid, arriving quietly.

### 8. Secrets are validated at startup, together, and never printed

`config.REQUIRED_SECRETS` is a table rather than a chain of `if not X: raise`,
so a deployment missing three variables is told about three variables. An `if`
chain reports the first, the operator sets it, redeploys, and is told the second
— one five-minute fix turned into three deploys.

Messages name variables and never values. This runs at boot and its exception
goes to the log, so a validator that quoted what it found invalid would put a
real secret into the line reporting that a secret was wrong.

`ENCRYPTION_KEY` is required in production and **will not be generated** there.
Generating one is silent, succeeds, and makes every already-stored institution
token unreadable — and the failure arrives at the next sync rather than at boot,
reported as an encryption error rather than as the missing file that caused it.

## Consequences

**A one-time sign-in cost, already paid.** ADR-0013's fail-closed session check
signs every pre-existing browser out once. Nothing here adds to that.

**`/` is now the most load-bearing route in the application.** It is public, it
renders two different pages, and it is the first thing anybody sees. Three test
files hold it: `tests/test_landing.py` for what it renders and what it must not,
`tests/test_route_guard.py` for the marker's meaning, and
`tests/browser/test_identity_journey.py` for the rendered result at three
viewports.

**Registration is off by default and the route still exists.** A closed instance
answers `/register` with 403 and a page explaining that invitations are how
people get in. 404 was the alternative and it is worse in both directions: for
the person, "not found" is indistinguishable from a typo, so they retry the URL
instead of asking for an invitation; for the product, a URL that exists on some
deployments and not others makes the landing page's own button a dead link that
nothing can detect.

**Email delivery is now a dependency of account recovery, and it is not one this
application controls.** `MAIL_BACKEND=console` in production is a warning rather
than an error, because a demo instance genuinely may want it — but on a real
deployment it means reset links are printed to a terminal nobody locked out can
reach. `ProductionConfig.warnings()` says so at boot.

**An attacker with live mailbox access takes the account.** No amount of token
hygiene changes that; mailbox access is a superset of this application's
authentication. It is the accepted floor of email-based recovery and the reason
`REQUIRE_EMAIL_VERIFICATION` and a second factor stay on the roadmap (SEC-0023).

**`ENCRYPTION_KEY` has no rotation path.** Stored blobs are encrypted under one
key and nothing re-encrypts them, so rotating means every household re-links
every institution — least welcome at exactly the moment a rotation is most
needed. Recorded as OPS-0025 rather than left implied.

## Alternatives considered

**Move the dashboard to `/dashboard`.** Rejected on cost — §1.

**Make `/register` 404 when closed.** Rejected — see Consequences.

**Two token tables.** Rejected — §4.

**Validate the reset token on GET and spend it on POST.** Rejected — §5. It is
the friendlier behaviour and it keeps a stolen link live across a window nobody
controls.

**Sign the user in after a successful reset.** Rejected. It is the one flow whose
premise is "somebody else may have your credentials", and ending it by handing
out a session — without anybody having typed the new password once — makes a
stolen link a session rather than a password change the owner notices.

**Require the current password to sign out everywhere.** Rejected. That control
only ever *removes* access, so the worst an attacker at an unlocked screen
achieves is signing everybody out — which is what the real owner would do on
discovering them. Demanding a password would mean the person who most needs the
button is the one who has to stop and remember something first. The *password
change* does require it, because that is the operation that locks the real owner
out.

**Implement Redis now.** Rejected — §7. The interface is the part that has to
exist before the call sites; the backend is a config change afterwards.
