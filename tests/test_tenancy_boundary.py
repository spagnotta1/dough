"""The tenant isolation boundary, specified before tenancy exists.

Per operating constraint 3, ORM-level filtering is defense-in-depth, never the
authorization mechanism. That distinction is what this file exists to enforce,
and it is why the tests are written now rather than alongside the
implementation: if the isolation tests were written in the same phase as the
`with_loader_criteria` event, every one of them would pass because of that
event, and nobody would ever find out that the route layer checks nothing.

So the contract has two independent halves, and Phase 5 must satisfy both:

  1. WITH the ORM event installed -- cross-tenant reads return nothing and
     cross-tenant writes raise.
  2. WITH the ORM event bypassed (`unscoped()`) -- routes that resolve a
     caller-supplied id STILL refuse to serve another household's row, because
     they carry their own explicit household predicate.

Half 2 is the one that matters. A route that only passes half 1 is a route that
will leak the day someone adds a `session.get()`, a raw `text()` query, a bulk
`update()`, or a background job that runs outside request context -- all four of
which already exist in this codebase.

Everything here was xfail(strict=True) until Phase 5 implemented it. Strict, so
the phase that implemented tenancy could not leave the marker on: an XPASS
fails the build, which is what forced this docstring to be revisited rather
than the file to be quietly forgotten.
"""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app


@pytest.fixture()
def tenant_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def two_households(tenant_app):
    """Two households, each with one transaction, one budget, one holding.

    Returns (household_a_id, household_b_id).
    """
    from datetime import date
    from decimal import Decimal

    from dough.tenancy import tenant_scope, unscoped
    from models import Budget, Household, Transaction, db

    with unscoped():
        a = Household(name='A', plaid_user_id='hh-a')
        b = Household(name='B', plaid_user_id='hh-b')
        db.session.add_all([a, b])
        db.session.commit()
        a_id, b_id = a.id, b.id

    for hid, tag in ((a_id, 'a'), (b_id, 'b')):
        with tenant_scope(hid):
            db.session.add(Transaction(
                account_name=f'checking-{tag}', date=date(2026, 1, 5),
                description=f'coffee-{tag}', amount=Decimal('-4.50'),
                category='Dining'))
            # `monthly_limit`, not `amount` -- the column this spec was written
            # against in Phase 0 does not exist and never did. Corrected when
            # the spec was made to run rather than renaming the column to match
            # it: the field name has nothing to do with tenancy, and a schema
            # change smuggled in under a security phase is how unrelated
            # breakage gets attributed to the wrong commit.
            db.session.add(Budget(category='Dining',
                                  monthly_limit=Decimal('100'),
                                  account_name=f'checking-{tag}'))
            db.session.commit()

    return a_id, b_id


# ---------------------------------------------------------------------------
# Half 1 -- the ORM event as a backstop.
# ---------------------------------------------------------------------------

def test_select_returns_only_the_current_household(two_households):
    from dough.tenancy import tenant_scope
    from models import Transaction

    a_id, b_id = two_households
    with tenant_scope(a_id):
        rows = Transaction.query.all()
        assert len(rows) == 1
        assert rows[0].description == 'coffee-a'
    with tenant_scope(b_id):
        rows = Transaction.query.all()
        assert len(rows) == 1
        assert rows[0].description == 'coffee-b'


def test_missing_tenant_context_raises_rather_than_leaking(tenant_app):
    """The critical failure mode: no context must never mean "all rows".

    Returning zero rows would be nearly as bad -- a broken tenant context would
    look exactly like an empty account, and would be debugged as a data problem
    rather than a security one.
    """
    from dough.tenancy import TenantContextMissing
    from models import Transaction

    with pytest.raises(TenantContextMissing):
        Transaction.query.all()


def test_aggregates_are_scoped(two_households):
    """func.sum() over a scoped entity must not cross households.

    finance_sync/repository.py computes net worth this way, so an unscoped
    aggregate would mean one household's net worth silently including another's.
    """
    from sqlalchemy import func

    from dough.tenancy import tenant_scope
    from models import Transaction, db

    a_id, _ = two_households
    with tenant_scope(a_id):
        total = db.session.query(func.sum(Transaction.amount)).scalar()
        assert float(total) == -4.50


def test_insert_is_stamped_with_the_current_household(two_households):
    from datetime import date
    from decimal import Decimal

    from dough.tenancy import tenant_scope
    from models import Transaction, db

    a_id, _ = two_households
    with tenant_scope(a_id):
        txn = Transaction(account_name='checking-a', date=date(2026, 2, 1),
                          description='new', amount=Decimal('-1.00'),
                          category='Dining')
        db.session.add(txn)
        db.session.commit()
        assert txn.household_id == a_id


def test_insert_without_tenant_context_raises(tenant_app):
    from datetime import date
    from decimal import Decimal

    from dough.tenancy import TenantContextMissing
    from models import Transaction, db

    db.session.add(Transaction(account_name='x', date=date(2026, 1, 1),
                               description='y', amount=Decimal('-1.00'),
                               category='Dining'))
    with pytest.raises(TenantContextMissing):
        db.session.commit()


def test_writing_a_foreign_household_id_raises(two_households):
    from datetime import date
    from decimal import Decimal

    from dough.tenancy import CrossTenantWrite, tenant_scope
    from models import Transaction, db

    a_id, b_id = two_households
    with tenant_scope(a_id):
        db.session.add(Transaction(
            household_id=b_id, account_name='x', date=date(2026, 1, 1),
            description='y', amount=Decimal('-1.00'), category='Dining'))
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
    db.session.rollback()


def test_reparenting_an_existing_row_raises(two_households):
    from dough.tenancy import CrossTenantWrite, tenant_scope
    from models import Transaction, db

    a_id, b_id = two_households
    with tenant_scope(a_id):
        txn = Transaction.query.one()
        txn.household_id = b_id
        with pytest.raises(CrossTenantWrite):
            db.session.commit()
    db.session.rollback()


def test_bulk_update_and_delete_are_scoped(two_households):
    from dough.tenancy import tenant_scope, unscoped
    from models import Transaction, db

    a_id, b_id = two_households
    with tenant_scope(a_id):
        Transaction.query.update({'category': 'Changed'})
        db.session.commit()
    with tenant_scope(b_id):
        assert Transaction.query.one().category == 'Dining'

    with tenant_scope(a_id):
        Transaction.query.delete()
        db.session.commit()
    with unscoped():
        assert Transaction.query.count() == 1


def test_both_households_can_import_the_identical_csv_row(two_households):
    """The content-dedupe unique index must be household-composite.

    repository.py relies on IntegrityError inside begin_nested() to dedupe CSV
    imports by (account_name, date, description, amount). If that index is not
    scoped, the second household importing the same statement row silently
    loses it -- a data-loss bug that looks like a dedupe feature working.
    """
    from datetime import date
    from decimal import Decimal

    from dough.tenancy import tenant_scope
    from models import Transaction, db

    a_id, b_id = two_households
    for hid in (a_id, b_id):
        with tenant_scope(hid):
            db.session.add(Transaction(
                account_name='shared', date=date(2026, 3, 3),
                description='NETFLIX', amount=Decimal('-15.99'),
                category='Entertainment'))
            db.session.commit()

    for hid in (a_id, b_id):
        with tenant_scope(hid):
            assert Transaction.query.filter_by(description='NETFLIX').count() == 1


def test_count_is_scoped_like_the_query_it_counts(two_households):
    """Added in Phase 5, after implementation turned up a leak the spec missed.

    `Query.count()` does not execute the query it is called on. It freezes it
    into a subquery and runs `SELECT count(*) FROM (<frozen>) AS anon_1`, so by
    the time the `do_orm_execute` backstop sees the statement there is no mapped
    entity left in it — `all_mappers` is empty, the handler decides no tenant
    data is involved, and the count comes back across every household while
    `.all()` on the identical query returns one household's rows.

    It surfaced through the CSV-dedupe test, which happened to be the only case
    with both households holding a row matching the same filter. Every other
    test would have counted the right number for the wrong reason. Asserted
    directly here so the fix cannot regress behind an accident.
    """
    from dough.tenancy import tenant_scope, unscoped
    from models import Transaction

    a_id, b_id = two_households
    with unscoped():
        assert Transaction.query.count() == 2, 'fixture should hold one row each'

    for household_id in (a_id, b_id):
        with tenant_scope(household_id):
            assert Transaction.query.count() == 1
            assert Transaction.query.filter_by(category='Dining').count() == 1
            # The invariant that actually matters: the two agree.
            assert (Transaction.query.count()
                    == len(Transaction.query.all()))


def test_ai_caches_do_not_leak_between_households(two_households):
    """app.py:36-38 hold process-global caches keyed only by time.

    _insight_cache, _brief_cache and _wealth_cache contain generated prose about
    a household's actual spending. Keyed by time alone, household B is served
    household A's financial summary. Highest-severity finding of the audit.

    Asserted behaviourally -- a value stored under household A must be invisible
    from household B -- rather than by inspecting key shapes, so that any
    correct implementation satisfies it and an empty cache cannot make it pass
    vacuously.
    """
    from dough.services.cache import TenantScopedTTLCache
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households
    cache = TenantScopedTTLCache(ttl=3600)

    with tenant_scope(a_id):
        cache.set('brief', 'household A spent $4.50 on coffee')
        assert cache.get('brief') == 'household A spent $4.50 on coffee'

    with tenant_scope(b_id):
        assert cache.get('brief') is None, 'household B can read A\'s cached insight'
        cache.set('brief', 'household B summary')

    with tenant_scope(a_id):
        assert cache.get('brief') == 'household A spent $4.50 on coffee'


# ---------------------------------------------------------------------------
# Half 2 -- explicit authorization, proven with the ORM backstop switched off.
# ---------------------------------------------------------------------------

# (path template, method) for every route that resolves a caller-supplied id.
# Each must refuse another household's row on its own, without the ORM event.
OWNED_RESOURCE_ROUTES = [
    ('/transactions/{txn_id}', 'PUT'),
    ('/transactions/{txn_id}', 'DELETE'),
    ('/anomalies/{txn_id}/dismiss', 'POST'),
]


@pytest.mark.parametrize('template,method', OWNED_RESOURCE_ROUTES)
def test_routes_deny_foreign_ids_without_the_orm_backstop(
        tenant_app, two_households, template, method):
    """The load-bearing test of this file.

    Runs each route against a row belonging to the *other* household while the
    ORM tenant filter is bypassed, proving the route carries its own explicit
    household predicate. If a route only passes with the filter installed, it is
    relying on defense-in-depth as its primary defense -- which is the exact
    thing constraint 3 forbids.
    """
    from dough.tenancy import tenant_scope, unscoped
    from models import Transaction

    a_id, b_id = two_households
    with unscoped():
        foreign = Transaction.query.filter_by(description='coffee-b').one()
        foreign_id = foreign.id

    client = tenant_app.test_client()
    path = template.format(txn_id=foreign_id)

    with tenant_scope(a_id):
        with unscoped():   # backstop off: only explicit checks remain
            resp = client.open(path, method=method, json={'category': 'Hacked'})

    assert resp.status_code in (403, 404), (
        f'{method} {path} returned {resp.status_code} for another household\'s '
        f'row with the ORM filter bypassed -- the route has no explicit '
        f'household check of its own')

    with unscoped():
        assert Transaction.query.get(foreign_id) is not None
        assert Transaction.query.get(foreign_id).category != 'Hacked'


def test_get_owned_helper_refuses_foreign_rows(two_households):
    """dough.tenancy.get_owned is the sanctioned way to resolve an id."""
    from werkzeug.exceptions import NotFound

    from dough.tenancy import get_owned, tenant_scope, unscoped
    from models import Transaction

    a_id, _ = two_households
    with unscoped():
        foreign_id = Transaction.query.filter_by(description='coffee-b').one().id
        own_id = Transaction.query.filter_by(description='coffee-a').one().id

    with tenant_scope(a_id):
        assert get_owned(Transaction, own_id).id == own_id
        with pytest.raises(NotFound):
            get_owned(Transaction, foreign_id)
        # ...and still refuses with the backstop switched off.
        with unscoped():
            with pytest.raises(NotFound):
                get_owned(Transaction, foreign_id)


def test_background_sync_binds_a_household_on_its_own_thread(
        tenant_app, two_households, monkeypatch):
    """The entry point ContextVar was chosen for, exercised on a real thread.

    `threading.Thread` starts with an empty context — it does not inherit the
    caller's. That is the property that stops a worker silently continuing to
    serve whichever household spawned it, and it is also what means the
    scheduler has to re-bind one deliberately. Asserted by running the real
    threaded path (`wait=False`) rather than the inline one every other sync
    test uses, because inline work inherits the caller's context and would pass
    whether or not the re-binding exists.
    """
    import threading

    import finance_sync.scheduler as scheduler_module
    from dough.tenancy import current_household

    seen = []
    done = threading.Event()

    class _RecordingEngine:
        def sync_all(self, trigger='manual'):
            seen.append(current_household())
            done.set()
            return type('R', (), {'status': 'success'})()

    monkeypatch.setattr(scheduler_module, 'SyncEngine', _RecordingEngine)
    scheduler = scheduler_module.SyncScheduler(tenant_app)

    a_id, _ = two_households
    assert scheduler.run_sync(trigger='manual', wait=False, household_id=a_id)
    assert done.wait(timeout=10), 'background sync never ran'
    assert seen == [a_id]


def test_a_sync_with_no_household_refuses_rather_than_guessing(tenant_app):
    """No caller and no explicit household is a bug, not "sync everything".

    Left to guess, the plausible guesses are both wrong: syncing nothing looks
    like a working scheduler with no connections, and syncing everything under
    one context writes one family's transactions into another's ledger.
    """
    import finance_sync.scheduler as scheduler_module

    scheduler = scheduler_module.SyncScheduler(tenant_app)
    with pytest.raises(RuntimeError, match='household'):
        scheduler.run_sync(trigger='scheduled', wait=True)


def test_no_row_can_exist_without_a_household(tenant_app, two_households):
    """Sweep every tenant-scoped table for orphans.

    Background work is the likely source of one: finance_sync/scheduler.py opens
    a fresh app context per work item with no request, so nothing sets the tenant
    context for it. If that entry point is missed, syncs either raise or write
    rows with a NULL household -- rows that then belong to nobody and are visible
    to everybody. Checking the invariant directly covers that entry point and any
    future one, without asserting on a particular function name.
    """
    from dough.tenancy import TenantScopedMixin, unscoped
    from models import db

    scoped = [m.class_ for m in db.Model.registry.mappers
              if issubclass(m.class_, TenantScopedMixin)]
    assert scoped, 'no models are tenant-scoped'

    with unscoped():
        orphaned = {
            model.__tablename__: model.query.filter(model.household_id.is_(None)).count()
            for model in scoped
        }
    assert not any(orphaned.values()), f'rows with no household: {orphaned}'
