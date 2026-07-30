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

Allowed:   flask, werkzeug, stdlib
Must not:  app, models (except late, function-local imports), finance_sync
"""

from __future__ import annotations

import functools
import hmac
import secrets
import time
from urllib.parse import urlsplit

from flask import current_app, jsonify, redirect, request, session, url_for
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
    'unauthorized_response',
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

    Intended for machine callers that authenticate some other way. Nothing
    uses it yet, and `tests/test_route_guard.py` asserts the exempt set stays
    empty, so adding the first one is a deliberate, reviewed act.
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
    """What an unauthenticated request gets: 401 JSON, or a redirect to login."""
    if wants_json():
        return jsonify({'error': 'authentication required'}), 401
    return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))


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
    """The signed-in `AppUser`, or None.

    The import is function-local for the same reason it is in `tenancy.py`:
    `models` imports `dough.tenancy`, and a module-level import here would
    close the loop the moment anything in `models` wanted an auth helper.
    """
    from models import AppUser
    uid = session.get('user_id')
    if not uid:
        return None
    return AppUser.query.get(uid)


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
