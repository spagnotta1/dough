"""The API's authentication boundary.  [Phase 10]

`tests/test_route_guard.py` proves no route runs anonymously and
`tests/test_csrf.py` proves no unsafe route runs without a token. Both enumerate
the URL map, so `/api/v1` inherited that cover the moment it existed. This file
asserts what those cannot: that the *bearer* path is a real credential with real
limits, and — the part that would be easiest to get wrong — that adding it did
not weaken the session path it sits beside.

The tests that matter most, in the order they would matter during a review:

- `test_session_authenticated_api_calls_still_need_a_token` — the CSRF skip is
  conditioned on the credential, never on the path. If it were conditioned on
  the path, this fails and every `/api/v1` call the web UI makes with a cookie
  would be forgeable.
- `test_a_token_cannot_reach_another_households_data` — the credential names a
  household, so a bug here is a cross-tenant read with no other layer behind it.
- `test_a_revoked_token_stops_working_immediately` — the entire reason tokens
  exist rather than long-lived sessions.
"""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

PASSWORD = 'hunter2boat'


@pytest.fixture()
def api_app(tmp_path):
    """An app with authentication *and* CSRF on.

    Both, deliberately. The suite's default fixture has them off so the ~180
    tests predating Phase 6 keep working, but every question in this file is
    about what happens when they are on — a bearer test against an app with auth
    disabled would pass without proving anything.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


def _owner(app, username='sal'):
    """A signed-in client that has created the owner account."""
    client = app.test_client()
    page = client.get('/setup')
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD,
                                '_csrf_token': _csrf(page)})
    return client


def _csrf(response):
    import re

    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _token(app, username='sal', scopes=None, device_name=None):
    """A bearer token, obtained the way a real client would.

    Through `/api/v1/auth/login` rather than by calling `api_tokens.issue`
    directly. Minting one in-process would test the service and skip the
    endpoint, which is where a client's first contact with this API actually is.

    `device_name` defaults to the username so two tokens in one test are
    distinguishable. A shared name made an earlier version of
    `test_only_an_owner_can_revoke_somebody_elses_token` select the wrong row
    and assert against the owner's token while believing it held the member's.
    """
    client = app.test_client()
    payload = {'username': username, 'password': PASSWORD,
               'device_name': device_name or f'{username} device'}
    if scopes is not None:
        payload['scopes'] = scopes
    response = client.post('/api/v1/auth/login', json=payload)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()['data']['token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


# ---------------------------------------------------------------------------
# Obtaining a credential
# ---------------------------------------------------------------------------

def test_login_exchanges_a_password_for_a_token(api_app):
    _owner(api_app)
    client = api_app.test_client()
    response = client.post('/api/v1/auth/login',
                           json={'username': 'sal', 'password': PASSWORD})

    assert response.status_code == 201
    data = response.get_json()['data']
    assert data['token'].startswith('dgh_')
    assert data['token_type'] == 'Bearer'
    assert data['shown_once'] is True
    # The plaintext must never appear in the serialized token row -- that object
    # is what the list endpoint returns, and one shared serializer leaking it
    # would leak every token to every member.
    assert 'token' not in data['api_token']


def test_login_refuses_a_wrong_password_without_saying_which_part_was_wrong(api_app):
    _owner(api_app)
    client = api_app.test_client()

    wrong_password = client.post('/api/v1/auth/login',
                                 json={'username': 'sal', 'password': 'nope'})
    unknown_user = client.post('/api/v1/auth/login',
                               json={'username': 'nobody', 'password': PASSWORD})

    assert wrong_password.status_code == unknown_user.status_code == 401
    # Identical bodies. Distinguishing them turns this into a username
    # enumeration endpoint -- and one with no CSRF token to fetch first, so it
    # is a friendlier one than the web login.
    assert (wrong_password.get_json()['error']['message']
            == unknown_user.get_json()['error']['message'])


def test_the_stored_token_is_a_hash_and_not_the_token(api_app):
    """The database must not hold anything that can be replayed.

    Asserted against the row rather than through the API, because the API is
    incapable of showing it -- which is exactly why the API cannot be the thing
    that proves this.
    """
    from models import ApiToken

    _owner(api_app)
    token = _token(api_app)

    row = ApiToken.query.first()
    assert row.token_hash != token
    assert token not in row.token_hash
    # The prefix is stored in clear and is meant to be. It identifies the row
    # for the revocation UI and leaves well over 200 bits unknown.
    assert token.startswith(row.prefix)
    assert len(row.prefix) < len(token) / 2


# ---------------------------------------------------------------------------
# Using a credential
# ---------------------------------------------------------------------------

def test_a_token_authenticates_a_request_with_no_session(api_app):
    _owner(api_app)
    token = _token(api_app)

    # A brand new client: no cookie jar, nothing carried over from login.
    fresh = api_app.test_client()
    response = fresh.get('/api/v1/auth/me', headers=_auth(token))

    assert response.status_code == 200
    data = response.get_json()['data']
    assert data['user']['username'] == 'sal'
    assert data['authenticated_via'] == 'api_token'


def test_a_bearer_write_needs_no_csrf_token(api_app):
    """The point of the whole design: a native client cannot do CSRF.

    CSRF_ENABLED is True on this app, so this passing means the skip is real
    rather than the check being off.
    """
    _owner(api_app)
    token = _token(api_app)

    response = api_app.test_client().post(
        '/api/v1/budgets', json={'category': 'Dining', 'monthly_limit': 400},
        headers=_auth(token))

    assert response.status_code == 201


def test_session_authenticated_api_calls_still_need_a_token(api_app):
    """The CSRF skip is conditioned on the credential, never on the path.

    This is the test that would catch the tempting shortcut. Exempting
    `/api/v1/*` wholesale would make this pass a 201, and every `/api/v1` call
    the web UI makes with a session cookie would become forgeable from any page
    the user visits — reopening precisely the hole SEC-0002 closed, on the
    newest routes in the application.
    """
    client = _owner(api_app)

    response = client.post('/api/v1/budgets',
                           json={'category': 'Dining', 'monthly_limit': 400})

    assert response.status_code == 403
    assert response.get_json()['error']['code'] == 'csrf_failed'


def test_a_session_cookie_and_a_csrf_token_together_still_work(api_app):
    """The other half: the web UI must be able to call v1 as it always could."""
    client = _owner(api_app)
    token = _csrf(client.get('/budgets'))

    response = client.post('/api/v1/budgets',
                           json={'category': 'Dining', 'monthly_limit': 400},
                           headers={'X-CSRF-Token': token})

    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Limits on a credential
# ---------------------------------------------------------------------------

def test_a_read_only_token_cannot_write(api_app):
    _owner(api_app)
    token = _token(api_app, scopes=['read'])
    client = api_app.test_client()

    assert client.get('/api/v1/transactions',
                      headers=_auth(token)).status_code == 200

    refused = client.post('/api/v1/budgets',
                          json={'category': 'Dining', 'monthly_limit': 400},
                          headers=_auth(token))
    assert refused.status_code == 403
    error = refused.get_json()['error']
    # A distinct code from `forbidden`, because the fix is mechanical and
    # different: reissue the token with the scope named in the details.
    assert error['code'] == 'insufficient_scope'
    assert error['details']['required_scope'] == 'write'


def test_a_revoked_token_stops_working_immediately(api_app):
    _owner(api_app)
    token = _token(api_app)
    client = api_app.test_client()

    token_id = client.get('/api/v1/auth/tokens',
                          headers=_auth(token)).get_json()['data'][0]['id']
    assert client.delete(f'/api/v1/auth/tokens/{token_id}',
                         headers=_auth(token)).status_code == 204

    after = client.get('/api/v1/auth/me', headers=_auth(token))
    assert after.status_code == 401


def test_a_revoked_token_is_refused_the_same_way_an_unknown_one_is(api_app):
    """Telling a holder that a token was *revoked* confirms it was once real.

    That is the fact somebody working through guesses wants, so both answer
    identically. The distinction is not lost — it is relocated to the audit log,
    which the next test checks.
    """
    _owner(api_app)
    token = _token(api_app)
    client = api_app.test_client()
    token_id = client.get('/api/v1/auth/tokens',
                          headers=_auth(token)).get_json()['data'][0]['id']
    client.delete(f'/api/v1/auth/tokens/{token_id}', headers=_auth(token))

    revoked = client.get('/api/v1/auth/me', headers=_auth(token))
    unknown = client.get('/api/v1/auth/me',
                         headers=_auth('dgh_' + 'x' * 43))

    assert revoked.status_code == unknown.status_code == 401
    assert revoked.get_json()['error'] == unknown.get_json()['error']


def test_a_rejected_token_is_recorded_in_the_audit_log(api_app):
    """The reason the client is not told still has to go somewhere."""
    from models import AuditEvent, EVENT_API_TOKEN_REJECTED
    from dough.tenancy import unscoped

    _owner(api_app)
    api_app.test_client().get('/api/v1/auth/me',
                              headers=_auth('dgh_' + 'x' * 43))

    with unscoped():
        events = AuditEvent.query.filter_by(
            event_type=EVENT_API_TOKEN_REJECTED).all()
    assert events, 'a rejected credential left no audit trail'


def test_an_expired_token_is_refused(api_app):
    from datetime import datetime, timedelta

    from models import ApiToken, db

    _owner(api_app)
    token = _token(api_app)

    row = ApiToken.query.first()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()

    response = api_app.test_client().get('/api/v1/auth/me', headers=_auth(token))
    assert response.status_code == 401


def test_a_token_whose_user_was_removed_stops_working(api_app):
    """The user is re-read every request rather than trusted from the token row.

    That is what makes three guarantees fall out for free: a removed member's
    tokens die, a demoted owner's tokens lose owner powers, and a dangling
    foreign key fails closed instead of authenticating.
    """
    from models import AppUser, db

    _owner(api_app)
    token = _token(api_app)

    db.session.delete(AppUser.query.filter_by(username='sal').first())
    db.session.commit()

    response = api_app.test_client().get('/api/v1/auth/me', headers=_auth(token))
    assert response.status_code == 401


def test_a_token_is_never_accepted_from_the_query_string(api_app):
    """Query strings are written to access logs, history and Referer.

    There is deliberately no code that would read one, so this asserts the
    absence rather than a rejection path.
    """
    _owner(api_app)
    token = _token(api_app)

    response = api_app.test_client().get(
        f'/api/v1/auth/me?access_token={token}&token={token}')
    assert response.status_code == 401


def test_the_actor_does_not_survive_into_the_next_request(api_app):
    """`authenticate_bearer` writes its answer on every path, not just success.

    The bug this pins, found in Phase 10.5: the hook only ever *set* the actor
    on `g` and returned early — writing nothing — for a non-API path and for a
    request offering no credential. `bearer_actor()` claims to name who *this*
    request authenticated as, and on those paths it named whoever the previous
    request had.

    `g` is per-app-context, and production pushes one per request, so nothing
    leaked there. It leaks wherever an app context outlives a request — every
    test fixture here, and any future CLI command or worker that pushes a
    context and handles more than one thing inside it.

    The consequence was real: `dough.auth.current_user` reads the bearer actor
    *first*, so a session request following an API request returned the token's
    user and skipped `session_is_current` entirely — leaving a browser signed in
    after a password change had invalidated it.

    Asserted at `bearer_actor()` rather than through a route, because the
    property is about the hook's contract and a route test would only catch the
    subset of consequences that route happens to have.
    """
    from flask import request

    from dough.api.guard import bearer_actor

    seen = []

    # Registered before any request. Flask refuses to add an `after_request` to
    # an application that has already served one, so this cannot move below the
    # setup calls.
    @api_app.after_request
    def _record(response):
        seen.append((request.path, bearer_actor()))
        return response

    _owner(api_app)
    token = _token(api_app)
    client = api_app.test_client()

    seen.clear()                                    # ignore the setup traffic
    client.get('/api/v1/auth/me', headers=_auth(token))
    client.get('/api/v1/auth/me')                   # same context, no credential
    client.get('/login')                            # not an API path at all

    # Indexed by order rather than by path: the two `/api/v1/auth/me` calls are
    # the whole point, and a dict keyed on path would silently keep only one of
    # them.
    assert [path for path, _ in seen] == [
        '/api/v1/auth/me', '/api/v1/auth/me', '/login']
    credentialed, uncredentialed, not_api = (actor for _, actor in seen)

    # Anti-vacuity: the first request really did authenticate, so the two Nones
    # below are the hook clearing state rather than the hook never running.
    assert credentialed is not None
    assert uncredentialed is None, (
        'the actor from the credentialed request survived into the next one')
    assert not_api is None


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def test_a_token_cannot_reach_another_households_data(api_app):
    """The credential names a household, and nothing else is behind it.

    Two households, each with a transaction. Neither token may count or read the
    other's rows. `.count()` is asserted as well as the list because SEC-0009
    was precisely a case where the two disagreed — the list was scoped and the
    count was not.
    """
    from datetime import date

    from models import AppUser, Household, Transaction, db
    from dough.auth import hash_password
    from dough.tenancy import tenant_scope, unscoped

    _owner(api_app)
    # Resolved, never assumed. `_seed_default_household` creates household 1
    # under TESTING before /setup runs, so the owner account lands in household
    # 2 -- and a test that hardcoded 1 would put both users' rows in the same
    # household and pass while proving nothing.
    sal_household = AppUser.query.filter_by(username='sal').first().household_id

    with unscoped():
        other = Household(name='Other household')
        db.session.add(other)
        db.session.commit()
        db.session.add(AppUser(username='pat',
                               password_hash=hash_password(PASSWORD),
                               household_id=other.id, role='owner'))
        db.session.commit()
        pat_household = other.id

    assert sal_household != pat_household

    for household_id, description in ((sal_household, 'Sal only'),
                                      (pat_household, 'Pat only')):
        with tenant_scope(household_id):
            db.session.add(Transaction(
                account_name='Checking', date=date(2026, 7, 1),
                description=description, amount=-10, category='Dining'))
            db.session.commit()

    client = api_app.test_client()
    for username, mine, theirs in (('sal', 'Sal only', 'Pat only'),
                                   ('pat', 'Pat only', 'Sal only')):
        body = client.get('/api/v1/transactions',
                          headers=_auth(_token(api_app, username=username))
                          ).get_json()
        descriptions = [t['description'] for t in body['data']]
        assert mine in descriptions
        assert theirs not in descriptions
        # The count in `meta` comes from `query.count()`. If that were unscoped
        # it would report 2 while the list showed 1 -- which is exactly the
        # shape of SEC-0009.
        assert body['meta']['pagination']['total'] == 1


def test_only_an_owner_can_revoke_somebody_elses_token(api_app):
    """A device may always sign itself out; anything else is administrative."""
    from models import AppUser, ROLE_MEMBER, db
    from dough.auth import hash_password

    _owner(api_app)
    owner_token = _token(api_app)

    # The member has to be in the *owner's* household, or this would test a
    # cross-tenant refusal rather than the role rule it is named for.
    household_id = AppUser.query.filter_by(username='sal').first().household_id
    db.session.add(AppUser(username='pat', password_hash=hash_password(PASSWORD),
                           household_id=household_id, role=ROLE_MEMBER))
    db.session.commit()
    member_token = _token(api_app, username='pat')

    client = api_app.test_client()
    issued = {t['name']: t['id'] for t in client.get(
        '/api/v1/auth/tokens', headers=_auth(owner_token)).get_json()['data']}

    # A member trying to revoke the owner's token.
    refused = client.delete(f"/api/v1/auth/tokens/{issued['sal device']}",
                            headers=_auth(member_token))
    assert refused.status_code == 403
    # Still usable -- a refused revocation must not half-succeed.
    assert client.get('/api/v1/auth/me',
                      headers=_auth(owner_token)).status_code == 200

    # The same member revoking its own is allowed, with no owner present.
    assert client.delete(f"/api/v1/auth/tokens/{issued['pat device']}",
                         headers=_auth(member_token)).status_code == 204
