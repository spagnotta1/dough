"""One counter, both credential surfaces.  [Phase 10.5]

Phase 10 left this application with two kinds of credential — a signed session
cookie and an opaque bearer token — and no way to withdraw either in response to
a change to the account behind it. `AppUser.session_version` is that way: every
credential is stamped with the value current when it was created, and raising it
invalidates all of them at once.

The tests that matter most, in the order they would matter during a review:

- `test_a_password_change_invalidates_an_api_token` and
  `test_a_password_change_signs_an_existing_browser_session_out` — the property
  the whole mechanism exists for, asserted once per surface. If either fails,
  changing a password does not actually take anything away.
- `test_a_password_change_made_outside_a_request_still_bumps` — the bump is a
  `before_flush` listener rather than a call the caller has to remember, and
  this is what says so. A future password-reset route inherits the invalidation
  without knowing this file exists.
- `test_the_sign_in_rehash_does_not_bump` — the one exemption. Without it, the
  first sign-in of anyone holding a pre-scrypt hash would silently sign out
  every one of their other devices.

The password changes here are made directly against the model rather than
through `/settings/password`, which now exists. That is deliberate and it is
the point rather than a shortcut: the invalidation must belong to the *data
change*, not to a particular endpoint, so these tests keep asserting it at the
level where the guarantee actually lives. The routes are exercised separately in
`tests/test_identity.py`.
"""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.auth import SESSION_VERSION_KEY, hash_password

PASSWORD = 'hunter2boat'
NEW_PASSWORD = 'trombone9pastry'


@pytest.fixture()
def api_app(tmp_path):
    """Auth and CSRF both on — see `tests/test_api_auth.py::api_app`."""
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


def _csrf(response):
    import re

    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _owner(app, username='sal'):
    client = app.test_client()
    page = client.get('/setup')
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD,
                                '_csrf_token': _csrf(page)})
    return client


def _token(app, username='sal', password=PASSWORD):
    client = app.test_client()
    response = client.post('/api/v1/auth/login',
                           json={'username': username, 'password': password,
                                 'device_name': f'{username} device'})
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()['data']['token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def _user(username='sal'):
    from models import AppUser

    return AppUser.query.filter_by(username=username).first()


def _change_password(username='sal', password=NEW_PASSWORD):
    """The write a password-change route will make, made directly.

    Returns the account's new `session_version`, which no caller sets — the
    listener in `dough/auth.py` is what moved it.
    """
    from models import db

    user = _user(username)
    user.password_hash = hash_password(password)
    db.session.commit()
    return user.session_version


# ---------------------------------------------------------------------------
# The counter itself
# ---------------------------------------------------------------------------

def test_a_new_account_starts_at_one(api_app):
    _owner(api_app)
    assert _user().session_version == 1


def test_changing_a_password_raises_the_counter(api_app):
    _owner(api_app)
    assert _change_password() == 2
    assert _change_password(password='third4passwordx') == 3


def test_a_password_change_made_outside_a_request_still_bumps(api_app):
    """The bump is not something a route does.

    This is the claim the whole design rests on: the listener is registered on
    `sqlalchemy.orm.Session`, so a password reset performed by a `tools/` script
    or a shell — with no request, no blueprint and no knowledge of any of this —
    invalidates credentials exactly as a route would. An explicit
    `invalidate(user)` call would be the thing such a script forgets.
    """
    _owner(api_app)
    token = _token(api_app)

    # No test client, no request context: just the model and a commit.
    _change_password()

    assert api_app.test_client().get(
        '/api/v1/auth/me', headers=_auth(token)).status_code == 401


def test_the_sign_in_rehash_does_not_bump(api_app):
    """Upgrading a stale hash must not sign anybody out.

    A rehash replaces the stored hash without the password having changed, so
    nothing has been superseded. If this bumped, the first sign-in of an account
    carrying a pre-scrypt hash would revoke every credential that account held —
    once, invisibly, and blamed on anything but the rehash.
    """
    from models import db
    from werkzeug.security import generate_password_hash

    from dough.auth import needs_rehash

    _owner(api_app)

    user = _user()
    user.password_hash = generate_password_hash(PASSWORD, method='pbkdf2:sha256')
    db.session.commit()
    # Planting the stale hash is itself a `password_hash` change, so it bumps
    # too -- correctly. Hence `before` is read rather than assumed to be 1.
    before = user.session_version

    login = api_app.test_client().post(
        '/api/v1/auth/login', json={'username': 'sal', 'password': PASSWORD})
    assert login.status_code == 201

    user = _user()
    # Asserted, not assumed. Without this the test would pass just as happily if
    # the rehash never ran, which is the one way it could be green while proving
    # nothing about the exemption.
    assert not needs_rehash(user.password_hash), 'the hash was not upgraded'
    assert user.session_version == before, (
        'the sign-in rehash bumped session_version and revoked every '
        'credential this account held')


def test_creating_a_second_member_does_not_disturb_the_first(api_app):
    """A new account is an INSERT, and the listener only looks at UPDATEs.

    Worth pinning because the naive listener — every `AppUser` in the session
    whose `password_hash` is set — would bump the *new* user harmlessly and, on
    a flush that carried both, do nothing to the existing one. The failure this
    guards against is subtler: a broad `isinstance` check with no history test
    would bump every user touched in any flush, quietly signing people out when
    somebody else joined.
    """
    from models import db
    from dough.services.membership import accept_invite, issue_invite
    from dough.tenancy import tenant_scope

    _owner(api_app)
    owner = _user()
    token = _token(api_app)

    # `household_invites` is tenant-scoped, so the write guard needs a bound
    # household -- there is no request here to have bound one.
    with tenant_scope(owner.household_id):
        invite, plaintext = issue_invite(owner.household_id, owner, role='member')
        accept_invite(invite, 'pat', hash_password('another8password'))
        db.session.commit()

    assert _user().session_version == 1
    assert api_app.test_client().get(
        '/api/v1/auth/me', headers=_auth(token)).status_code == 200
    assert plaintext


# ---------------------------------------------------------------------------
# The bearer surface
# ---------------------------------------------------------------------------

def test_a_password_change_invalidates_an_api_token(api_app):
    _owner(api_app)
    token = _token(api_app)
    assert api_app.test_client().get(
        '/api/v1/auth/me', headers=_auth(token)).status_code == 200

    _change_password()

    response = api_app.test_client().get('/api/v1/auth/me', headers=_auth(token))
    assert response.status_code == 401
    assert response.get_json()['error']['code'] == 'unauthenticated'


def test_a_token_issued_after_the_change_works(api_app):
    """The invalidation is of a generation, not of the account."""
    _owner(api_app)
    _change_password()

    token = _token(api_app, password=NEW_PASSWORD)
    assert api_app.test_client().get(
        '/api/v1/auth/me', headers=_auth(token)).status_code == 200


def test_a_stale_token_is_refused_indistinguishably_from_an_unknown_one(api_app):
    """A superseded credential must not be told that it was once real.

    Same reasoning as revoked-vs-unknown: the distinction is relocated to the
    audit log rather than lost, because telling a caller that its token used to
    work confirms it once held a valid credential for this account.
    """
    _owner(api_app)
    token = _token(api_app)
    _change_password()

    stale = api_app.test_client().get('/api/v1/auth/me', headers=_auth(token))
    unknown = api_app.test_client().get('/api/v1/auth/me',
                                        headers=_auth('dgh_' + 'x' * 43))

    assert stale.status_code == unknown.status_code == 401
    assert stale.get_json()['error'] == unknown.get_json()['error']


def test_the_audit_log_says_the_token_was_stale(api_app):
    """Where the distinction the client is denied actually goes."""
    from models import AuditEvent, EVENT_API_TOKEN_REJECTED
    from dough.tenancy import unscoped

    _owner(api_app)
    token = _token(api_app)
    _change_password()
    api_app.test_client().get('/api/v1/auth/me', headers=_auth(token))

    with unscoped():
        events = AuditEvent.query.filter_by(
            event_type=EVENT_API_TOKEN_REJECTED).all()
    assert any('stale' in (e.metadata_json or '') for e in events), (
        'a superseded token was refused without recording why')


def test_a_stale_token_cannot_write_either(api_app):
    """The check runs before any route, not just on `/auth/me`.

    `/auth/me` is the endpoint a client hits first, so a check that only covered
    it would look like it worked. This asserts the refusal happens in the guard,
    which is the layer every route sits behind.
    """
    _owner(api_app)
    token = _token(api_app)
    _change_password()

    response = api_app.test_client().post(
        '/api/v1/budgets', headers=_auth(token),
        json={'category': 'Groceries', 'monthly_limit': 100})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# The session surface
# ---------------------------------------------------------------------------

#: An ordinary protected page, used wherever these tests need to ask "is this
#: session still good?".
#:
#: It used to be `/`. Phase 10.5 made `/` `@public` -- it serves the marketing
#: page to a stranger and the dashboard to a signed-in user -- so a 302 is no
#: longer what an invalidated session gets there, and asking `/` would have
#: quietly stopped testing invalidation at all. `/transactions` is chosen
#: precisely because nothing about it is special.
#:
#: What `/` does instead is asserted directly, once, in
#: `test_an_invalidated_session_sees_the_landing_page_not_the_dashboard` -- the
#: guarantee that matters there is not the status code but that no data is
#: rendered.
PROTECTED_PAGE = '/transactions'


def test_a_password_change_signs_an_existing_browser_session_out(api_app):
    client = _owner(api_app)
    assert client.get(PROTECTED_PAGE).status_code == 200

    _change_password()

    # A redirect to the login page, which is what `unauthorized_response` gives
    # a navigation. The point is that it is no longer 200.
    assert client.get(PROTECTED_PAGE).status_code == 302


def test_an_invalidated_session_sees_the_landing_page_not_the_dashboard(api_app):
    """`@public` on `/` must not become a way around the version check.

    This is the failure the marker makes possible and it is worth pinning
    explicitly: `_require_login` short-circuits on a public view, so without the
    session-lifetime call added to that branch in Phase 10.5, a browser whose
    credentials had been invalidated would still be handed the dashboard at `/`
    -- with a bound household and real figures on it.

    The status code is deliberately not the assertion. 200 is correct here: a
    cleared session is an anonymous visitor, and an anonymous visitor gets the
    marketing page. What must be true is that the response contains no data and
    the cookie is gone.
    """
    client = _owner(api_app)
    _change_password()

    response = client.get('/')
    assert response.status_code == 200
    assert b'Your money' in response.data          # the landing hero
    # Markers unique to the markup rather than to base.html's stylesheet, which
    # is shared by every page: `.nav-link--accent` and the words "Ask Dough"
    # both appear in that CSS whatever chrome is rendered, so asserting on them
    # would pass for the wrong reason.
    assert b'id="profile-btn"' not in response.data   # signed-in nav absent
    assert b'id="tab-bar"' not in response.data       # signed-in tab bar absent
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_the_signed_out_session_is_cleared_rather_than_left_dangling(api_app):
    """A refused session must not survive the request that refused it.

    Leaving `user_id` in the cookie would mean every subsequent request repeats
    the lookup and the refusal, and — worse — that a rollback of the version
    would silently restore the session. `_enforce_session_lifetime` clears it.
    """
    client = _owner(api_app)
    _change_password()
    client.get(PROTECTED_PAGE)

    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_signing_in_again_works_and_records_the_new_generation(api_app):
    client = _owner(api_app)
    _change_password()
    client.get(PROTECTED_PAGE)

    page = client.get('/login')
    client.post('/login', data={'username': 'sal', 'password': NEW_PASSWORD,
                                '_csrf_token': _csrf(page)})

    assert client.get(PROTECTED_PAGE).status_code == 200
    with client.session_transaction() as sess:
        assert sess[SESSION_VERSION_KEY] == 2


def test_a_session_carrying_no_version_at_all_is_refused(api_app):
    """Fail-closed for cookies minted before this mechanism existed.

    Accepting them would exempt every pre-existing session from the check
    permanently — and those are the long-lived ones, so "it will age out" is not
    an argument that covers the session anybody is actually worried about. The
    cost is one sign-in per browser at deploy time.
    """
    client = _owner(api_app)
    with client.session_transaction() as sess:
        del sess[SESSION_VERSION_KEY]

    assert client.get(PROTECTED_PAGE).status_code == 302


def test_a_forged_version_does_not_help(api_app):
    """The cookie is signed, so this is only reachable from inside a test.

    It is asserted anyway because the value is compared for *equality* with the
    stored one, not `>=`: a session claiming a version from the future is as
    invalid as one claiming a version from the past, and an implementation that
    used a comparison would let a tampered cookie outlive every future change.
    """
    client = _owner(api_app)
    with client.session_transaction() as sess:
        sess[SESSION_VERSION_KEY] = 99

    assert client.get(PROTECTED_PAGE).status_code == 302


# ---------------------------------------------------------------------------
# What the token list says about it
# ---------------------------------------------------------------------------

def test_a_superseded_token_is_listed_as_stale_not_active(api_app):
    """The list must not describe a dead credential as live.

    Nothing sweeps `api_tokens` on a password change — the invalidation is a
    comparison, so there is no second write to lose — which means the row still
    has a null `revoked_at`. Reporting that as `'active'` would tell somebody
    checking after a password change that their old phone still had access.
    """
    client = _owner(api_app)
    _token(api_app)
    _change_password()

    # The browser session is dead too, so sign in again to read the list.
    page = client.get('/login')
    client.post('/login', data={'username': 'sal', 'password': NEW_PASSWORD,
                                '_csrf_token': _csrf(page)})

    listing = client.get('/api/v1/auth/tokens')
    assert listing.status_code == 200
    states = [t['state'] for t in listing.get_json()['data']]
    assert 'stale' in states
    assert 'active' not in states


def test_a_token_whose_user_was_deleted_is_stale_too(api_app):
    """A removed member's rows are kept deliberately; they must not read active.

    `20260730_05` chose not to cascade `user_id` so that removing somebody does
    not erase the evidence that their credential existed. The cost of that
    choice is a row pointing at a missing user, and this is the assertion that
    the cost is paid honestly rather than shown as a working token.
    """
    from models import ApiToken, db

    _owner(api_app)
    _token(api_app)

    user = _user()
    token_row = ApiToken.query.filter_by(user_id=user.id).first()
    db.session.delete(user)
    db.session.commit()
    db.session.expire(token_row)

    assert token_row.state() == 'stale'
