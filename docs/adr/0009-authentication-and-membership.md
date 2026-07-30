# ADR-0009: One person, one household — and the marker travels with the view

- **Status:** Accepted
- **Date:** 2026-07-27
- **Phase:** 6

## Context

ADR-0008 made a household the unit of isolation and left it with exactly one way
to gain a member: `/setup`, which runs once, on an empty installation. A
two-person household was expressible in the schema and unreachable through the
product.

Three findings were also sitting open in `docs/security.md` waiting for this
phase, and they turn out not to be parallel work. SEC-0002 — no CSRF protection
of any kind — is a *prerequisite* for invitations rather than a sibling of them.
Today the worst a forged cross-site request can do is corrupt the caller's own
data. After invitations it can change who is in a household: forge
`POST /household/invites` from an owner's browser and the attacker is in;
forge `POST /join/<token>` and the victim's browser is signed into the
attacker's household, where everything they subsequently connect or upload lands
in a ledger somebody else reads.

So the ordering inside the phase is: security primitives first, membership
second.

## Decision

### 1. A person belongs to exactly one household

The alternatives were considered and rejected in this order.

**Leave-and-join** — an existing user accepting an invitation leaves their
current household — produces more edge cases than features. If they were its
only owner, the household they left is unadministerable, and their data stays
there with nobody able to reach it. That is a data-loss shape dressed as a
convenience.

**A membership join table** is the honest way to model somebody in two
households, and ADR-0008 already recorded why it was not built: it is a product
decision nobody has made, adding it later is a migration, and removing an unused
one that authorization code has learned to consult is not.

So: an invitation link lands on a signup form, and redeeming it creates a *new*
`AppUser` directly in the inviting household. A signed-in visitor following a
link is turned away with an explanation rather than silently switched. The
realistic case — one partner sets up, invites the other — is covered exactly.
The case that is not covered is two people who each already set up separately
and now want to merge, which is a household *merge*: a genuinely hard operation
involving duplicate accounts and overlapping transactions, and deliberately out
of scope rather than half-implemented.

### 2. Default-deny twice, and the marker lives on the view

`@public` replaces the endpoint-name allowlist that used to sit in `app.py`.
The allowlist was fail-*open* in the way that matters: it is not the route you
remember to add that leaks, it is the one you never thought about, and the list
lived in a different file from the routes it described.

`@csrf_exempt` is the same shape for the second question, and nothing uses it.
`tests/test_route_guard.py` pins the exempt set at empty, so adding the first
exemption is a reviewed act rather than a quick way to make a failing test pass.

Both markers are attributes on the view function, so the tests enumerate them
from `app.view_functions` and compare against a stated expectation. A test that
read a constant instead would pass while the constant drifted from the code.

### 3. Authentication is checked before CSRF

Ordering decides what an anonymous POST is told. Checking CSRF first answers
403 "could not be verified" to a caller whose actual problem is an expired
session — misleading, and it would undo the 401 negotiation that SEC-0006
exists to provide. A request with no session has no session to forge.

Being `@public` exempts a route from needing a session, never from needing a
token. `/login` and `/setup` are both state-changing.

### 4. The CSRF token is session-bound, and `Origin` is a second signal

Double-submit — a cookie the JavaScript echoes back in a header — is defeated by
any subdomain able to set a cookie on the parent domain. The token lives in the
signed session instead, so forging one means forging the session, which is what
the whole scheme already rests on.

`Origin` and `Sec-Fetch-Site` are checked as well, and neither is trusted alone.
A *missing* header proves nothing: several browsers omit `Origin` on same-origin
requests and `Sec-Fetch-Site` did not exist before 2020. A *wrong* one is
evidence. Getting that backwards is how a CSRF layer breaks real clients.

`base.html` patches `window.fetch` once rather than editing the ~40 call sites,
for the usual reason: the call site that matters is the one added next, and it
inherits the header automatically. Plain forms cannot be helped that way, so
`{{ csrf_field() }}` is in each of the fourteen POST forms and
`tests/test_csrf.py` reads the templates to catch the fifteenth.

### 5. The last-owner rule is enforced by writing first and counting after

A household with no owner is not recoverable through the UI — nobody could
invite, rename, or promote anyone. `tools/verify_tenancy.py` already reported it
as a failure; nothing prevented it.

Reading the owner count and then deciding is check-then-act: two owners demoting
each other at the same moment each see the other still in place, and both
succeed. `dough/services/membership.py` flushes the change and *then* counts, so
the count includes its own uncommitted write and one of the two transactions
sees zero and rolls back. SQLite's single writer makes this hard to hit today,
which is precisely why it was worth writing the version that survives the
database changing.

### 6. Role checks are route-layer, and the ORM cannot help

ADR-0008's second constraint applies here with more force than it did to
tenancy. A member of household 7 removing household 7's owner **never crosses a
tenant boundary** — every row involved belongs to the household doing the
asking — so there is no version of the ORM backstop that could catch it. Roles
are checked by `@owner_required` on the view and nowhere else, and
`tests/test_membership.py` pairs every refusal with the permission that proves
the refusal meant something.

### 7. Invitation tokens are stored as a bare SHA-256

The link is a bearer credential: holding it is the entire authorization. Storing
plaintext would mean a backup, a stray `SELECT *` or a log line yields working
invitations into a family's finances.

No salt and no work factor, which is the opposite of the advice for passwords
and correct for the same reason it is wrong there. The input is 256 bits from
`secrets.token_urlsafe` — nobody chose it, so there is no dictionary to
precompute — and a work factor would only slow down the person redeeming a
legitimate link.

The plaintext is returned once, rendered once from a one-shot session value, and
never stored. "Shown once" is therefore a fact about the system rather than a
policy about the page.

## Consequences

**Good.** SEC-0002 and SEC-0006 are closed and SEC-0004 is narrowed; SEC-0005
turned out to be stale (werkzeug 3 already defaults to scrypt) and is now pinned
by a test rather than left to a default that has moved before. The session
lifetime keys declared in `config.py` in Phase 1 do something at last. A latent
500 — a session whose user has been deleted — is fixed by the same phase that
makes deleting a user possible.

**Bad.** `app.py` grew by about 160 lines and the line-count guard in
`tests/test_services.py` had to be raised from 2,700 to 2,900. That is honest
here (the growth is six new routes, and the membership *rules* went into a
service) but the number cannot keep moving. The remaining bulk is what the
planned blueprint extraction is for, and `tests/test_url_map_snapshot.py` exists
to make that safe.

`/join/<token>` is the second entry in the `grep -rn 'unscoped()'` audit that
ADR-0008 asked to stay short. It is legitimate — the token is what names the
household, so the lookup necessarily precedes binding one — and the route binds
`tenant_scope(invite.household_id)` immediately afterwards, so the write that
consumes the invitation is ordinary scoped work.

**Accepted risk.** The login throttle's account bucket is a denial-of-service
surface by construction: anyone who knows a username can fill it. Its threshold
is higher and its window shorter than the address bucket's, and a successful
sign-in clears it, so the exposure is bounded rather than removed. Both buckets
are in-memory, so they reset on restart and do not span processes — recorded in
`docs/security.md` rather than fixed, because a shared store is a dependency
this application does not otherwise have.

`X-Forwarded-For` is ignored unless `TRUSTED_PROXIES` says how many hops are
ours. An operator who deploys behind a proxy without setting it gets one shared
throttle bucket for every caller; one who sets it too high gets no throttle at
all, because a forged address per attempt is a fresh bucket per attempt. The
default of 0 fails toward the first, which is the recoverable one.
