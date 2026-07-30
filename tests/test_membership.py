"""Household membership: invitations, roles, and the rule that a household
always has an owner.  [Phase 6]

Two things here are worth knowing before reading.

**The role tests run with the tenancy backstop off.** ADR-0008's second
constraint says ORM-level filtering is defense in depth and never the
authorization mechanism, and role checks are a case the backstop could not
cover even if it were trusted: a member of household 7 removing household 7's
owner never crosses a tenant boundary. So `owner_required` has to hold on its
own, and these tests make sure it is what is holding.

**Every refusal is paired with the permission that proves the refusal meant
something.** A test that only asserts "member cannot promote" passes just as
well against a route that is broken for everybody.
"""

import re
from datetime import datetime, timedelta

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.auth import CSRF_FIELD
from dough.services.membership import (LastOwnerError, MembershipError,
                                       find_redeemable_invite,
                                       hash_invite_token, issue_invite,
                                       remove_member, set_member_role)
from dough.tenancy import tenant_scope, unscoped
from models import AppUser, Household, HouseholdInvite, ROLE_MEMBER, ROLE_OWNER, db

PASSWORD = 'hunter2boat'


@pytest.fixture()
def home_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': False,      # exercised end to end in test_csrf.py
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def owner_client(home_app):
    client = home_app.test_client()
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD})
    return client


def _invite_link(client, role='member', label='for Jamie'):
    resp = client.post('/household/invites',
                       data={'role': role, 'label': label},
                       follow_redirects=True)
    match = re.search(r'value="(http[^"]*/join/[^"]+)"', resp.get_data(as_text=True))
    assert match, 'the invitation link was not rendered'
    return match.group(1)


def _join(app, link, username='jamie', password=PASSWORD):
    """Follow an invitation link as a brand-new visitor."""
    path = link.split('localhost', 1)[-1]
    visitor = app.test_client()
    return visitor, visitor.post(path, data={
        'username': username, 'password': password, 'confirm': password})


# ---------------------------------------------------------------------------
# Issuing and redeeming
# ---------------------------------------------------------------------------

def test_an_owner_can_invite_and_the_link_works_once(home_app, owner_client):
    link = _invite_link(owner_client)

    visitor, resp = _join(home_app, link)
    assert resp.status_code == 302
    jamie = AppUser.query.filter_by(username='jamie').one()
    assert jamie.household_id == AppUser.query.filter_by(
        username='sal').one().household_id
    assert jamie.role == ROLE_MEMBER
    assert visitor.get('/transactions').status_code == 200

    # The second visitor gets nothing, and is not told why.
    _, second = _join(home_app, link, username='someone-else')
    assert second.status_code == 404


def test_the_plaintext_token_is_never_stored(home_app, owner_client):
    """Holding the link is the whole authorization, so the database must not.

    A backup, a stray `SELECT *`, or a log line would otherwise hand over
    working invitations into a household's finances.
    """
    link = _invite_link(owner_client)
    token = link.rsplit('/', 1)[-1]

    with unscoped():
        stored = [row.token_hash for row in HouseholdInvite.query.all()]
    assert stored, 'no invitation was recorded'
    assert token not in stored
    assert hash_invite_token(token) in stored


def test_an_expired_invitation_is_refused(home_app, owner_client):
    link = _invite_link(owner_client)
    # tenant_scope, not unscoped: `unscoped()` relaxes reads only, and ageing a
    # row is a write. See ADR-0008 section 3 for the autoflush bug that
    # asymmetry exists to prevent.
    household_id = AppUser.query.filter_by(username='sal').one().household_id
    with tenant_scope(household_id):
        invite = HouseholdInvite.query.one()
        invite.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()

    _, resp = _join(home_app, link)
    assert resp.status_code == 404
    assert AppUser.query.filter_by(username='jamie').first() is None


def test_a_revoked_invitation_is_refused(home_app, owner_client):
    link = _invite_link(owner_client)
    with unscoped():
        invite_id = HouseholdInvite.query.one().id
    owner_client.post(f'/household/invites/{invite_id}/revoke')

    _, resp = _join(home_app, link)
    assert resp.status_code == 404


def test_an_unknown_token_is_indistinguishable_from_a_used_one(home_app, owner_client):
    """Both 404 with the same page. Distinguishing them confirms which tokens
    were once real, which is what somebody guessing wants to learn."""
    link = _invite_link(owner_client)
    _join(home_app, link)

    used = home_app.test_client().get(link.split('localhost', 1)[-1])
    never = home_app.test_client().get('/join/' + 'x' * 43)
    assert used.status_code == never.status_code == 404
    assert used.get_data() == never.get_data()


def test_an_invited_owner_gets_the_owner_role(home_app, owner_client):
    link = _invite_link(owner_client, role='owner')
    _join(home_app, link)
    assert AppUser.query.filter_by(username='jamie').one().role == ROLE_OWNER


def test_a_signed_in_visitor_is_turned_away_rather_than_switched(home_app, owner_client):
    """ADR-0009: one person, one household.

    Accepting while already a member would mean silently leaving, and the
    household left behind might be one nobody else can administer.
    """
    link = _invite_link(owner_client)
    resp = owner_client.get(link.split('localhost', 1)[-1])
    assert resp.status_code == 403
    assert b'already in a household' in resp.data


def test_a_taken_username_is_reported_not_swallowed(home_app, owner_client):
    link = _invite_link(owner_client)
    _, resp = _join(home_app, link, username='sal')
    assert resp.status_code == 200
    assert b'taken' in resp.data
    with unscoped():
        assert HouseholdInvite.query.one().accepted_at is None, (
            'a failed join must not consume the invitation')


# ---------------------------------------------------------------------------
# Roles  —  asserted without the tenancy backstop
# ---------------------------------------------------------------------------

def test_a_member_cannot_invite_or_remove(home_app, owner_client):
    """The half of the boundary the ORM cannot see.

    A member acting on their own household's rows never crosses a tenant
    boundary, so nothing in dough/tenancy.py has an opinion about this.
    """
    link = _invite_link(owner_client)
    member_client, _ = _join(home_app, link)
    sal = AppUser.query.filter_by(username='sal').one()

    assert member_client.post('/household/invites',
                              data={'role': 'member'}).status_code == 403
    assert member_client.post(
        f'/household/members/{sal.id}/remove').status_code == 403
    assert member_client.post(
        f'/household/members/{sal.id}/role', data={'role': 'member'}
    ).status_code == 403
    assert AppUser.query.count() == 2


def test_a_member_can_still_see_the_household_page(home_app, owner_client):
    """Read access is not the thing being restricted.

    Hiding the roster from a member would be theatre: they can already see
    every transaction in the household, so who else can is not the secret.
    """
    link = _invite_link(owner_client)
    member_client, _ = _join(home_app, link)
    resp = member_client.get('/household')
    assert resp.status_code == 200
    assert b'sal' in resp.data


def test_an_owner_can_promote_and_demote(home_app, owner_client):
    """The permission that makes the three refusals above mean something."""
    link = _invite_link(owner_client)
    _join(home_app, link)
    jamie = AppUser.query.filter_by(username='jamie').one()

    owner_client.post(f'/household/members/{jamie.id}/role', data={'role': 'owner'})
    assert AppUser.query.filter_by(username='jamie').one().role == ROLE_OWNER

    owner_client.post(f'/household/members/{jamie.id}/role', data={'role': 'member'})
    assert AppUser.query.filter_by(username='jamie').one().role == ROLE_MEMBER


# ---------------------------------------------------------------------------
# The last owner
# ---------------------------------------------------------------------------

def test_the_last_owner_cannot_be_demoted(home_app, owner_client):
    """A household with no owner is not recoverable through the UI.

    Nobody could invite, rename, or promote anyone. tools/verify_tenancy.py
    reports it as a failure for the same reason.
    """
    sal = AppUser.query.filter_by(username='sal').one()
    household_id = sal.household_id

    with tenant_scope(household_id):
        with pytest.raises(LastOwnerError):
            set_member_role(household_id, sal.id, ROLE_MEMBER)

    db.session.expire_all()
    assert AppUser.query.filter_by(username='sal').one().role == ROLE_OWNER


def test_the_last_owner_cannot_be_removed(home_app, owner_client):
    link = _invite_link(owner_client)
    _join(home_app, link)
    sal = AppUser.query.filter_by(username='sal').one()
    household_id = sal.household_id

    with tenant_scope(household_id):
        with pytest.raises(LastOwnerError):
            remove_member(household_id, sal.id)

    db.session.expire_all()
    assert AppUser.query.filter_by(username='sal').first() is not None


def test_one_of_two_owners_can_be_demoted(home_app, owner_client):
    """The permission the two refusals above are measured against.

    Without this, a `set_member_role` that raised unconditionally would satisfy
    both of them.
    """
    link = _invite_link(owner_client, role='owner')
    _join(home_app, link)
    sal = AppUser.query.filter_by(username='sal').one()

    with tenant_scope(sal.household_id):
        set_member_role(sal.household_id, sal.id, ROLE_MEMBER)

    db.session.expire_all()
    assert AppUser.query.filter_by(username='sal').one().role == ROLE_MEMBER
    assert AppUser.query.filter_by(username='jamie').one().role == ROLE_OWNER


def test_removing_yourself_is_refused(home_app, owner_client):
    """Not a safety property — another owner can always remove you.

    But a self-removal that succeeded would 500 the very next request, and
    "sign out" is what the person wanted in every case where they were not the
    last owner anyway.
    """
    link = _invite_link(owner_client, role='owner')
    _join(home_app, link)
    sal = AppUser.query.filter_by(username='sal').one()

    with tenant_scope(sal.household_id):
        with pytest.raises(MembershipError):
            remove_member(sal.household_id, sal.id, acting_user_id=sal.id)


# ---------------------------------------------------------------------------
# Tenancy still holds across households
# ---------------------------------------------------------------------------

def test_an_owner_cannot_touch_another_households_member(home_app, owner_client):
    """The tenancy half, checked here because membership is the first feature
    whose routes take an id belonging to a *person* rather than to a row of
    financial data."""
    other = Household(name='Someone else')
    db.session.add(other)
    db.session.flush()
    stranger = AppUser(username='stranger', password_hash='x',
                       household_id=other.id, role=ROLE_OWNER)
    db.session.add(stranger)
    db.session.commit()

    resp = owner_client.post(f'/household/members/{stranger.id}/remove',
                             follow_redirects=True)
    assert resp.status_code == 200
    assert b'not in this household' in resp.data
    assert AppUser.query.filter_by(username='stranger').first() is not None


def test_invitations_are_scoped_to_their_household(home_app, owner_client):
    """An owner listing invitations must see only their own."""
    sal = AppUser.query.filter_by(username='sal').one()
    other = Household(name='Someone else')
    db.session.add(other)
    db.session.flush()
    stranger = AppUser(username='stranger', password_hash='x',
                       household_id=other.id, role=ROLE_OWNER)
    db.session.add(stranger)
    db.session.commit()

    with tenant_scope(other.id):
        issue_invite(other.id, stranger, role=ROLE_MEMBER, label='theirs')
    _invite_link(owner_client, label='mine')

    resp = owner_client.get('/household')
    assert b'mine' in resp.data
    assert b'theirs' not in resp.data


def test_redeeming_binds_the_invitations_household_not_the_default(home_app,
                                                                   owner_client):
    """The join route is the only place a row is resolved before a household is
    bound. It must bind the one the *token* names."""
    sal = AppUser.query.filter_by(username='sal').one()
    other = Household(name='Someone else')
    db.session.add(other)
    db.session.flush()
    stranger = AppUser(username='stranger', password_hash='x',
                       household_id=other.id, role=ROLE_OWNER)
    db.session.add(stranger)
    db.session.commit()

    with tenant_scope(other.id):
        _, token = issue_invite(other.id, stranger, role=ROLE_MEMBER)

    visitor = home_app.test_client()
    resp = visitor.post(f'/join/{token}', data={
        'username': 'newcomer', 'password': PASSWORD, 'confirm': PASSWORD})
    assert resp.status_code == 302
    assert AppUser.query.filter_by(username='newcomer').one().household_id == other.id
    assert sal.household_id != other.id


# ---------------------------------------------------------------------------
# The service, without a request
# ---------------------------------------------------------------------------

def test_find_redeemable_invite_returns_nothing_for_a_wrong_token(home_app,
                                                                  owner_client):
    _invite_link(owner_client)
    with unscoped():
        assert find_redeemable_invite('not-a-real-token') is None
        assert find_redeemable_invite('') is None
        assert find_redeemable_invite(None) is None


def test_an_unknown_role_is_refused_rather_than_defaulted(home_app, owner_client):
    sal = AppUser.query.filter_by(username='sal').one()
    with tenant_scope(sal.household_id):
        with pytest.raises(MembershipError):
            issue_invite(sal.household_id, sal, role='administrator')
        with pytest.raises(MembershipError):
            set_member_role(sal.household_id, sal.id, 'administrator')
