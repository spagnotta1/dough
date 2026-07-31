"""`/api/v1/auth` — how a client gets a credential and gives it back.

This is the one module in v1 whose endpoints are not about a household's money,
and the only one carrying `@public` and `@csrf_exempt`. Both markers are on
exactly one view (`login`), and the reasoning for each is at its definition
rather than here, because a marker explained somewhere else is a marker nobody
re-reads when they add the next one.

## The flow, and why it is two steps

    POST /api/v1/auth/login   username + password  -> a token
    GET  /api/v1/auth/me      Bearer <token>       -> who am I
    GET  /api/v1/auth/tokens  Bearer <token>       -> what else is issued
    DELETE /api/v1/auth/tokens/<id>                -> revoke one

`login` exchanges a password for a token and is the *only* place a password is
accepted. Everything after it is bearer-authenticated, so a client stores the
token and never the password — which is the property that makes revocation
meaningful. A client that kept the password could always mint itself a new
token, and revoking one would achieve nothing.

## Why login is throttled the same way the web login is

It is the same attack surface with a friendlier shape for the attacker: no CSRF
token to fetch, no HTML to parse, a JSON body. `dough.auth.LoginThrottle` is
reused rather than reimplemented, and the app's single instance is shared with
the web login deliberately — two independent throttles on one credential would
each see half the attempts and neither would fire.
"""

from __future__ import annotations

from flask import Blueprint, current_app

from dough.api.envelope import created, no_content, ok
from dough.api.errors import Forbidden, RateLimited, Unauthenticated, ValidationError
from dough.api.guard import bearer_token
from dough.api.validation import body, optional_number, optional_str, require_str
from dough.auth import (client_address, csrf_exempt, current_user, public,
                        upgrade_password_hash, verify_password)
from dough.services import api_tokens, audit
from dough.tenancy import require_household, tenant_scope, unscoped

from models import (EVENT_API_TOKEN_ISSUED, EVENT_API_TOKEN_REVOKED,
                    EVENT_LOGIN_FAILED, EVENT_LOGIN_SUCCEEDED,
                    EVENT_LOGIN_THROTTLED, EVENT_PASSWORD_REHASHED)

bp = Blueprint('api_v1_auth', __name__)


@bp.record_once
def _install_throttle(state):
    """Make sure a throttle exists, sharing the web login's when there is one.

    `setdefault` on both sides, so registration order does not decide which
    surface gets the real one. They must be the same object: two throttles over
    one credential each see half the attempts, so alternating between
    `/login` and `/api/v1/auth/login` would fill neither and the control would
    be present but inert.

    This blueprint installs one at all because it is registered unconditionally,
    while `dough/blueprints/auth.py` is registered only when `AUTH_ENABLED`.
    Without this, the API login would be the one unthrottled password endpoint
    in the application in exactly the configuration where nobody is looking.
    """
    from dough.auth import LoginThrottle

    state.app.extensions.setdefault('dough_login_throttle', LoginThrottle())


#: How long a token issued by `login` lives when the caller does not say.
#: Ninety days is long enough that a phone is not re-authenticating constantly
#: and short enough that a credential forgotten on a decommissioned device stops
#: working within a quarter. A caller wanting a non-expiring token asks for one.
DEFAULT_TOKEN_TTL_DAYS = 90


@bp.route('/auth/login', methods=['POST'])
@public
@csrf_exempt
def login():
    """Exchange a username and password for an API token.

    `@public` because a caller with no credential is the entire point of this
    endpoint — it is where credentials come from. It is the fourth public route
    in the application and `tests/test_route_guard.py` pins that set, so adding
    it was a reviewed edit rather than a drift.

    `@csrf_exempt` because there is no session yet to bind a token to, which
    makes CSRF here not merely unnecessary but impossible. It is the first
    exemption in the application; `dough/auth.py::csrf_exempt` was written for
    "machine callers that authenticate some other way" and this is that case.

    What makes the exemption safe is narrower than "it is an API": this endpoint
    accepts a password, and a password is not a credential a browser attaches
    automatically. A cross-site page forging this request would have to already
    know the password, at which point it does not need the forgery. Contrast the
    session-authenticated routes, where the browser supplies the credential
    unprompted — that is the case CSRF exists for and none of them are exempt.
    """
    data = body()
    username = require_str(data, 'username', max_length=80, allow_empty=False)
    password = require_str(data, 'password', max_length=200, allow_empty=False)

    throttle = current_app.extensions.get('dough_login_throttle')
    address = client_address()

    if throttle is not None and throttle.is_locked(address, username):
        audit.record(EVENT_LOGIN_THROTTLED,
                     metadata={'username': username, 'surface': 'api'})
        # The web login distinguishes an address lock from an account lock in
        # its wording, because one is safe to explain and the other confirms a
        # username exists. An API client cannot act on the difference, so it is
        # told the coarse fact and the detail stays in the audit log.
        raise RateLimited(
            'Too many failed attempts. Wait a few minutes and try again.')

    user = _find_user(username)
    if user is None or not verify_password(user.password_hash, password):
        if throttle is not None:
            throttle.record_failure(address, username)
        audit.record(EVENT_LOGIN_FAILED,
                     metadata={'username': username, 'surface': 'api'})
        # Deliberately identical whether the username exists or the password was
        # wrong. Same reasoning as the web login: distinguishing them turns this
        # into a username enumeration endpoint, and one with no CSRF token to
        # fetch first.
        raise Unauthenticated('Those credentials were not recognised.')

    if throttle is not None:
        throttle.record_success(address, username)

    # Everything below this point writes tenant-scoped rows, and no household is
    # bound yet -- this request arrived with no session and no token, so
    # `_bind_tenant` had nothing to resolve. The user's household is what the
    # credential just proved.
    with tenant_scope(user.household_id):
        # The only moment the plaintext exists to re-derive from, and the same
        # function the web login calls. Skipping it here would mean an account
        # that only ever signs in from a phone keeps an obsolete hash forever,
        # and nothing would report it -- both logins succeed either way.
        if upgrade_password_hash(user, password):
            audit.record(EVENT_PASSWORD_REHASHED, entity_type='app_user',
                         entity_id=user.id)

        name = optional_str(data, 'device_name', max_length=80) or 'API client'
        ttl = optional_number(data, 'ttl_days', minimum=1, maximum=3650)
        scopes = data.get('scopes') or [api_tokens.SCOPE_READ,
                                        api_tokens.SCOPE_WRITE]

        try:
            token, plaintext = api_tokens.issue(
                user.household_id, user, name=name, scopes=scopes,
                ttl_days=int(ttl) if ttl else DEFAULT_TOKEN_TTL_DAYS)
        except api_tokens.ApiTokenError as exc:
            raise ValidationError(str(exc))

        audit.record(EVENT_LOGIN_SUCCEEDED,
                     metadata={'username': username, 'surface': 'api'})
        audit.record(EVENT_API_TOKEN_ISSUED, entity_type='api_token',
                     entity_id=token.id,
                     metadata={'name': token.name, 'scopes': token.scopes,
                               'surface': 'api'})

        # The one and only time the plaintext is emitted. It is not stored, so
        # there is no second chance and the response says so in a field a client
        # cannot miss.
        return created({
            'token': plaintext,
            'token_type': 'Bearer',
            'expires_at': (token.expires_at.isoformat() + 'Z'
                           if token.expires_at else None),
            'scopes': token.scope_list(),
            'shown_once': True,
            'api_token': token.to_dict(),
            'user': _serialize_user(user),
        })


def _find_user(username):
    """Resolve a username to an `AppUser`, before any household is bound.

    `unscoped()` is not needed -- `AppUser` is not tenant-scoped, precisely so
    that login can find a user before knowing their household. The lookup is
    written out here rather than reusing a helper because this is the one query
    in the API that runs with no tenant at all, and that is worth seeing.
    """
    from models import AppUser

    return AppUser.query.filter(AppUser.username == username).first()


def _serialize_user(user):
    return {
        'id': user.id,
        'username': user.username,
        'role': user.role,
        'household_id': user.household_id,
    }


@bp.route('/auth/me', methods=['GET'])
def me():
    """Who this credential acts as, and what it may do.

    The first call a client should make after storing a token: it confirms the
    credential works, names the household, and reports the scopes — so a client
    can hide the buttons its token cannot use rather than discovering the limit
    as a 403 in the middle of somebody's edit.
    """
    user = current_user()
    if user is None:
        raise Unauthenticated()

    token = bearer_token()
    payload = {'user': _serialize_user(user),
               'household': _serialize_household(user.household_id),
               'authenticated_via': 'api_token' if token else 'session'}
    if token is not None:
        payload['token'] = token.to_dict()
        payload['scopes'] = token.scope_list()
    else:
        # A session-authenticated caller is bounded by role, not by scope. Both
        # are reported so a client has one field to read either way rather than
        # a branch on how it happened to authenticate.
        payload['scopes'] = [api_tokens.SCOPE_READ, api_tokens.SCOPE_WRITE]
    return ok(payload)


def _serialize_household(household_id):
    from models import Household, db

    with unscoped():   # Household is the tenant; it has no household of its own
        home = db.session.get(Household, household_id)
    return {'id': household_id, 'name': home.name if home else None}


@bp.route('/auth/tokens', methods=['GET'])
def list_tokens():
    """Every token this household has issued, including revoked ones.

    Household-wide rather than per-user, and that is a deliberate product
    decision worth naming: a household shares its finances, so a member seeing
    that a credential exists is not a disclosure. Only an owner may revoke one,
    which is where the actual privilege boundary sits.
    """
    return ok([t.to_dict() for t in api_tokens.household_tokens(
        require_household())])


@bp.route('/auth/tokens', methods=['POST'])
def create_token():
    """Issue an additional token — one per device, ideally.

    Reachable by a session-authenticated caller (the web UI's settings page) and
    by a bearer-authenticated one. The latter means a token can mint another
    token, which is deliberate: a device rotating its own credential without a
    password round trip is the flow that makes short TTLs tolerable. It cannot
    escalate — `issue` copies nothing from the calling token, and the new
    token's powers still resolve through the same user.
    """
    user = current_user()
    if user is None:
        raise Unauthenticated()

    data = body()
    try:
        token, plaintext = api_tokens.issue(
            require_household(), user,
            name=require_str(data, 'name', max_length=80, allow_empty=False),
            scopes=data.get('scopes'),
            ttl_days=_ttl_days(data))
    except api_tokens.ApiTokenError as exc:
        raise ValidationError(str(exc))

    audit.record(EVENT_API_TOKEN_ISSUED, entity_type='api_token',
                 entity_id=token.id,
                 metadata={'name': token.name, 'scopes': token.scopes})

    return created({'token': plaintext, 'token_type': 'Bearer',
                    'shown_once': True, 'api_token': token.to_dict()},
                   location=f'/api/v1/auth/tokens/{token.id}')


def _ttl_days(data):
    ttl = optional_number(data, 'ttl_days', minimum=1, maximum=3650)
    if ttl is None:
        # An explicit null means "does not expire", which is different from the
        # field being absent. A caller has to say so rather than getting a
        # permanent credential by omission.
        return None
    return int(ttl) if ttl else DEFAULT_TOKEN_TTL_DAYS


@bp.route('/auth/tokens/<int:token_id>', methods=['DELETE'])
def revoke_token(token_id):
    """Revoke a token. Owner-only, or the token revoking itself.

    Two callers are allowed and the second one matters: a device signing itself
    out must be able to revoke its own credential without an owner present.
    Anything else — revoking somebody else's phone — is an administrative act
    over a shared household and belongs to an owner.

    `@owner_required` is not used because the rule is conditional on which token
    is being revoked, which a decorator cannot see.
    """
    user = current_user()
    if user is None:
        raise Unauthenticated()

    presented = bearer_token()
    revoking_itself = presented is not None and presented.id == token_id
    if not revoking_itself and not user.is_owner:
        raise Forbidden('Only a household owner can revoke another token.')

    try:
        token = api_tokens.revoke(require_household(), token_id)
    except api_tokens.ApiTokenError as exc:
        # "No such token" and "another household's token" arrive here as the
        # same message by design -- see the service.
        from dough.api.errors import NotFound
        raise NotFound(str(exc))

    audit.record(EVENT_API_TOKEN_REVOKED, entity_type='api_token',
                 entity_id=token.id,
                 metadata={'name': token.name,
                           'self_revoked': revoking_itself})
    return no_content()
