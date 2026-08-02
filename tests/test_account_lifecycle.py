"""Taking your data out, and taking your account away.  [Phase 10.7]

The two operations the privacy policy promises. They are tested together
because they are one feature — "your data is yours" is only true if both work —
and because they share the property that makes them worth testing carefully:
**both are irreversible in one direction.** An export that omits a table is a
promise quietly broken; a deletion that leaves rows behind is the same promise
broken in the more serious direction.

Four kinds of assertion here, in the order that matters:

1. **Deletion actually deletes.** Row counts to zero, in every tenant-scoped
   table, verified by walking the tables rather than by naming the ones the
   author remembered.
2. **Deletion does not delete too much.** One household closing its account
   must not touch another's rows. This is the same boundary the whole
   application is built around, tested against the one operation written as a
   bulk `DELETE`.
3. **The confirmation cannot be bypassed.** Wrong password, wrong username,
   missing CSRF — each refuses, and each leaves everything intact.
4. **The export is complete and carries no credentials.** Those two pull in
   opposite directions, which is why both are pinned.
"""

import json
import re

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.services import account_lifecycle

PASSWORD = 'hunter2boat'


@pytest.fixture()
def live_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'life.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


def _csrf(response):
    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _owner(app, username='sal'):
    """A signed-in client whose account owns a fresh household."""
    client = app.test_client()
    page = client.get('/setup')
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD, '_csrf_token': _csrf(page)})
    return client


def _user(username='sal'):
    from dough.tenancy import unscoped
    from models import AppUser

    with unscoped():
        return AppUser.query.filter_by(username=username).one()


def _seed_finances(household_id, *, description='Coffee'):
    """One row in several scoped tables, so deletion has something to remove."""
    from datetime import date

    from dough.tenancy import tenant_scope
    from models import Budget, Holding, Transaction, db

    with tenant_scope(household_id):
        db.session.add(Transaction(date=date(2026, 7, 1), description=description,
                                   amount=-4.50, category='Dining',
                                   account_name='checking'))
        db.session.add(Budget(category='Dining', monthly_limit=100))
        db.session.add(Holding(ticker='VTI', name='Total Market',
                               shares=3, current_value=600))
        db.session.commit()


# ---------------------------------------------------------------------------
# The table list is the whole correctness argument
# ---------------------------------------------------------------------------

def test_every_tenant_scoped_model_is_accounted_for(live_app):
    """The test that makes the literal list in the service safe.

    `_SCOPED_TABLES` is written out by hand because deriving it from
    `TenantScopedMixin.__subclasses__()` fails in the direction that does not
    raise: a model whose module has not been imported is simply absent, so the
    export omits a table and the deletion leaves one behind, silently.

    This walks the subclasses — which is safe *here*, because the app fixture
    has imported every model — and requires the hand-written list to match. A
    new scoped model added without touching the service fails this rather than
    quietly escaping both operations.
    """
    from models import TenantScopedMixin

    declared = {name for _key, name in account_lifecycle._SCOPED_TABLES}
    actual = {cls.__name__ for cls in TenantScopedMixin.__subclasses__()}

    assert declared == actual, (
        'dough/services/account_lifecycle.py::_SCOPED_TABLES is out of date.\n'
        f'  missing from the list: {sorted(actual - declared)}\n'
        f'  listed but not a model: {sorted(declared - actual)}\n'
        'A model missing here is one that export skips and delete leaves behind.')


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_the_export_contains_the_households_records(live_app):
    client = _owner(live_app)
    _seed_finances(_user().household_id, description='Flat white')

    response = client.get('/settings/export')

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload['account']['username'] == 'sal'
    assert payload['counts']['transactions'] == 1
    assert payload['data']['transactions'][0]['description'] == 'Flat white'
    assert payload['counts']['budgets'] == 1
    assert payload['counts']['holdings'] == 1


def test_the_export_is_offered_as_a_download_and_never_cached(live_app):
    """It is the most sensitive response this application produces.

    A shared browser must not be able to pull a household's whole ledger back
    out of history, which `no-store` is what prevents.
    """
    client = _owner(live_app)
    response = client.get('/settings/export')

    assert 'attachment' in response.headers['Content-Disposition']
    assert response.headers['Content-Disposition'].endswith('.json')
    assert 'no-store' in response.headers['Cache-Control']


def test_the_export_carries_no_credentials(live_app):
    """The property that pulls against completeness, so it is pinned.

    An export is a file somebody downloads, emails to themselves and drops in a
    cloud folder. A password hash is offline-crackable and an `auth_blob` is a
    bank credential; neither belongs in it, however encrypted.
    """
    from dough.tenancy import tenant_scope
    from models import InstitutionConnection, db

    client = _owner(live_app)
    household_id = _user().household_id
    with tenant_scope(household_id):
        db.session.add(InstitutionConnection(
            institution='plaid', display_name='Test Bank',
            auth_blob='ENCRYPTED-TOKEN-SENTINEL'))
        db.session.commit()

    body = client.get('/settings/export').get_data(as_text=True)

    assert 'Test Bank' in body, 'the connection itself should be in the export'
    assert 'ENCRYPTED-TOKEN-SENTINEL' not in body
    assert 'password_hash' not in body
    assert 'auth_blob' not in body


def test_an_export_is_audited(live_app):
    """A full copy of a household's finances leaving in one request.

    The highest-value action a stolen session can take short of a password
    change, and it should not be the one that leaves no trace.
    """
    from dough.tenancy import unscoped
    from models import EVENT_ACCOUNT_EXPORTED, AuditEvent

    client = _owner(live_app)
    client.get('/settings/export')

    with unscoped():
        assert AuditEvent.query.filter_by(
            event_type=EVENT_ACCOUNT_EXPORTED).count() == 1


def test_an_export_needs_a_session(live_app):
    _owner(live_app)
    anonymous = live_app.test_client()
    assert anonymous.get('/settings/export').status_code in (302, 401)


# ---------------------------------------------------------------------------
# Deletion — the confirmation
# ---------------------------------------------------------------------------

def test_the_confirmation_page_says_what_would_be_removed(live_app):
    client = _owner(live_app)
    _seed_finances(_user().household_id)

    body = client.get('/settings/delete').get_data(as_text=True)

    assert 'transactions' in body
    assert 'sal' in body, 'it must name the username that has to be typed'


def test_a_wrong_password_deletes_nothing(live_app):
    from models import AppUser

    client = _owner(live_app)
    page = client.get('/settings/delete')
    response = client.post('/settings/delete', data={
        'password': 'not-the-password', 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    assert AppUser.query.filter_by(username='sal').count() == 1
    assert b'not right' in response.data


def test_a_mistyped_username_deletes_nothing(live_app):
    """The control aimed at the account holder rather than at an attacker.

    Somebody who arrived by misreading a link must not lose a household because
    a confirm dialog was one keystroke from an accepted default.
    """
    from models import AppUser

    client = _owner(live_app)
    page = client.get('/settings/delete')
    response = client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'Sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    assert AppUser.query.filter_by(username='sal').count() == 1
    assert b'exactly' in response.data


def test_deletion_without_a_csrf_token_is_refused(live_app):
    from models import AppUser

    client = _owner(live_app)
    response = client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal'})

    assert response.status_code == 403
    assert AppUser.query.filter_by(username='sal').count() == 1


# ---------------------------------------------------------------------------
# Deletion — the last member
# ---------------------------------------------------------------------------

def test_deleting_the_last_member_removes_the_household_and_its_data(live_app):
    """The whole promise, checked table by table rather than by sampling."""
    from dough.tenancy import unscoped
    from models import AppUser, Household

    client = _owner(live_app)
    household_id = _user().household_id
    _seed_finances(household_id)

    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    with unscoped():
        assert AppUser.query.filter_by(username='sal').count() == 0
        assert Household.query.get(household_id) is None
        for _key, model_name in account_lifecycle._SCOPED_TABLES:
            import models as models_module
            model = getattr(models_module, model_name)
            remaining = model.query.filter_by(household_id=household_id).count()
            assert remaining == 0, f'{model_name} still has {remaining} rows'


def test_deletion_removes_api_tokens_and_verification_tokens(live_app):
    """Credentials outlive sessions, so they have to be deleted explicitly.

    An API token that survived its owner's deletion would be a working
    credential for an account that no longer exists.
    """
    from dough.services import api_tokens, identity
    from dough.tenancy import tenant_scope, unscoped
    from models import PURPOSE_VERIFY_EMAIL, ApiToken, EmailVerification, db

    client = _owner(live_app)
    user = _user()
    household_id = user.household_id
    user_id = user.id

    with tenant_scope(household_id):
        api_tokens.issue(household_id, user, name='phone', scopes=['read'])
        # `issue_token` refuses an account with no address on file, and /setup
        # does not ask for one -- so the address is set here rather than the
        # test asserting against a token that was never minted.
        user.email = 'sal@example.com'
        db.session.commit()
        identity.issue_token(user, PURPOSE_VERIFY_EMAIL)
        db.session.commit()

    with unscoped():
        assert ApiToken.query.filter_by(user_id=user_id).count() == 1
        assert EmailVerification.query.filter_by(user_id=user_id).count() >= 1

    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    with unscoped():
        assert ApiToken.query.filter_by(user_id=user_id).count() == 0
        assert EmailVerification.query.filter_by(user_id=user_id).count() == 0


def test_the_session_is_cleared_so_the_next_request_is_signed_out(live_app):
    """The row backing the session is gone; the cookie must go with it."""
    client = _owner(live_app)
    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    assert client.get('/transactions').status_code in (302, 401)


def test_a_deletion_is_audited_and_the_record_outlives_the_household(live_app):
    """The one event that deliberately survives everything it describes.

    After this, the audit row is the only remaining evidence the account existed
    — which is what makes it the answer to "what happened to it".
    """
    from dough.tenancy import unscoped
    from models import EVENT_ACCOUNT_DELETED, AuditEvent

    client = _owner(live_app)
    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    with unscoped():
        events = AuditEvent.query.filter_by(
            event_type=EVENT_ACCOUNT_DELETED).all()
    assert len(events) == 1
    assert 'sal' in (events[0].metadata_json or '')


# ---------------------------------------------------------------------------
# Deletion — tenancy, which is the dangerous part
# ---------------------------------------------------------------------------

def test_deleting_one_household_does_not_touch_another(live_app):
    """The bulk `DELETE` is the one place a missing WHERE would be catastrophic.

    Written scoped so the tenant filter is applied by construction rather than
    by the author remembering it — and asserted here, because "by construction"
    is a claim about code that a later edit can quietly falsify.
    """
    from dough.tenancy import tenant_scope, unscoped
    from models import Household, Transaction, db

    client = _owner(live_app, 'sal')
    mine = _user('sal').household_id
    _seed_finances(mine, description='Mine')

    with unscoped():
        other = Household(name='Someone else')
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    _seed_finances(other_id, description='Theirs')

    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    with tenant_scope(other_id):
        survivors = Transaction.query.all()
    assert len(survivors) == 1
    assert survivors[0].description == 'Theirs'


# ---------------------------------------------------------------------------
# Deletion — shared households
# ---------------------------------------------------------------------------

def _second_member(app, household_id, username='partner', role=None):
    from dough.auth import hash_password
    from dough.tenancy import unscoped
    from models import ROLE_MEMBER, AppUser, db

    with unscoped():
        member = AppUser(username=username,
                         password_hash=hash_password(PASSWORD),
                         household_id=household_id,
                         role=role or ROLE_MEMBER)
        db.session.add(member)
        db.session.commit()
        return member.id


def test_leaving_a_shared_household_keeps_its_financial_records(live_app):
    """The other half of the rule the privacy policy states.

    The ledger belongs to the household. Deleting it because one of two people
    left would destroy data belonging to somebody who did not ask for anything.
    """
    from dough.tenancy import tenant_scope, unscoped
    from models import ROLE_OWNER, AppUser, Household, Transaction

    client = _owner(live_app, 'sal')
    household_id = _user('sal').household_id
    _seed_finances(household_id, description='Shared groceries')
    _second_member(live_app, household_id, 'partner', role=ROLE_OWNER)

    page = client.get('/settings/delete')
    client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    with unscoped():
        assert AppUser.query.filter_by(username='sal').count() == 0
        assert AppUser.query.filter_by(username='partner').count() == 1
        assert Household.query.get(household_id) is not None
    with tenant_scope(household_id):
        assert Transaction.query.count() == 1


def test_the_only_owner_of_a_shared_household_is_refused(live_app):
    """Not a technical limit: a household must keep an owner.

    Silently promoting somebody would hand a person administrative control of a
    household because somebody else quit. The refusal names the fix instead.
    """
    from models import AppUser

    client = _owner(live_app, 'sal')
    household_id = _user('sal').household_id
    _second_member(live_app, household_id, 'partner')   # a member, not an owner

    page = client.get('/settings/delete')
    response = client.post('/settings/delete', data={
        'password': PASSWORD, 'confirm_username': 'sal',
        '_csrf_token': _csrf(page)}, follow_redirects=True)

    assert AppUser.query.filter_by(username='sal').count() == 1
    assert b'only owner' in response.data


def test_the_confirmation_page_warns_the_sole_owner_before_they_try(live_app):
    """A refusal after typing a password and a username is a bad refusal."""
    from models import ROLE_MEMBER

    client = _owner(live_app, 'sal')
    _second_member(live_app, _user('sal').household_id, 'partner',
                   role=ROLE_MEMBER)

    body = client.get('/settings/delete').get_data(as_text=True)
    assert 'only owner' in body


def test_the_preview_distinguishes_the_two_cases(live_app):
    """The dict the confirmation page is built from, checked directly."""
    from dough.tenancy import tenant_scope
    from models import ROLE_OWNER

    _owner(live_app, 'sal')
    household_id = _user('sal').household_id
    _seed_finances(household_id)

    with tenant_scope(household_id):
        alone = account_lifecycle.deletion_preview(_user('sal'))
    assert alone['last_member'] is True
    assert alone['counts']['transactions'] == 1

    _second_member(live_app, household_id, 'partner', role=ROLE_OWNER)
    with tenant_scope(household_id):
        shared = account_lifecycle.deletion_preview(_user('sal'))
    assert shared['last_member'] is False
    assert shared['other_members'] == ['partner']
    assert shared['counts'] == {}, (
        'nothing household-wide is removed when somebody else remains')
