"""Authenticating a bearer token, and the three hooks that then behave differently.

Allowed:   flask, dough.api.*, dough.services.api_tokens, dough.services.audit
Must not:  models at module scope (function-local only, as `dough/auth.py` does)

## Where this sits

`create_app` runs its `before_request` handlers in registration order:

    logging -> **this** -> require_login -> verify_csrf -> bind_tenant -> view

It has to be first of the authentication hooks, because the two after it both
ask questions whose answer changes when a bearer token is present:

- `_require_login` looks for `session['user_id']`. A bearer client has no
  session and never will, so without this it would be redirected to a login page
  it cannot render.
- `_verify_csrf` demands a token bound to the session. There is no session to
  bind one to, and demanding one would make the API unusable by design.

Both consult `bearer_actor()` rather than this module reaching into them, so the
reasoning stays readable from `app.py`, which is where a person goes to find out
what happens to a request.

## Why skipping CSRF here is safe, and would not be elsewhere

CSRF exists because a browser attaches cookies to cross-site requests
*automatically*. That is the entire mechanism: the credential travels without
the attacking page having to know it. An `Authorization` header is the opposite
— it is never attached automatically, and a cross-origin page cannot set one
without a CORS preflight this application never grants. So a request carrying a
valid bearer token is, by construction, a request whose sender knew the
credential. There is nothing left for a CSRF token to prove.

The condition is therefore *authenticated by bearer token*, never *path starts
with /api*. Exempting a path would exempt the same routes when a browser reached
them with a session cookie, which is precisely the hole CSRF closes. This is
also why `@csrf_exempt` is applied to exactly one view (`auth.login`, which has
no session to protect yet) rather than to the blueprint.

## Scope enforcement

Unsafe methods require `write`. Checked here, once, rather than per route: a
route added later inherits the requirement instead of needing to remember it,
which is the same default-deny argument `dough/auth.py` makes about `@public`.
A route needing something narrower says so with `@require_scope`.
"""

from __future__ import annotations

from flask import current_app, g, has_app_context, request

from dough.api.errors import (API_PREFIX, ErrorCode, InsufficientScope,
                              RateLimited, api_error_response)
from dough.auth import SAFE_METHODS
from dough.services import api_tokens

__all__ = [
    'BEARER_SCHEME',
    'authenticate_bearer',
    'bearer_actor',
    'bearer_token',
    'enforce_rate_limit',
    'install',
    'require_scope',
]

BEARER_SCHEME = 'bearer'

#: Where the resolved identity lives for the rest of the request. On `g` rather
#: than in the session, deliberately: writing to the session would set a cookie
#: on an API response, which is both useless to a native client and a way for a
#: bearer request to acquire a second, longer-lived credential it never asked
#: for.
_ACTOR_KEY = '_dough_api_actor'
_TOKEN_KEY = '_dough_api_token'


def bearer_actor():
    """The `AppUser` this request authenticated as via bearer token, or None.

    Read by `app.py`'s login, CSRF and tenancy hooks, and by
    `dough.auth.current_user`. Returns None for every session-authenticated
    request, so the presence of a value is exactly the statement "this request
    came from an API client".

    Guarded with `has_app_context` rather than assuming one. `dough.auth.
    current_user` calls this, and that function is reachable from a CLI command
    and from a test with no context at all — where an unguarded `g` raises
    RuntimeError from inside what looks like a simple lookup.
    """
    return g.get(_ACTOR_KEY) if has_app_context() else None


def bearer_token():
    """The `ApiToken` row backing this request, or None."""
    return g.get(_TOKEN_KEY) if has_app_context() else None


def _presented_token():
    """The credential this request offered, or None.

    Only the `Authorization` header. A token in the query string is refused by
    omission, and that is a decision rather than an oversight: query strings are
    written to access logs by every proxy in the world, kept in browser history,
    and sent in `Referer`. There is no way to use one safely, so there is no
    code here that would accept one.
    """
    header = request.headers.get('Authorization', '')
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return None
    return parts[1].strip()


def authenticate_bearer():
    """`before_request`: resolve a bearer token, or refuse if it is bad.

    Returns None in two very different cases and it is worth being explicit
    about both:

    - **No token offered.** Not this hook's business. The request falls through
      to the session-based chain, which is what lets the web UI call `/api/v1`
      with a cookie and lets `AUTH_ENABLED=False` keep working under test.
    - **A good token.** The identity is on `g` and the later hooks read it.

    A token that is *offered and bad* is refused here rather than falling
    through, and that asymmetry is the important part. Falling through would
    answer a client holding a revoked credential with a redirect to a login
    page — telling it nothing about the actual problem, which is that its token
    needs replacing.

    ## Why the answer is written on every path, including "no token"

    This hook used to only ever *set* the actor, and returned early — without
    writing anything — for a non-API path and for a request offering no
    credential. That made its contract partial: `bearer_actor()` claims to name
    who *this* request authenticated as, and on those two paths it named whoever
    the last request that reached line 146 authenticated as.

    `g` is per-app-context, and in production every request pushes its own, so
    nothing leaked. It leaks the moment an app context outlives a request —
    which is every test fixture in this repository (`conftest.py` and each
    suite's own `app` fixture push one and serve many requests inside it), and
    would also be true of any future CLI command or worker that pushed a context
    and handled more than one thing inside it.

    The symptom was worth the four lines: a session-authenticated request made
    after an API-authenticated one was answered as the *token's* actor, so it
    skipped `session_is_current` entirely and a browser whose credentials had
    been invalidated stayed signed in. It was found by
    `tests/test_identity.py::test_a_reset_invalidates_every_session_and_every_api_token`,
    which mixes both surfaces in one context precisely because that is what a
    real deployment does across two requests.

    So the negative answer is written down rather than left implied. Default-deny
    in the shape `dough/auth.py` already uses: state the safe value first, and
    let the credential overwrite it.
    """
    # Before any early return. This is the whole fix described above.
    setattr(g, _ACTOR_KEY, None)
    setattr(g, _TOKEN_KEY, None)

    if not request.path.startswith(API_PREFIX):
        return None

    presented = _presented_token()
    if presented is None:
        return None

    # `authenticate` returns (token, user) on success and (None, reason) on
    # failure, so the second element is only a user when the first is a token.
    token, actor_or_reason = api_tokens.authenticate(presented)
    if token is None:
        return _reject(actor_or_reason)

    setattr(g, _ACTOR_KEY, actor_or_reason)
    setattr(g, _TOKEN_KEY, token)
    api_tokens.touch(token)

    if request.method not in SAFE_METHODS and not token.has_scope(
            api_tokens.SCOPE_WRITE):
        return _scope_refusal(api_tokens.SCOPE_WRITE)
    return None


def _reject(reason):
    """Refuse a bad credential without telling the holder which kind of bad.

    The reason goes to the log and the audit trail; the body says only that the
    credential is not usable. See `dough/services/api_tokens.py` for why —
    distinguishing "revoked" from "unknown" confirms that a guess was once a
    real token, which is the one fact somebody guessing wants.
    """
    from models import EVENT_API_TOKEN_REJECTED
    from dough.services import audit

    current_app.logger.warning('api token rejected',
                               extra={'api_token_outcome': reason})
    try:
        # Best-effort. There is no household to attribute this to -- that is the
        # whole nature of a rejected credential -- so it lands as a NULL-household
        # row, visible to an operator reading the table and to no tenant. The
        # same shape as a failed login, for the same reason.
        audit.record(EVENT_API_TOKEN_REJECTED, metadata={'outcome': reason})
    except Exception:
        # An audit failure must not convert a 401 into a 500. The log line above
        # has already recorded it.
        current_app.logger.exception('could not audit api token rejection')

    return api_error_response(
        401, ErrorCode.UNAUTHENTICATED,
        'That access token is not valid. Issue a new one from your household '
        'settings.')


def _scope_refusal(scope):
    return api_error_response(
        403, ErrorCode.INSUFFICIENT_SCOPE,
        f'This token cannot make changes. It needs the {scope!r} scope.',
        details={'required_scope': scope,
                 'token_scopes': bearer_token().scope_list()})


def require_scope(scope):
    """Demand a specific scope of a bearer-authenticated view.

    A no-op for a session-authenticated caller, and that is correct rather than
    a gap: scopes narrow what a *token* may do below what its user may do. A
    person signed into the web UI is already bounded by their role, which
    `@owner_required` enforces on both paths.
    """
    import functools

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            token = bearer_token()
            if token is not None and not token.has_scope(scope):
                raise InsufficientScope(
                    f'This token needs the {scope!r} scope for that.',
                    details={'required_scope': scope,
                             'token_scopes': token.scope_list()})
            return view(*args, **kwargs)

        wrapper._dough_required_scope = scope
        return wrapper

    return decorator


def enforce_rate_limit():
    """Spend one unit of this token's API allowance, or refuse with 429.
    [Phase 10.6 — wires SEC-0018's `api` and `api_write` policies]

    ## Once, here, rather than per resource

    The same argument as scope enforcement above: a resource added later
    inherits the limit instead of having to remember it. A rate limit each new
    endpoint must opt into is one the next endpoint will not have.

    ## Why this covers bearer requests and not session ones

    Both policies declare `per='token'`, and `tests/test_ratelimit.py` pins that.
    A request authenticated by session cookie has no token, so there is no key
    to spend against that would honour the declaration — and inventing one (per
    user, say) would mean the table says `token` while the code counts users,
    which is the exact drift `Policy.scope` exists to prevent.

    That leaves session traffic to `/api/v1` unlimited by this hook, and that is
    a real residual rather than a hidden one: it is recorded in SEC-0018. It is
    the narrower half of the problem — a session belongs to somebody who logged
    in, is bounded by the session lifetime, and the expensive surface behind it
    (model calls) is metered per household in `dough/ai/service.py` regardless
    of how the caller authenticated.

    ## Order

    Registered after `authenticate_bearer`, which is what puts the token on `g`.
    Before it, every request would look unauthenticated and nothing would ever
    be counted.
    """
    if not request.path.startswith(API_PREFIX):
        return None
    token = bearer_token()
    if token is None:
        return None

    from dough.services.ratelimit import current_limiter

    try:
        limiter = current_limiter()
    except RuntimeError:
        return None

    # The token's id, never the credential itself. The presented secret is a
    # bearer credential and a rate-limit key ends up in a dict, a log line and
    # eventually a Redis keyspace -- none of which are places for it.
    identity = token.id

    # Both names as literals, for the reason `dough/ai/service.py` spells out:
    # `tests/test_ratelimit.py` reads the AST for a constant first argument, so
    # that which policies are enforced is answerable by reading the code.
    _spend('api', identity, limiter)
    if request.method not in SAFE_METHODS:
        _spend('api_write', identity, limiter)
    return None


def _spend(policy_name, identity, limiter):
    """One policy against one token. Raises `RateLimited` when it is spent."""
    decision = limiter.check(policy_name, identity)
    if decision.allowed:
        return

    current_app.logger.warning('Rate limit %s reached for token %s',
                               policy_name, identity)
    from dough.services import audit
    from models import EVENT_RATE_LIMITED
    audit.record(EVENT_RATE_LIMITED,
                 metadata={'policy': policy_name, 'token_id': identity,
                           'retry_after': decision.retry_after,
                           'method': request.method, 'path': request.path})
    raise RateLimited(headers={'Retry-After': str(decision.retry_after)})


def install(app):
    """Register the bearer hook. Must run before `app.py`'s auth hooks.

    Called from `create_app` immediately after `configure_logging`, so a
    rejected credential still gets a trace id in its response — the same
    ordering argument the logging hook itself is registered under.
    """
    app.before_request(authenticate_bearer)
    app.before_request(enforce_rate_limit)
