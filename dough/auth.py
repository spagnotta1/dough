"""Who are you, are you allowed to do this, and did you mean to.

Phase 6. `dough/tenancy.py` answers *which rows may you see*; this module
answers the three questions that sit in front of it:

1. **Are you signed in?** — and if not, does this caller want a login page or a
   401 it can act on.
2. **May you do this, given your role?** — `owner_required`.
3. **Did you actually mean to send this request?** — CSRF.

None of the three is a tenancy question, which is why they live apart: a
member of household 7 acting on household 7's data passes every check in
`tenancy.py` while still not being allowed to remove the household's owner.

## Default-deny, twice

Two allowlists, both fail-closed, both derived from the view function itself
rather than from a list somewhere else that has to be kept in step:

- `@public` — the view may run without a session. Anything unmarked requires
  one. The previous design was an endpoint-name allowlist in `app.py`, which is
  fail-*open* in the way that matters: it is not the route you remember to add
  that leaks, it is the one you forget to think about.
- `@csrf_exempt` — an unsafe method may arrive without a token. Everything
  else must carry one, including the routes that are `@public`. Login is a
  state-changing request: an unprotected one lets an attacker sign a victim's
  browser into the *attacker's* account, where anything the victim then uploads
  lands in a ledger the attacker can read.

Both markers are attributes on the view, so `tests/test_route_guard.py` can
enumerate them from `app.view_functions` and compare against what it expects.
A test that reads a constant instead would pass while the constant drifted.

## Why the CSRF token is session-bound and not double-submit

Double-submit (a cookie the JavaScript echoes into a header) is defeated by any
subdomain that can set a cookie on the parent domain. This app stores the token
in the signed session, so forging one means forging the session, which is the
thing the whole scheme already depends on. `Origin` / `Sec-Fetch-Site` are
checked as well, and neither is trusted alone — `Origin` is absent on some
legitimate same-origin requests, and `Sec-Fetch-Site` is absent on old browsers.
A missing header is not evidence of anything; a *wrong* one is.

## Credential generation, and why the bump is not a function call

`AppUser.session_version` is a counter every credential is stamped with — the
session cookie carries the value it was signed in under, `api_tokens` stores the
value it was issued under, and both are compared on every request. Raising it
invalidates all of them at once.

It is raised by a `before_flush` listener in this module rather than by whoever
changes a password, and that is the whole design.  [Phase 10.5] An explicit
`invalidate(user)` call is a convention: it works until somebody adds a second
place a password can change — a reset link, an admin action, a CLI — and does
not know the convention exists. Nothing fails when they forget. The account just
keeps honouring credentials obtained with the old password, which is the exact
outcome the mechanism exists to prevent, arriving silently.

So the listener is the rule and there is one exemption, marked at the single
place that earns it: `upgrade_password_hash`, which changes the stored hash
without changing the password. Default-deny again, in the shape this file
already uses twice.

Allowed:   flask, werkzeug, sqlalchemy, stdlib
Must not:  app, models (except late, function-local imports), finance_sync
"""

from __future__ import annotations

import functools
import hmac
import secrets
import time
from urllib.parse import urlsplit

from flask import current_app, flash, jsonify, redirect, request, session, url_for
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session as OrmSession
from werkzeug.exceptions import Forbidden
from werkzeug.security import check_password_hash, generate_password_hash

__all__ = [
    'CSRFError',
    'CSRF_FIELD',
    'CSRF_HEADER',
    'LoginThrottle',
    'PASSWORD_METHOD',
    'SAFE_METHODS',
    'SESSION_CSRF_KEY',
    'SESSION_VERSION_KEY',
    'client_address',
    'csrf_exempt',
    'csrf_field',
    'csrf_token',
    'current_user',
    'hash_password',
    'is_csrf_exempt',
    'is_public',
    'needs_rehash',
    'owner_required',
    'public',
    'session_is_current',
    'unauthorized_response',
    'upgrade_password_hash',
    'validate_csrf',
    'verify_password',
    'wants_json',
]

#: Where the per-session CSRF secret lives. One token per session, rotated
#: whenever the session is cleared (sign-in, sign-out), rather than per form —
#: per-form tokens break the back button and every parallel tab, and buy
#: nothing here because the token is already unguessable and session-bound.
SESSION_CSRF_KEY = '_csrf_token'

#: Methods that may not change state, per RFC 9110. Everything else needs a
#: token. HEAD and OPTIONS are included because a browser can issue them
#: without script, and a route that mutates on GET is a bug to fix in the route
#: rather than something to paper over here.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})

#: The header a `fetch` caller sends its token in. `static/js/dough.js`
#: installs one wrapper that attaches this to every same-origin unsafe request,
#: so the 40-odd existing call sites did not each have to learn about CSRF.
CSRF_HEADER = 'X-CSRF-Token'

#: The form field, for ordinary non-JavaScript submissions.
CSRF_FIELD = '_csrf_token'

#: Where the session records which credential generation it belongs to.
#: `AppUser.session_version` as it stood at sign-in; `current_user` refuses a
#: session whose value no longer matches.  [Phase 10.5]
#:
#: In the signed cookie rather than server-side because there is nowhere
#: server-side to put it — that is what a cookie session means. It is safe
#: there for the same reason `user_id` is: the cookie is signed, so a client
#: that could forge this number could forge the identity it sits next to.
SESSION_VERSION_KEY = '_session_version'


class CSRFError(Forbidden):
    """A state-changing request arrived without a valid token.

    A `Forbidden` subclass rather than a bare exception so an unhandled one is
    a 403 rather than a 500 — the distinction matters to anyone reading logs,
    because a 500 says *we* are broken and a 403 says the request was.
    """

    description = 'This request could not be verified. Please reload and try again.'


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def public(view):
    """Mark a view as reachable without a session.

    The marker goes on the function so the guard reads it from
    `app.view_functions` at request time. Nothing has to remember to also
    update a list.
    """
    view._dough_public = True
    return view


def is_public(view):
    return bool(getattr(view, '_dough_public', False))


def csrf_exempt(view):
    """Mark a view as accepting unsafe methods without a CSRF token.

    Intended for machine callers that authenticate some other way. Exactly one
    view carries it — `/api/v1/auth/login`, which accepts a password and so has
    no session to bind a token to — and `tests/test_csrf.py` pins the set to
    that one name, so adding the next is a deliberate, reviewed act.
    """
    view._dough_csrf_exempt = True
    return view


def is_csrf_exempt(view):
    return bool(getattr(view, '_dough_csrf_exempt', False))


# ---------------------------------------------------------------------------
# Content negotiation: a login page or a 401
# ---------------------------------------------------------------------------

def wants_json():
    """Would this caller rather have JSON than an HTML login page?

    The failure this fixes: `fetch('/rules', {method: 'POST'})` on an expired
    session got a 302, followed it, received 200 with a login page in the body,
    and handed that to `.json()`. The caller saw a parse error, not an auth
    error, so the UI reported that saving a rule was broken.

    The order of the checks is the whole design:

    - `/api/*` is machine-facing by construction, whatever it says it accepts.
    - `Sec-Fetch-Mode: navigate` is a real page load. It wins over everything
      below, so a form post keeps getting a redirect the browser can follow.
    - An explicit `Accept` preference is honoured only when it actually
      expresses one. `Accept: */*` — what a bare test client and curl send —
      is not a preference, and treating it as one would turn every unadorned
      request into a 401.
    """
    if request.path.startswith('/api/'):
        return True
    if request.headers.get('Sec-Fetch-Mode') == 'navigate':
        return False
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.accept_mimetypes
    if not accept.provided:
        return False
    return accept.best_match(['text/html', 'application/json']) == 'application/json'


def unauthorized_response():
    """What an unauthenticated request gets: 401 JSON, or a redirect to login.

    Under `/api/v1` the 401 is the versioned envelope rather than the bare
    `{'error': ...}` the older endpoints answer with.  [Phase 10] A client of a
    versioned API must be able to parse *every* response the same way, and the
    one it is most likely to meet first is this one -- a request sent before it
    has a credential.

    The import is function-local for the same reason `current_user`'s is:
    `dough/api/guard.py` imports `SAFE_METHODS` from this module, so a
    module-level import here would close the loop.
    """
    if wants_json():
        from dough.api.errors import ErrorCode, api_error_response, is_api_request

        if is_api_request():
            return api_error_response(
                401, ErrorCode.UNAUTHENTICATED,
                'Authentication is required. Send an API token as '
                '`Authorization: Bearer <token>`.')
        return jsonify({'error': 'authentication required'}), 401
    return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))


# ---------------------------------------------------------------------------
# Saying why the login page is on screen
# ---------------------------------------------------------------------------
#
# Four ways a browser arrives at `/login`, and until now they were indis-
# tinguishable once you got there: one of them is a button the person pressed
# and three of them are the application deciding for them. An unexplained login
# form is read as "it logged me out again" whichever it was, and the security
# ones are exactly the cases where somebody needs to know something happened.
#
# The wording lives here rather than at the call sites because there are five of
# those across two modules, and the same event described two ways reads as two
# different events.

#: The person pressed Sign out.
SIGN_OUT_DELIBERATE = 'deliberate'
#: An idle or absolute session limit ran out. Nothing is wrong.
SIGN_OUT_EXPIRED = 'expired'
#: `session_version` moved past what this session was minted under -- today,
#: that means the account's password changed. Anyone else holding a session for
#: this account was signed out at the same moment, which is the point of it.
SIGN_OUT_CREDENTIALS = 'credentials'
#: The AppUser row is gone: removed from the household while signed in.
SIGN_OUT_ACCOUNT_GONE = 'account_gone'

_SIGN_OUT_NOTICES = {
    SIGN_OUT_DELIBERATE: (
        'success', "You've been signed out successfully."),
    SIGN_OUT_EXPIRED: (
        'info', 'Your session has expired. Please sign in again.'),
    SIGN_OUT_CREDENTIALS: (
        'warning', 'Your password was changed, so every session using the old '
                   'one was signed out. Please sign in again.'),
    SIGN_OUT_ACCOUNT_GONE: (
        'warning', 'This account is no longer active. Ask whoever runs the '
                   'household if you think that is a mistake.'),
}


def notify_signed_out(reason):
    """Leave a message on the login page explaining why it is being shown.

    **Call this after `session.clear()`, never before.** Flask keeps flashes in
    the session, so a message queued first is wiped by the very call that signs
    the person out, and the login page renders silently — which is the bug this
    function exists to prevent, reintroduced. Clearing and then flashing leaves
    a session holding nothing but the message, which is what should survive.

    Silent for callers that want JSON. `unauthorized_response` answers those
    with a 401 they can act on, and a flash queued alongside it would be read by
    whichever page the browser loaded next — arriving with no relation to
    anything the person had just done.
    """
    if wants_json():
        return
    category, message = _SIGN_OUT_NOTICES[reason]
    flash(message, category)


def sign_out_reason(user_id):
    """Which of the two involuntary sign-outs `current_user() is None` meant.

    `current_user` collapses "the row is gone" and "the credential generation
    moved" into one None, correctly — neither session can be repaired, only
    replaced, so the *handling* is identical. The two are not the same thing to
    read on a login page, though: one says somebody changed the password on this
    account, the other says the account is not there any more.

    Read before `session.clear()`, because it needs the id the session is
    carrying.
    """
    from models import AppUser

    if not user_id:
        return SIGN_OUT_ACCOUNT_GONE
    return (SIGN_OUT_ACCOUNT_GONE if AppUser.query.get(user_id) is None
            else SIGN_OUT_CREDENTIALS)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_token():
    """This session's token, minted on first use.

    Exposed to Jinja as `csrf_token()`, so a template renders the hidden field
    without the view having to pass anything down.
    """
    token = session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF_KEY] = token
    return token


def csrf_field():
    """The hidden input, as markup, for `{{ csrf_field() }}` in a template.

    A helper rather than fourteen copies of the same `<input>`: the field name
    is a protocol detail shared with `_submitted_token`, and the two are now
    impossible to change apart.
    """
    from markupsafe import Markup
    return Markup('<input type="hidden" name="{}" value="{}">').format(
        CSRF_FIELD, csrf_token())


def _submitted_token():
    """The token this request offered, from header or form field.

    Not from the JSON body. A token read out of `request.json` would have to be
    stripped before every handler that parses its own payload, and forgetting
    once means a stray key in whatever that route writes. The header is the
    channel for JSON callers, and a cross-site request cannot set it.
    """
    return request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD)


def _origin_is_foreign():
    """True only when a request *states* an origin and it is the wrong one.

    Absence proves nothing — `Origin` is omitted on same-origin GETs by some
    browsers and `Sec-Fetch-Site` does not exist on older ones — so a missing
    header falls through to the token check rather than failing here. This is a
    second signal, not the mechanism.
    """
    site = request.headers.get('Sec-Fetch-Site')
    if site and site not in ('same-origin', 'none'):
        return True
    origin = request.headers.get('Origin')
    if not origin:
        return False
    if origin == 'null':
        # A sandboxed iframe or a `file://` page. Never legitimate here.
        return True
    return urlsplit(origin).netloc != urlsplit(request.host_url).netloc


def validate_csrf():
    """Raise `CSRFError` unless this request carries this session's token.

    Comparison is `hmac.compare_digest`: the timing leak on a 43-character
    random token is not a practical attack, but the habit is worth more than
    the microseconds, and the alternative is one `==` that nobody re-reads.
    """
    if _origin_is_foreign():
        raise CSRFError()
    expected = session.get(SESSION_CSRF_KEY)
    submitted = _submitted_token()
    if not expected or not submitted:
        raise CSRFError()
    if not hmac.compare_digest(str(expected), str(submitted)):
        raise CSRFError()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def owner_required(view):
    """Refuse the view unless the signed-in user owns their household.

    Deliberately a route-layer check and not something the ORM backstop could
    do for us. `tenancy.py` knows which *household* a row belongs to; it has no
    idea which *person* is asking, and a member of household 7 removing
    household 7's owner never crosses a tenant boundary. ADR-0008's second
    constraint applies here too: this is the load-bearing half, and
    `tests/test_route_guard.py` exercises it with the tenancy backstop off.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None or not user.is_owner:
            if wants_json():
                return jsonify({'error': 'owner permission required'}), 403
            raise Forbidden('Only a household owner can do that.')
        return view(*args, **kwargs)

    wrapper._dough_owner_required = True
    return wrapper


def current_user():
    """Who this request acts as — from an API token or a session — or None.

    Reading the bearer actor first is what makes `owner_required` and every
    other role check work identically on both surfaces.  [Phase 10] Without it,
    a token-authenticated request would find no user and be refused as though it
    were anonymous, which is the wrong answer to a credential that has already
    been verified.

    The explicit credential wins over the session for the reason
    `_household_for_request` gives: a request may carry both, and the header is
    the one the caller deliberately attached.

    Both imports are function-local. `models` imports `dough.tenancy` and would
    close a loop; `dough/api/guard.py` imports `SAFE_METHODS` from this module
    and would close another.
    """
    from dough.api.guard import bearer_actor

    actor = bearer_actor()
    if actor is not None:
        return actor

    from models import AppUser
    uid = session.get('user_id')
    if not uid:
        return None
    user = AppUser.query.get(uid)
    if user is None or not session_is_current(user):
        return None
    return user


def session_is_current(user):
    """Was this session minted under the account's current credential generation?

    False means the account has been invalidated since — today, that its
    password changed.  [Phase 10.5] `current_user` then answers None, and
    `app.py`'s `_enforce_session_lifetime` clears the cookie and sends the
    person to the login page, which is the same handling a deleted user already
    got and the right one here: the session cannot be repaired, only replaced.

    A session with no version recorded at all is refused. That is fail-closed
    and it costs one sign-in per browser when this ships, because cookies minted
    before it exist do not carry the key. Accepting them instead would exempt
    every pre-existing session from the mechanism permanently, and those are
    precisely the long-lived ones — an absolute lifetime is measured in days,
    so "it will age out" is not an argument that holds for the session somebody
    is worried about.
    """
    recorded = session.get(SESSION_VERSION_KEY)
    return recorded is not None and recorded == user.session_version


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

#: Werkzeug 3 already defaults to scrypt, which is memory-hard. Naming it
#: anyway means a werkzeug upgrade that changed the default would change this
#: file — a reviewable diff — rather than silently changing how every password
#: in the database is protected. `tests/test_auth.py` pins the stored prefix.
PASSWORD_METHOD = 'scrypt:32768:8:1'


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_METHOD)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


def needs_rehash(password_hash):
    """True if this hash predates the current KDF and should be upgraded.

    Called on a successful sign-in, which is the only moment the plaintext
    exists to re-derive from. A stored pbkdf2 hash from before this phase is
    not a vulnerability that can be fixed in a migration.
    """
    return not (password_hash or '').startswith(PASSWORD_METHOD.split(':')[0] + ':')


def upgrade_password_hash(user, password):
    """Re-derive a stale hash under the current KDF. True if it was upgraded.

    One implementation, called by both sign-in paths.  [Phase 10] It was inline
    in `dough/blueprints/auth.py` and was copied into `dough/api/v1/auth.py`
    when the API login was written -- which is the duplication
    `tests/test_services.py::test_api_resource_holds_no_business_logic` exists
    to catch, and it caught it.

    Sharing this matters more than the four lines suggest. Skipping the upgrade
    on one path means an account that only ever signs in from that surface keeps
    an obsolete hash indefinitely, and nothing would report it: both logins
    succeed either way.

    The caller records the audit event rather than this function, because the
    two surfaces attribute it differently and this module has no opinion about
    which one is asking.
    """
    if not needs_rehash(user.password_hash):
        return False

    from models import db

    user.password_hash = hash_password(password)
    # The one exemption from the listener below, and the only place in the
    # application entitled to set it.  [Phase 10.5] This changes the stored hash
    # without changing the password, so nothing about the account's credentials
    # has been superseded: the person just proved they still know it. Bumping
    # here would sign every one of their devices out on the sign-in that
    # upgraded their hash -- an invisible, once-per-account logout that would
    # look like a bug and be blamed on anything but this.
    #
    # It cannot be inferred instead of marked. A rehash goes old-KDF -> new-KDF,
    # and so does a password *change* made by somebody whose hash was stale, so
    # the two are indistinguishable from the values alone. Guessing would be
    # wrong in the direction that leaves credentials alive.
    user._dough_rehash_only = True
    db.session.commit()
    return True


@event.listens_for(OrmSession, 'before_flush')
def _bump_session_version_on_password_change(db_session, flush_context, instances):
    """Raise `session_version` whenever a stored password is replaced.

    A `before_flush` listener for the same reason `dough/tenancy.py` puts its
    write guard there: it is the last point at which every pending change is
    visible and can still be added to, so a rule stated here holds for every
    caller — including ones written later that have never heard of it.

    Only `session.dirty` is examined. A *new* `AppUser` carries a password_hash
    too, and bumping it would be meaningless: nothing has been issued against an
    account that does not exist yet, and the row's default already says 1.

    The listener is registered on the generic `sqlalchemy.orm.Session` rather
    than on Flask-SQLAlchemy's, so it is live in any session this process opens
    — including the ones `tools/` scripts and a shell use, which are exactly the
    contexts where somebody resets a password by hand and no route code runs at
    all.

    Nothing is audited from here. `audit.record` writes through the session, and
    doing that inside a flush is a re-entrant flush; the event is recorded by
    whichever caller changed the password, which is also the only party that
    knows *why* it changed.
    """
    from models import AppUser

    for obj in db_session.dirty:
        if not isinstance(obj, AppUser):
            continue
        if not sa_inspect(obj).attrs.password_hash.history.has_changes():
            continue
        if getattr(obj, '_dough_rehash_only', False):
            # Cleared as it is consumed, so the exemption covers exactly the one
            # flush it was set for. Leaving it set would silently exempt the
            # next password change made against the same in-memory instance.
            obj._dough_rehash_only = False
            continue
        obj.session_version = (obj.session_version or 1) + 1


# ---------------------------------------------------------------------------
# Failed-attempt throttling
# ---------------------------------------------------------------------------

class LoginThrottle:
    """Rate-limits failed sign-ins by source address *and* by account.

    Two buckets, because they stop different attacks and neither covers the
    other. The address bucket stops one host walking a password list. The
    account bucket stops the distributed version, where every attempt comes
    from a different address and the address bucket never fills — which is what
    credential-stuffing against a known username actually looks like.

    The account bucket is a denial-of-service surface by construction: anyone
    who knows a username can lock it. Its threshold is therefore higher and its
    window shorter than the address bucket's, and the lockout is silent — the
    caller is told the credentials are wrong, which is both true and free of
    the "this account exists" signal a distinct message would leak.

    In-memory, so it resets on restart and does not span processes. That is a
    real limitation and it is recorded in `docs/security.md` rather than fixed
    here; a shared store is a dependency this application does not otherwise
    have.
    """

    def __init__(self, *, address_limit=5, address_window=900,
                 account_limit=10, account_window=300, clock=time.time):
        self.address_limit = address_limit
        self.address_window = address_window
        self.account_limit = account_limit
        self.account_window = account_window
        self._clock = clock
        self._by_address = {}
        self._by_account = {}

    def _recent(self, bucket, key, window):
        now = self._clock()
        kept = [t for t in bucket.get(key, []) if now - t < window]
        if kept:
            bucket[key] = kept
        else:
            bucket.pop(key, None)
        return kept

    def lock_reason(self, address, username):
        """None, `'address'`, or `'account'` — and the caller must tell them apart.

        The two locks may not produce the same message. "Too many failed
        attempts" is the right thing to say about an *address* lock: the person
        reading it is almost always the one who caused it, it leaks nothing
        (the counter is keyed on where the request came from, not on whether
        any account exists), and the alternative leaves someone staring at
        "invalid password" while typing the correct one.

        An *account* lock is the opposite. Saying anything specific confirms
        that this username exists, which is exactly what someone walking a list
        of usernames is trying to find out, so that case is told the same thing
        a wrong password is told.
        """
        if len(self._recent(self._by_address, address, self.address_window)) \
                >= self.address_limit:
            return 'address'
        if username and len(self._recent(self._by_account, username.lower(),
                                         self.account_window)) >= self.account_limit:
            return 'account'
        return None

    def is_locked(self, address, username):
        return self.lock_reason(address, username) is not None

    def record_failure(self, address, username):
        now = self._clock()
        self._by_address.setdefault(address, []).append(now)
        if username:
            self._by_account.setdefault(username.lower(), []).append(now)

    def record_success(self, address, username):
        """Clear both buckets.

        The account bucket is cleared too, so a legitimate sign-in ends a
        lockout somebody else provoked. An attacker cannot use this: reaching
        it requires the password.
        """
        self._by_address.pop(address, None)
        if username:
            self._by_account.pop(username.lower(), None)


def client_address():
    """The caller's address, honouring `X-Forwarded-For` only when configured.

    `request.remote_addr` behind a reverse proxy is the proxy, so every caller
    shares one bucket and the throttle either never fires or locks everyone out
    at once. The fix is not to read `X-Forwarded-For` — that header is
    attacker-controlled, and trusting it turns the throttle into a no-op, since
    a new fake address per attempt means a fresh bucket per attempt.

    So it is read only when `TRUSTED_PROXIES` says how many hops are ours, and
    only that many entries from the right-hand end are believed.
    """
    hops = current_app.config.get('TRUSTED_PROXIES', 0)
    if hops:
        forwarded = request.headers.get('X-Forwarded-For', '')
        chain = [part.strip() for part in forwarded.split(',') if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.remote_addr or 'unknown'
