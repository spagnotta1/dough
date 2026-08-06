"""Automatic categorization: the pass that runs itself after a sync.

[UAT round 1] Testers connected a bank and then had to visit a second page,
press Analyze, wait, and press Accept before their ledger meant anything. The
analysis is now run for them. These tests are about the two properties that
make that acceptable rather than alarming:

- **It is reversible and labelled.** Rules Dough wrote carry `source='ai'`, and
  removing them also stops it doing the same thing again on the next sync — an
  undo the application silently reverses is worse than no undo.
- **It never breaks the sync.** A model outage leaves the imported transactions
  uncategorized, which is where they would have been anyway.
"""

import json

import pytest

from dough.ai import EchoAdapter


@pytest.fixture()
def auto_app(tmp_path):
    """An app with a working (scripted) model and a real scheduler."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    from dough.tenancy import tenant_scope
    from models import db

    scheduler_module._scheduler = None
    adapter = EchoAdapter()
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'auto.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': adapter,
    })
    application.echo = adapter
    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


def _script(app, *replies):
    app.echo.scripted = list(replies)


def _suggestions(*pairs):
    """A scripted model reply proposing `(category, keyword)` rules."""
    return json.dumps({'suggestions': [{'category': c, 'keyword': k,
                                        'reason': 'test'}
                                       for c, k in pairs]})


def _ledger(*descriptions):
    from datetime import date
    from decimal import Decimal
    from models import Transaction, db
    for index, description in enumerate(descriptions):
        db.session.add(Transaction(account_name='Visa', date=date(2026, 8, 1),
                                   description=description,
                                   amount=Decimal(-10 - index),
                                   category='Uncategorized'))
    db.session.commit()


# ── the pass itself ─────────────────────────────────────────────────────────

def test_it_writes_rules_and_categorizes_without_being_asked(auto_app):
    from dough.services import auto_categorize, rules_service
    from models import Transaction

    _ledger('WHOLE FOODS MKT 101', 'WHOLE FOODS MKT 102', 'SHELL OIL 4432')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS'), ('Gas', 'SHELL')))

    result = auto_categorize.run_once()

    assert not result.skipped
    assert result.rules_added == 2
    assert result.transactions_categorized == 3
    assert result.remaining_uncategorized == 0
    assert rules_service.all_rules() == {'Gas': ['SHELL'],
                                         'Groceries': ['WHOLE FOODS']}
    assert Transaction.query.filter_by(category='Uncategorized').count() == 0


def test_the_rules_it_writes_are_marked_as_its_own(auto_app):
    from dough.services import auto_categorize, rules_service
    from models import CategoryRule

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.run_once()

    assert [r.source for r in CategoryRule.query.all()] == ['ai']
    assert rules_service.sources() == {'Groceries': 'ai'}


def test_a_rule_the_user_typed_is_never_marked_as_dough_s(auto_app):
    """The badge has to mean something, so `source` defaults to the user.

    A category the user started and Dough later added a keyword to stays
    theirs — `sources()` only says `'ai'` when every keyword is Dough's.
    """
    from dough.services import auto_categorize, rules_service

    rules_service.add_rule('Groceries', 'SAFEWAY')
    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.run_once()

    assert sorted(rules_service.all_rules()['Groceries']) == ['SAFEWAY',
                                                              'WHOLE FOODS']
    assert rules_service.sources() == {'Groceries': 'user'}


def test_it_declines_when_there_is_nothing_uncategorized(auto_app):
    from dough.services import auto_categorize

    result = auto_categorize.run_once()

    assert result.skipped and result.reason == 'nothing uncategorized'
    assert auto_app.echo.requests == []


def test_it_declines_when_no_model_is_configured(tmp_path):
    """No API key must not mean an exception on a background thread."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    from dough.services import auto_categorize
    from dough.tenancy import tenant_scope
    from models import db

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'nokey.db'}",
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': EchoAdapter(configured=False),
    })
    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            _ledger('WHOLE FOODS MKT 101')
            result = auto_categorize.run_once()
        db.session.remove()
    scheduler_module._scheduler = None

    assert result.skipped and result.reason == 'no model configured'


def test_a_model_failure_leaves_the_ledger_exactly_as_it_was(auto_app):
    from dough.services import auto_categorize, rules_service
    from models import Transaction

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, 'I would rather write prose than JSON.')

    result = auto_categorize.run_once()

    assert result.skipped
    assert rules_service.all_rules() == {}
    assert Transaction.query.filter_by(category='Uncategorized').count() == 1


# ── the opt-out ─────────────────────────────────────────────────────────────

def test_turning_it_off_stops_it(auto_app):
    from dough.services import auto_categorize

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.set_enabled(False)

    result = auto_categorize.run_once()

    assert result.skipped and result.reason == 'turned off for this household'
    assert auto_app.echo.requests == []


def test_clearing_the_auto_rules_also_stops_it_recreating_them(auto_app):
    """The whole point of the off switch.

    Deleting the rules alone is not an undo: the next sync reads the same
    descriptions and derives the same rules. The Rules page's "stop
    categorizing for me" does both, and this is the test that says so.
    """
    from dough.services import auto_categorize, rules_service

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')),
            _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.run_once()
    assert rules_service.all_rules() == {'Groceries': ['WHOLE FOODS']}

    response = auto_app.test_client().post(
        '/rules', data={'action': 'clear_auto'}, follow_redirects=True)
    assert response.status_code == 200

    assert rules_service.all_rules() == {}
    assert auto_categorize.is_enabled() is False
    assert auto_categorize.run_once().skipped


def test_clearing_the_auto_rules_keeps_the_users_own(auto_app):
    from dough.services import auto_categorize, rules_service

    rules_service.add_rule('Income', 'PAYROLL')
    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.run_once()

    assert rules_service.clear_auto() == 1
    assert rules_service.all_rules() == {'Income': ['PAYROLL']}


# ── the surface ─────────────────────────────────────────────────────────────

def test_accepting_a_suggestion_by_hand_is_recorded_as_the_users(auto_app):
    """`/rules/ai-apply` writes `source='user'` even though a model proposed it.

    A person read the card and pressed the button; that is the distinction the
    column exists to draw, and getting it backwards would put an "Auto" badge on
    a rule the user explicitly chose.
    """
    from dough.services import rules_service

    _ledger('WHOLE FOODS MKT 101')
    client = auto_app.test_client()
    response = client.post('/rules/ai-apply',
                           json={'category': 'Groceries',
                                 'keyword': 'WHOLE FOODS'})
    assert response.status_code == 200
    assert response.get_json()['added'] == 1
    assert rules_service.sources() == {'Groceries': 'user'}


def test_the_scheduler_reports_what_it_categorized(auto_app):
    """`/api/sync/status` carries the counts the Connections page reports.

    The page keeps polling while `categorizing` is true — stopping at
    `!running` would reload mid-analysis and tell the user everything had
    finished while Dough was still working.
    """
    from finance_sync.scheduler import get_scheduler

    scheduler = get_scheduler()
    assert scheduler is not None
    status = scheduler.status()
    assert status['categorizing'] is False
    assert status['last_categorization'] is None

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    scheduler._categorize(auto_app.config['DEFAULT_HOUSEHOLD_ID'])

    done = scheduler.status()
    assert done['categorizing'] is False
    assert done['last_categorization']['rules_added'] == 1
    assert done['last_categorization']['transactions_categorized'] == 1


def _fake_engine(transactions_added):
    """A SyncEngine stand-in that reports importing `transactions_added` rows."""
    from finance_sync.engine import ConnectionSyncResult, EngineRunResult

    class FakeEngine:
        def sync_all(self, trigger='manual'):
            return EngineRunResult(trigger=trigger, results=[
                ConnectionSyncResult(connection_id=1, institution='chase',
                                     status='success',
                                     transactions_added=transactions_added)])

        def sync_connection(self, connection_id, trigger='manual'):
            return ConnectionSyncResult(connection_id=connection_id,
                                        institution='chase', status='success',
                                        transactions_added=transactions_added)

    return FakeEngine


def test_a_refresh_that_imports_nothing_never_reaches_the_model(auto_app,
                                                               monkeypatch):
    """The billing loop this gate exists to prevent.

    A description the model cannot place stays `Uncategorized` forever. Gating
    the pass on "is anything uncategorized?" would re-send that same
    unplaceable description on every manual refresh and twice a day from the
    scheduled loop — paying for an identical prompt that cannot make progress.

    So the backlog here is deliberately non-empty: the pass must still decline,
    because the *sync* brought in nothing new to read.
    """
    from finance_sync import scheduler as scheduler_module
    from finance_sync.scheduler import get_scheduler

    _ledger('SOME UNPLACEABLE THING')
    _script(auto_app, _suggestions(('Shopping', 'UNPLACEABLE')))
    monkeypatch.setattr(scheduler_module, 'SyncEngine', _fake_engine(0))

    assert get_scheduler().run_sync(trigger='manual', wait=True) is True

    assert auto_app.echo.requests == []
    assert get_scheduler().status()['last_categorization'] is None


def test_a_refresh_that_imports_transactions_hands_off(auto_app, monkeypatch):
    from dough.services import rules_service
    from finance_sync import scheduler as scheduler_module
    from finance_sync.scheduler import get_scheduler

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    monkeypatch.setattr(scheduler_module, 'SyncEngine', _fake_engine(4))

    get_scheduler().run_sync(trigger='manual', wait=True)

    assert rules_service.sources() == {'Groceries': 'ai'}
    assert get_scheduler().status()['last_categorization'][
        'transactions_categorized'] == 1


def test_a_failed_sync_does_not_hand_off(auto_app, monkeypatch):
    """It imported nothing, so there is nothing new to read."""
    from finance_sync import scheduler as scheduler_module
    from finance_sync.scheduler import get_scheduler

    class ExplodingEngine:
        def sync_all(self, trigger='manual'):
            raise RuntimeError('every bank is down')

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    monkeypatch.setattr(scheduler_module, 'SyncEngine', ExplodingEngine)

    get_scheduler().run_sync(trigger='manual', wait=True)

    assert auto_app.echo.requests == []


def test_a_crash_inside_categorization_never_escapes(auto_app, monkeypatch):
    """It runs after a sync that already succeeded; it must not undo that."""
    from dough.services import auto_categorize
    from finance_sync.scheduler import get_scheduler

    def boom(**kwargs):
        raise RuntimeError('model on fire')

    monkeypatch.setattr(auto_categorize, 'run_once', boom)
    scheduler = get_scheduler()
    scheduler._categorize(auto_app.config['DEFAULT_HOUSEHOLD_ID'])

    assert scheduler.status()['categorizing'] is False
