"""The audit trail: that it records, that it redacts, and that it cannot lie.

Three properties, in the order they matter:

1. **Append-only.** Asserted against the ORM, not against `dough.services.audit`,
   because the guarantee has to hold for code that never calls that module.
2. **Nothing sensitive is stored.** The interesting cases are the ones no call
   site produces today, so `redact()` is tested directly rather than through
   `record()` -- a future caller passing a whole provider response is who these
   tests are for.
3. **Recording never breaks the thing being recorded.** A member removal that
   succeeded and then raised because the audit insert failed is worse than a
   missing row.

Isolation is asserted separately and specifically. `audit_events` is the one
table with a nullable `household_id`, so it is outside the ORM tenant backstop
and its isolation is `recent()` and nothing else. That makes `recent()` the
single most load-bearing function in this module, and it is tested as such.
"""

import json

import pytest

from dough.services import audit
from dough.tenancy import tenant_scope
from models import (AUDIT_EVENT_TYPES, AuditEvent, EVENT_LOGIN_FAILED,
                    EVENT_LOGIN_SUCCEEDED, EVENT_MEMBER_REMOVED, Household, db)


@pytest.fixture()
def two_households(app):
    with app.app_context():
        a = Household(name='A')
        b = Household(name='B')
        db.session.add_all([a, b])
        db.session.commit()
        yield a.id, b.id


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_record_appends_a_row(app):
    with app.app_context():
        row = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1,
                           actor_user_id=7, entity_type='user', entity_id=7)
        assert row is not None
        assert row.id is not None
        stored = db.session.get(AuditEvent, row.id)
        assert stored.event_type == EVENT_LOGIN_SUCCEEDED
        assert stored.household_id == 1
        assert stored.actor_user_id == 7
        assert stored.created_at is not None


def test_record_rejects_an_unknown_event_type(app):
    """Loud, unlike every other failure in this module.

    A typo in an event name is silent data loss discovered at the exact moment
    somebody is reconstructing an incident. It is a programming error, it is
    caught by the first test run, and it is the one thing here worth raising for.
    """
    with app.app_context():
        with pytest.raises(ValueError, match='unknown audit event type'):
            audit.record('auth.login.succeded')   # sic


def test_every_declared_event_type_is_accepted(app):
    """The constants and the frozenset cannot drift apart."""
    with app.app_context():
        for event_type in sorted(AUDIT_EVENT_TYPES):
            assert audit.record(event_type, household_id=1) is not None


def test_record_outside_an_app_context_returns_none_rather_than_raising():
    """The promise in the module docstring, at its hardest point.

    `dough/ai/service.py` calls `record()` from `_log`, which unit tests
    exercise with no Flask application at all. Before this was guarded the
    failure was a RuntimeError raised while working out *who* to attribute the
    event to -- the audit trail breaking the thing it was describing.
    """
    assert audit.record(EVENT_LOGIN_FAILED) is None


def test_record_does_not_raise_when_the_insert_fails(app, monkeypatch):
    with app.app_context():
        def boom(*a, **k):
            raise RuntimeError('database is on fire')
        monkeypatch.setattr(db.session, 'add', boom)
        assert audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1) is None


def test_record_with_commit_false_joins_the_callers_transaction(app):
    with app.app_context():
        row = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1, commit=False)
        assert row.id is not None            # flushed, so it has an id
        db.session.rollback()
        assert db.session.query(AuditEvent).count() == 0


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('key', [
    'password', 'Password', 'plaid_access_token', 'ACCESS_TOKEN', 'api_key',
    'secret', 'authorization', 'cookie', 'account_number', 'ssn', 'card_number',
    'routing_number', 'prompt', 'completion', 'message_body',
])
def test_redact_strips_by_key_name(key):
    assert audit.redact({key: 'sensitive'})[key] == audit.REDACTED


@pytest.mark.parametrize('value', [
    'sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA',
    'access-sandbox-1a2b3c4d-5e6f-7890-abcd-ef1234567890',
    '4111111111111111',
    'balance is 123456789012 today',
])
def test_redact_strips_by_value_shape(value):
    """Caught regardless of the key it arrived under.

    Deliberately crude: a false positive costs one redacted metadata value, a
    false negative puts a credential in a table nothing ever deletes from.
    """
    assert audit.redact({'note': value})['note'] == audit.REDACTED


def test_redact_keeps_ordinary_values():
    out = audit.redact({'role': 'owner', 'count': 3, 'ok': True, 'none': None})
    assert out == {'role': 'owner', 'count': 3, 'ok': True, 'none': None}


def test_redact_recurses_into_nested_structures():
    out = audit.redact({'outer': {'password': 'hunter2', 'role': 'member'}})
    assert out['outer'] == {'password': audit.REDACTED, 'role': 'member'}


def test_redact_bounds_value_length():
    out = audit.redact({'label': 'x' * 5000})
    assert len(out['label']) == audit.MAX_VALUE_CHARS


def test_recorded_metadata_is_redacted_on_the_way_in(app):
    """The redaction is a property of the stored row, not of a helper."""
    with app.app_context():
        row = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1,
                           metadata={'username': 'sal', 'password': 'hunter2'})
        stored = json.loads(db.session.get(AuditEvent, row.id).metadata_json)
        assert stored == {'username': 'sal', 'password': audit.REDACTED}


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------

def test_a_recorded_event_cannot_be_modified(app):
    with app.app_context():
        row = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1)
        row.event_type = EVENT_LOGIN_FAILED
        with pytest.raises(audit.AuditImmutableError):
            db.session.commit()
        db.session.rollback()
        assert db.session.get(AuditEvent, row.id).event_type == EVENT_LOGIN_SUCCEEDED


def test_a_recorded_event_cannot_be_deleted(app):
    with app.app_context():
        row = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1)
        db.session.delete(row)
        with pytest.raises(audit.AuditImmutableError):
            db.session.commit()
        db.session.rollback()
        assert db.session.query(AuditEvent).count() == 1


def test_bulk_delete_is_not_covered_and_that_is_stated(app):
    """The honest boundary of a before_flush hook.

    `Query.delete()` emits DELETE directly and never populates
    `session.deleted`, so the guard does not see it -- and neither would a
    reviewer who assumed "append-only" meant the database enforced it. This test
    exists to record that, not to endorse it: the guarantee is that the
    application does not rewrite its own history through the ORM, including by
    accident. An operator with a SQL prompt is outside it, and so is this.
    """
    with app.app_context():
        audit.record(EVENT_LOGIN_SUCCEEDED, household_id=1)
        db.session.query(AuditEvent).delete()
        db.session.commit()
        assert db.session.query(AuditEvent).count() == 0


# ---------------------------------------------------------------------------
# Isolation -- the whole cost of the nullable household_id
# ---------------------------------------------------------------------------

def test_recent_returns_only_this_households_events(app, two_households):
    a, b = two_households
    with app.app_context():
        audit.record(EVENT_LOGIN_SUCCEEDED, household_id=a)
        audit.record(EVENT_MEMBER_REMOVED, household_id=b)
        with tenant_scope(a):
            assert [e.event_type for e in audit.recent()] == [EVENT_LOGIN_SUCCEEDED]
        with tenant_scope(b):
            assert [e.event_type for e in audit.recent()] == [EVENT_MEMBER_REMOVED]


def test_recent_never_returns_events_with_no_household(app, two_households):
    """A failed login belongs to no tenant, and to no tenant's activity view.

    This is the reason the nullable column was acceptable: rows with a NULL
    household are for the operator reading the database, and `recent()` is the
    only read path in the application.
    """
    a, _ = two_households
    with app.app_context():
        audit.record(EVENT_LOGIN_FAILED, household_id=None,
                     metadata={'username': 'nobody'})
        with tenant_scope(a):
            assert audit.recent() == []


def test_recent_orders_newest_first(app, two_households):
    a, _ = two_households
    with app.app_context():
        first = audit.record(EVENT_LOGIN_SUCCEEDED, household_id=a)
        second = audit.record(EVENT_MEMBER_REMOVED, household_id=a)
        with tenant_scope(a):
            # Same-second timestamps are the normal case in a test, which is why
            # the ordering falls back to id -- without that this passes or fails
            # depending on clock resolution.
            assert [e.id for e in audit.recent()] == [second.id, first.id]


def test_recent_honours_its_filters(app, two_households):
    a, _ = two_households
    with app.app_context():
        audit.record(EVENT_LOGIN_SUCCEEDED, household_id=a, actor_user_id=1)
        audit.record(EVENT_MEMBER_REMOVED, household_id=a, actor_user_id=2)
        with tenant_scope(a):
            assert len(audit.recent(event_type=EVENT_LOGIN_SUCCEEDED)) == 1
            assert len(audit.recent(actor_user_id=2)) == 1
            assert len(audit.recent(limit=1)) == 1


def test_recent_refuses_to_run_without_a_household(tmp_path):
    """No implicit "all households" mode, even for an operator.

    The failure is a raised exception rather than an empty list: a read path
    that silently returned nothing when the scope was missing would look like
    "this household has no activity", which is a wrong answer rather than an
    error.

    Builds its own application rather than using the `app` fixture, which binds
    DEFAULT_HOUSEHOLD_ID for the whole test -- the same reason
    tests/test_tenancy_boundary.py does. With an ambient scope in place this
    would pass for the wrong reason, by never reaching the code it is about.
    """
    from app import create_app
    from dough.tenancy import TenantContextMissing

    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'bare.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        with pytest.raises(TenantContextMissing):
            audit.recent()


# ---------------------------------------------------------------------------
# The wiring. Everything above tests the service; these test that the flows
# actually call it, which is the part that rots.
# ---------------------------------------------------------------------------

@pytest.fixture()
def auth_app(tmp_path):
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def auth_client(auth_app):
    return auth_app.test_client()


def _events(event_type=None):
    """Every recorded event, read without a tenant scope.

    Deliberately not `audit.recent()`: these tests are about whether the flow
    recorded anything at all, and going through the scoped read path would make
    a missing `household_id` look identical to a missing call.
    """
    from dough.tenancy import unscoped
    with unscoped():
        query = AuditEvent.query
        if event_type:
            query = query.filter_by(event_type=event_type)
        return query.order_by(AuditEvent.id).all()


def _setup(client, username='sal', password='hunter2boat'):
    return client.post('/setup', data={'username': username,
                                       'password': password,
                                       'confirm': password})


def test_setup_is_recorded(auth_client):
    _setup(auth_client)
    events = _events('auth.setup.completed')
    assert len(events) == 1
    assert events[0].household_id is not None
    assert events[0].actor_user_id is not None


def test_a_successful_login_is_recorded_with_who_and_from_where(auth_client):
    _setup(auth_client)
    auth_client.post('/logout')
    auth_client.post('/login', data={'username': 'sal', 'password': 'hunter2boat'})
    event = _events(EVENT_LOGIN_SUCCEEDED)[-1]
    assert event.actor_user_id is not None
    assert event.household_id is not None
    assert event.ip_address        # provenance, from dough.auth.client_address


def test_a_failed_login_is_recorded_with_no_household(auth_client):
    """The reason `household_id` is nullable at all.

    Nobody is signed in, so there is no tenant. Attributing this to household 1
    would be inventing an association that does not exist -- and household 1 is
    somebody's real data.
    """
    _setup(auth_client)
    auth_client.post('/logout')
    auth_client.post('/login', data={'username': 'sal', 'password': 'wrong'})
    event = _events(EVENT_LOGIN_FAILED)[-1]
    assert event.household_id is None
    assert event.actor_user_id is None
    assert json.loads(event.metadata_json)['username'] == 'sal'


def test_a_failed_login_never_stores_the_attempted_password(auth_client):
    _setup(auth_client)
    auth_client.post('/logout')
    auth_client.post('/login', data={'username': 'sal',
                                     'password': 'correct-horse-battery'})
    for event in _events():
        assert 'correct-horse-battery' not in (event.metadata_json or '')


def test_logout_is_recorded_before_the_session_is_cleared(auth_client):
    """Order matters: the actor is read from the session logout destroys."""
    _setup(auth_client)
    auth_client.post('/logout')
    event = _events('auth.logout')[-1]
    assert event.actor_user_id is not None


def test_an_invitation_records_creation_without_the_token(auth_client, auth_app):
    _setup(auth_client)
    auth_client.post('/household/invites', data={'role': 'member',
                                                 'label': 'for Alex'})
    event = _events('membership.invite.created')[-1]
    metadata = json.loads(event.metadata_json)
    assert metadata['label'] == 'for Alex'
    assert metadata['role'] == 'member'

    from models import HouseholdInvite
    from dough.tenancy import unscoped
    with unscoped():
        invite = HouseholdInvite.query.one()
    # The bearer credential is never in the row that outlives everything.
    assert invite.token_hash not in (event.metadata_json or '')


def test_a_role_change_records_what_it_changed_from(auth_client, auth_app):
    _setup(auth_client)
    from dough.tenancy import unscoped
    from models import AppUser, ROLE_MEMBER, ROLE_OWNER
    with unscoped():
        owner = AppUser.query.one()
        other = AppUser(username='alex', password_hash='x',
                        household_id=owner.household_id, role=ROLE_MEMBER)
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    auth_client.post(f'/household/members/{other_id}/role',
                     data={'role': ROLE_OWNER})
    metadata = json.loads(_events('membership.role.changed')[-1].metadata_json)
    assert metadata == {'username': 'alex', 'from': ROLE_MEMBER, 'to': ROLE_OWNER}


def test_a_removal_records_who_was_removed_not_just_an_id(auth_client, auth_app):
    """After the row is gone, `entity_id=4` is not a record of anything."""
    _setup(auth_client)
    from dough.tenancy import unscoped
    from models import AppUser, ROLE_MEMBER
    with unscoped():
        owner = AppUser.query.one()
        other = AppUser(username='alex', password_hash='x',
                        household_id=owner.household_id, role=ROLE_MEMBER)
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    auth_client.post(f'/household/members/{other_id}/remove')
    event = _events(EVENT_MEMBER_REMOVED)[-1]
    assert event.entity_id == other_id
    assert json.loads(event.metadata_json)['username'] == 'alex'
