# ADR-0013 — One generation counter invalidates every credential

- **Status:** Accepted
- **Date:** 2026-07-30
- **Phase:** 10.5
- **Extends:** ADR-0009 (authentication) with a lifecycle for the session;
  ADR-0012 (the versioned API) with the same lifecycle for its bearer tokens.
- **Supersedes:** nothing.

## Context

After Phase 10 this application had two kinds of credential and no way to
withdraw either one in response to a change to the account behind it.

Revocation existed, but only per object. An owner could revoke a *particular*
API token, and a person could clear *their own* browser session by signing out.
Neither is the operation that matters after a password changes, which is "and
everything else this account has issued". The only lever that did that was
rotating `SECRET_KEY`, which signs the entire installation out and is therefore
a lever nobody pulls.

This was latent rather than exploited, because there is no password-change route
in this application. Nothing could change a password, so nothing could strand a
credential behind one.

That is the whole reason it was worth doing before the next phase rather than
alongside the feature that needs it. A missing mechanism is a gap somebody can
see while writing the route. A mechanism that exists but is never called is a
gap that looks like working code — and the failure mode is silent in the unsafe
direction: the password changes, the old token keeps working, and every
observable signal says the change succeeded.

The same argument is why it was not deferred to Phase 11+. Each phase after this
adds endpoints that a token can reach; the cost of retrofitting an invalidation
rises with the number of things that would need to have been thinking about it,
and none of them would have been.

## Decision

### 1. A generation counter on the account, copied onto each credential

`AppUser.session_version` is an integer every credential is stamped with at
creation. The session cookie carries the value it was signed in under; each
`api_tokens` row stores the value it was issued under. Both are compared against
the account's current value on every request. Raising the counter invalidates
every credential of every kind at once, with one write.

Two stores rather than one because the session is not ours to store — it lives
in the user's cookie, which is what makes the value travel there.

The comparison is equality, not `>=`. A credential claiming a generation from
the future is as invalid as one claiming a generation from the past; a
comparison would let a tampered value outlive every subsequent change.

### 2. The bump is a `before_flush` listener, not a call

`dough/auth.py::_bump_session_version_on_password_change` raises the counter
whenever `AppUser.password_hash` changes, in any `sqlalchemy.orm.Session` this
process opens — a route, a `tools/` script, a shell.

The alternative was an explicit `credentials.invalidate(user)` that each caller
invokes. It reads better and it is wrong here, for the reason ADR-0008 gives
about the tenancy backstop and `dough/auth.py` gives about `@public`: an
invariant maintained by every caller remembering is an invariant nobody can
check. It holds until the second place a password can change gets written — a
reset link, an admin action, a migration that repairs a bad hash — by somebody
who has not read this file. Nothing fails when they forget.

Registered on the generic `Session` rather than Flask-SQLAlchemy's, because the
contexts where somebody resets a password by hand are precisely the ones with no
request and no route code running.

Nothing is audited from the listener. `audit.record` writes through the session,
so doing it inside a flush is a re-entrant flush; the event belongs to whichever
caller changed the password, which is also the only party that knows why.

### 3. One exemption, marked at the site that earns it

`upgrade_password_hash` replaces a stale hash on a successful sign-in without
the password having changed. It sets a marker the listener consumes.

This cannot be inferred instead of marked, and the reason is worth recording
because the inference is tempting: a rehash goes old-KDF → new-KDF, but so does
a password *change* made by somebody whose stored hash was stale. The two are
indistinguishable from the values alone, and guessing would be wrong in the
direction that leaves credentials alive.

Without the exemption, the first sign-in of any account still holding a
pre-scrypt hash would revoke every credential that account held — once,
invisibly, and blamed on anything but the rehash.

### 4. Invalidation is lazy; nothing sweeps `api_tokens`

A password change does not stamp `revoked_at` across the account's tokens.

Eager revocation was the obvious design and was rejected on failure behaviour
rather than cost: a sweep is a second write, and a second write can be lost — a
crash between the bump and the sweep, an exception in the middle of the update —
so a token could survive an invalidation by having been *missed*. A comparison
against a row the request already loaded cannot be missed.

The price is that a superseded token still looks issued in the table, with a
null `revoked_at`. `ApiToken.state()` pays it by reporting `'stale'`, which is
also the honest answer for a token whose user has been deleted. The state
vocabulary is closed (`ApiToken.STATES`) and pinned against the OpenAPI enum, so
adding a fifth state cannot silently outrun the spec.

### 5. A session with no recorded generation is refused

Fail-closed. Cookies minted before this shipped do not carry the key, so
deploying it costs one sign-in per browser.

Accepting an absent value would have avoided that and exempted every
pre-existing session from the mechanism permanently. Those are the long-lived
ones — the absolute session lifetime is measured in days — so "they will age
out" is not an argument that covers the session anybody would be worried about.

## Consequences

**What this buys.** A single, checkable statement: a credential is valid only if
it belongs to the account's current generation. It holds for both surfaces, it
holds for code written after this, and it holds for writes made outside the
application entirely.

**What it does not buy.** There is still no user-facing way to trigger it. No
password-change route exists, and no "sign out everywhere" control. Both are now
small — the password change is automatic, and a deliberate mass sign-out is one
assignment — but neither is built, and the mechanism is until then reachable
only by an operator writing to the model. This is recorded as the residual on
SEC-0019 rather than described as done.

**What was deliberately left alone.** Role changes and household removal do not
bump the counter. They do not need to: `api_tokens.authenticate` re-reads the
user on every request, which is what makes a demoted owner's token lose owner
powers rather than stop working, and a removed member's token fail closed. Using
the counter there would replace a live check with a stamped one and lose that
property.

**Cost per request.** Nothing measurable. Both comparisons read a row the
request had already loaded — the session path loads the user to check it exists,
the bearer path loads the user because a demoted owner's token must demote with
them.

**Downgrade.** `20260730_06` drops both columns, which removes the enforcement
along with the schema: credentials invalidated while it was in place become
usable again. That is unusual for an add-column migration and is stated in the
migration's docstring rather than left to be discovered.
