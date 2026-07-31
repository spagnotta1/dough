"""Who Plaid thinks is opening Link.

Reported from a live deployment: signing in as a second account and starting a
Plaid connection offered the *first* account's phone number to send the one-time
code to. Two separate accounts, two separate households, one phone number
visible across both.

Nothing leaked out of the database, and nothing in this application ever sent a
phone number anywhere. `finance_sync/routes.py` created every Link token with
one hardcoded `client_user_id` — `"checkbook-app-user"`, from when this was a
single-user local app — and `client_user_id` is precisely the key Plaid's
returning-user experience remembers people by. So Plaid did what it was told:
same user id, same person, here is the number that person used last time.

That makes this a tenancy bug that no tenancy mechanism could have caught. The
ORM backstop guards rows; this is an identifier handed to a third party, and the
only place it can be checked is the call that sends it.

## What these tests pin

1. Two accounts never share an identifier — different households *or* the same
   one. A household shares connections; it does not share phone numbers.
2. One account keeps its identifier across sessions, because that is what makes
   the returning-user experience work for the person it belongs to.
3. The identifier is opaque and deployment-specific, so two installations
   sharing Plaid API credentials — staging and production — cannot collide on
   row ids and reintroduce the same bug one level up.
"""

import re
from pathlib import Path

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

PASSWORD = 'hunter2boat'
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'ALLOW_REGISTRATION': True,
        'SECRET_KEY': 'deployment-one-secret',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture(autouse=True)
def plaid_configured(monkeypatch):
    """`/api/plaid/link-token` 404s unless Plaid is configured, and the suite
    deliberately strips real credentials (see tests/conftest.py). These are
    fictional: no request reaches Plaid, because `create_link_token` is
    replaced below."""
    monkeypatch.setenv('PLAID_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('PLAID_SECRET', 'test-secret')


@pytest.fixture()
def link_token_calls(monkeypatch):
    """Every `client_user_id` the route hands the adapter, in order.

    The assertion has to be made here rather than on the response: the id is
    sent *to Plaid* and never comes back, so a test that only read the returned
    link token would have passed throughout the entire life of the bug.
    """
    seen = []

    def _record(self, client_user_id):
        seen.append(client_user_id)
        return 'link-sandbox-token'

    monkeypatch.setattr(
        'finance_sync.adapters.plaid_adapter.PlaidAdapter.create_link_token',
        _record)
    return seen


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _first_owner(app, username='spagnotta11'):
    """The install's first account, and the household it starts."""
    client = app.test_client()
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD})
    return client


def _second_household(app, username='rank-parsely'):
    """A second account that starts a household of its own — the reported case."""
    client = app.test_client()
    client.post('/register', data={'username': username,
                                   'email': f'{username}@example.com',
                                   'password': PASSWORD, 'confirm': PASSWORD})
    return client


def _housemate(app, owner_username='spagnotta11', username='housemate'):
    """A second account *inside* the first one's household, through an invite."""
    from dough.services.membership import issue_invite
    from dough.tenancy import tenant_scope
    from models import AppUser, ROLE_MEMBER

    owner = AppUser.query.filter_by(username=owner_username).first()
    with tenant_scope(owner.household_id):
        _invite, token = issue_invite(owner.household_id, owner,
                                      role=ROLE_MEMBER, label='housemate')
    client = app.test_client()
    client.post(f'/join/{token}', data={'username': username,
                                        'password': PASSWORD,
                                        'confirm': PASSWORD})
    return client


def _link_token(client):
    response = client.post('/api/plaid/link-token')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


# ---------------------------------------------------------------------------
# 1. Two accounts are two people
# ---------------------------------------------------------------------------

def test_separate_households_get_separate_plaid_identities(app, link_token_calls):
    """The reported bug, asserted directly.

    Before the fix both calls carried `"checkbook-app-user"`, which is what let
    Plaid offer one account's phone number to the other.
    """
    _link_token(_first_owner(app))
    _link_token(_second_household(app))

    first, second = link_token_calls
    assert first != second, (
        'two accounts in two households opened Plaid Link as the same person; '
        'Plaid will offer one of them the other\'s phone number')


def test_housemates_get_separate_plaid_identities(app, link_token_calls):
    """The smaller bug that scoping to the household would have left behind.

    Two members of one household share their money; they do not share the phone
    that receives an SMS code. This is the test that fails if `client_user_id`
    is ever "simplified" to the household id.
    """
    _link_token(_first_owner(app))
    _link_token(_housemate(app))

    owner, housemate = link_token_calls
    assert owner != housemate, (
        'two people in one household opened Plaid Link as the same person')


def test_the_identity_is_stable_across_sessions(app, link_token_calls):
    """Stability is the feature, not an accident.

    A value that changed per session would fix the leak and break the thing the
    leak was a corruption of: Plaid would stop recognising the person whose
    number it is, and ask them for it again on every connection.
    """
    client = _first_owner(app)
    _link_token(client)

    client.post('/logout')
    client.post('/login', data={'username': 'spagnotta11', 'password': PASSWORD})
    _link_token(client)

    assert len(link_token_calls) == 2
    assert link_token_calls[0] == link_token_calls[1], (
        'the same person opened Link as two different Plaid users')


def test_a_second_browser_for_the_same_account_agrees(app, link_token_calls):
    """Same account, different session cookie — a phone and a laptop."""
    _link_token(_first_owner(app))

    laptop = app.test_client()
    laptop.post('/login', data={'username': 'spagnotta11', 'password': PASSWORD})
    _link_token(laptop)

    assert link_token_calls[0] == link_token_calls[1]


# ---------------------------------------------------------------------------
# 2. What the identifier is made of
# ---------------------------------------------------------------------------

def test_the_identity_carries_nothing_about_the_account(app, link_token_calls):
    """It goes into a third party's records, so it says nothing.

    Not the username, not the address, and not the row id either — the last of
    those is what makes the next test possible.
    """
    _link_token(_second_household(app, 'rank-parsely'))
    sent = link_token_calls[0]

    for leak in ('rank-parsely', 'rank_parsely', 'example.com'):
        assert leak not in sent, f'the Plaid identity contains {leak!r}: {sent}'
    assert re.fullmatch(r'dough-[0-9a-f]{32}', sent), (
        f'expected an opaque digest, got {sent!r}')


def test_two_deployments_sharing_plaid_credentials_do_not_collide(tmp_path,
                                                                  link_token_calls):
    """Staging and production, one Plaid client id, `AppUser` 1 in both.

    A raw row id would make those two people the same Plaid user, which is the
    reported bug again with a wider blast radius and no obvious symptom. The
    identifier is keyed on the application secret, which differs per deployment.
    """
    from models import db

    tokens = []
    for name, secret in (('one', 'deployment-one-secret'),
                         ('two', 'deployment-two-secret')):
        scheduler_module._scheduler = None
        application = create_app(test_config={
            'TESTING': True,
            'AUTH_ENABLED': True,
            'SECRET_KEY': secret,
            'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / (name + '.db')}",
            'SYNC_SYNCHRONOUS': True,
            'SYNC_AUTO_ENABLED': False,
        })
        with application.app_context():
            client = application.test_client()
            client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                        'confirm': PASSWORD})
            _link_token(client)
            tokens.append(link_token_calls[-1])
            db.session.remove()
        scheduler_module._scheduler = None

    assert tokens[0] != tokens[1], (
        'the same row id on two deployments produced one Plaid identity')


# ---------------------------------------------------------------------------
# 3. The route still needs a session, and the constant is gone
# ---------------------------------------------------------------------------

def test_a_stranger_cannot_mint_a_link_token(app, link_token_calls):
    """Default-deny, asserted where it matters rather than assumed.

    An anonymous caller reaching this route would be handed a Link token minted
    under *somebody's* identity — whichever the fallback picked.
    """
    stranger = app.test_client()
    response = stranger.post('/api/plaid/link-token')

    assert response.status_code in (301, 302, 401), (
        f'/api/plaid/link-token answered an anonymous caller with '
        f'{response.status_code}')
    assert not link_token_calls, 'a Link token was minted for nobody'


def test_the_link_token_is_never_minted_from_a_constant():
    """The shape of the call, not the name of the old constant.

    That constant read as a harmless leftover for as long as it existed — the
    comment above it even said what it assumed — and it survived the arrival of
    households because nothing failed when it did. The tests above would catch
    its return; this catches it one step earlier, at the only line in the
    application that decides who Plaid is told this is.

    Deliberately not a grep for the old string: the docstring on
    `_plaid_client_user_id` quotes it, and that history is worth more where it
    is than a simpler assertion is here.
    """
    source = (ROOT / 'finance_sync' / 'routes.py').read_text(encoding='utf-8')

    # Greedy to the last `)` on the line: the argument is itself a call, and a
    # lazy match would stop inside it.
    calls = [call.strip() for call in re.findall(r'create_link_token\((.*)\)', source)]
    assert calls == ['_plaid_client_user_id()'], (
        f'a Link token is minted from something other than the per-user '
        f'derivation: {calls}')
    assert not re.search(r'^\s*_PLAID_CLIENT_USER_ID\s*=', source, re.M), (
        'a module-level Plaid identifier is back; one constant means one person')
