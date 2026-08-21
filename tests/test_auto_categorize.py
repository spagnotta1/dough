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
- **It reads all of it, on the deep model.** A pass that stops part-way makes
  "Dough categorizes what arrives on its own" true of the first few hundred
  descriptions and quietly false after them, and the user is never told which
  rows were skipped versus which could not be placed.
- **It says how far it has got.** Reading a whole history on the deep model is
  minutes of unattended work; the progress frames are what the dialog in
  `templates/_categorizing.html` draws so that is visible rather than
  mysterious.
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


# ── the model, and how much of the ledger it reads ──────────────────────────

def test_the_automatic_pass_reads_on_the_deep_model(auto_app):
    """Nobody is watching it, and what it writes is not an answer but rules.

    Every later categorization is derived from what this pass decides, so a
    merchant the cheap model misreads stays misread until a person notices.
    The role is asserted rather than the id: which model is "deep" is
    `dough/ai/catalog.py`'s decision to change, but that this pass takes the
    deep one is this test's.
    """
    from dough.ai import catalog
    from dough.services import auto_categorize

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    auto_categorize.run_once()

    assert catalog.resolve(role='categorize').tier == 'deep'
    assert [r.model for r in auto_app.echo.requests] == [
        catalog.provider_id(role='categorize')]


def test_pressing_analyze_is_still_the_suggest_model(auto_app):
    """The two callers are not the same job and must not drift into one.

    A person on the Rules page is waiting on the answer, and the picker on that
    page is theirs to set. Re-tiering the unattended pass must not quietly
    re-tier — and re-price — the button.
    """
    from dough.ai import catalog

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))

    response = auto_app.test_client().post('/rules/ai-suggest', json={})
    assert response.status_code == 200

    assert [r.model for r in auto_app.echo.requests] == [
        catalog.provider_id(role='suggest')]


def test_the_automatic_pass_reads_the_whole_backlog(auto_app):
    """No batch cap, on the path that used to carry the tightest one.

    This household already has a rule, so it is the *incremental* pass — the
    one that used to stop after three batches. Four batches of descriptions go
    in and four model calls come out; the old behaviour left the fourth batch
    unread, unreported, and indistinguishable from a batch the model could not
    place.
    """
    from dough.services import auto_categorize, rules_service
    from models import Transaction

    rules_service.add_rule('Groceries', 'SAFEWAY')
    batches = 4
    _ledger(*[f'MERCHANT {i:04d}'
              for i in range(auto_categorize.BATCH_SIZE * (batches - 1) + 1)])
    _script(auto_app, *[_suggestions(('Shopping', f'MERCHANT {i:04d}'))
                        for i in range(batches)])

    result = auto_categorize.run_once()

    assert len(auto_app.echo.requests) == batches
    assert result.partial is False
    assert result.first_run is False
    # The four merchants the scripted model actually named, and nothing lost to
    # a cap: every other description is still uncategorized because no rule
    # covers it, which is a different thing from never having been read.
    assert Transaction.query.filter_by(category='Shopping').count() == batches


# ── progress ────────────────────────────────────────────────────────────────

def test_it_reports_progress_as_it_reads(auto_app):
    """The frames the dialog draws: totals up front, then the read, then done.

    `transactions_total` is fixed on the first frame and never moves. A
    progress bar whose denominator grows is worse than no progress bar, and
    the numbers are transactions rather than descriptions because "412 of
    1,290 transactions" is a sentence about the user's money and "3 of 11
    batches" is one about our batching.
    """
    from dough.services import auto_categorize

    _ledger('WHOLE FOODS MKT 101', 'WHOLE FOODS MKT 102', 'SHELL OIL 4432')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS'), ('Gas', 'SHELL')))

    frames = []
    auto_categorize.run_once(on_progress=lambda p: frames.append(p.as_dict()))

    assert frames[0]['phase'] == 'reading'
    assert frames[0]['first_run'] is True
    assert frames[0]['transactions_done'] == 0
    assert frames[0]['transactions_total'] == 3
    assert frames[0]['percent'] == 0

    assert [f['transactions_total'] for f in frames] == [3] * len(frames)
    assert any(f['phase'] == 'applying' for f in frames)

    assert frames[-1]['phase'] == 'done'
    assert frames[-1]['percent'] == 100
    assert frames[-1]['transactions_categorized'] == 3


def test_a_skipped_pass_still_reports_that_it_finished(auto_app):
    """Otherwise a dialog opened on "categorizing started" never closes.

    Every early return is a path the UI has to survive, so each one reports a
    final frame rather than going quiet.
    """
    from dough.services import auto_categorize

    frames = []
    result = auto_categorize.run_once(on_progress=lambda p: frames.append(p.as_dict()))

    assert result.skipped
    assert frames[-1]['phase'] == 'done'


def test_a_progress_callback_that_raises_never_costs_the_pass(auto_app):
    """Reporting is decoration on work that must not fail.

    A UI callback that throws — a dead state lock, a test double with the
    wrong signature — must not leave the household uncategorized.
    """
    from dough.services import auto_categorize

    def boom(progress):
        raise RuntimeError('the dialog exploded')

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))

    result = auto_categorize.run_once(on_progress=boom)

    assert not result.skipped
    assert result.transactions_categorized == 1


def test_the_scheduler_publishes_progress_for_the_dialog(auto_app):
    """`/api/sync/status` is how the frames reach the browser."""
    from finance_sync.scheduler import get_scheduler

    scheduler = get_scheduler()
    assert scheduler.status()['categorization_progress'] is None

    _ledger('WHOLE FOODS MKT 101')
    _script(auto_app, _suggestions(('Groceries', 'WHOLE FOODS')))
    scheduler._categorize(auto_app.config['DEFAULT_HOUSEHOLD_ID'])

    status = scheduler.status()
    assert status['categorizing'] is False
    assert status['categorization_progress']['phase'] == 'done'
    assert status['categorization_progress']['percent'] == 100
    assert status['categorization_progress']['transactions_categorized'] == 1
    assert status['last_categorization']['first_run'] is True

    body = auto_app.test_client().get('/api/sync/status').get_json()
    assert body['categorization_progress']['percent'] == 100


# ── whose budget it spends ──────────────────────────────────────────────────

@pytest.fixture()
def budgeted(auto_app):
    """`auto_app` with the rate limiter actually switched on.

    The test config turns it off (`config.py`), which is right for a suite that
    makes thousands of requests and has no interest in production ceilings.
    These tests are *about* the ceiling, so they switch it back on for the
    length of one test and clear the counters on the way in and out — a limiter
    left armed would then fail whichever test ran next.
    """
    from dough.services.ratelimit import current_limiter

    limiter = current_limiter()
    was = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        yield auto_app
    finally:
        limiter.reset()
        limiter.enabled = was


def _spent(policy):
    """How much of `policy` this household has used, without spending more."""
    from dough.services.ratelimit import current_limiter
    from dough.tenancy import current_household

    decision = current_limiter().peek(policy, current_household())
    from dough.services.ratelimit import POLICIES
    return POLICIES[policy].limit - decision.remaining


def test_the_automatic_pass_does_not_spend_the_interactive_budget(budgeted):
    """A first connection must not cost the household its chat for an hour.

    `ai` allows sixty model calls an hour and is sized for a person clicking
    around a dashboard. The unattended pass is one burst per import, sized by
    how much history a bank handed over. Charging it to the same bucket got
    that wrong in both directions at once: a first connection could black out
    the dashboard insight and the chat immediately after signing up, *and*
    leave the pass cut off part-way through the ledger it had just promised to
    read.
    """
    from dough.services import auto_categorize

    _ledger('WHOLE FOODS MKT 101', 'SHELL OIL 4432')
    _script(budgeted, _suggestions(('Groceries', 'WHOLE FOODS'), ('Gas', 'SHELL')))

    auto_categorize.run_once()

    assert _spent('ai') == 0
    assert _spent('ai_daily') == 0
    assert _spent('ai_categorize') == 1


def test_pressing_analyze_still_spends_the_interactive_budget(budgeted):
    """The exemption is for the pass, not for the analysis it shares.

    A person can press Analyze as often as they like, and that is exactly what
    `ai` exists to bound. Keying on the *surface* rather than on the function
    is what keeps these two apart while they run the same code.
    """
    _ledger('WHOLE FOODS MKT 101')
    _script(budgeted, _suggestions(('Groceries', 'WHOLE FOODS')))

    response = budgeted.test_client().post('/rules/ai-suggest', json={})
    assert response.status_code == 200

    assert _spent('ai') == 1
    assert _spent('ai_daily') == 1
    assert _spent('ai_categorize') == 0


def test_the_pass_is_still_bounded(budgeted):
    """Its own budget, not no budget.

    `ai_categorize` is a runaway stop rather than a ration — 200 calls a day is
    24,000 distinct descriptions — but it is a real ceiling, and the pass
    degrades into a reported skip at it rather than looping. That is the whole
    difference between this and switching the limiter off around the pass: the
    other way to make it finish leaves nothing standing if it never does.
    """
    from dough.ai.errors import AIBudgetExceeded
    from dough.ai.service import current_ai
    from dough.services import auto_categorize
    from dough.services.ratelimit import POLICIES, current_limiter
    from dough.tenancy import current_household

    limiter = current_limiter()
    identity = current_household()
    for _ in range(POLICIES['ai_categorize'].limit):
        assert limiter.check('ai_categorize', identity).allowed

    _ledger('WHOLE FOODS MKT 101')
    _script(budgeted, _suggestions(('Groceries', 'WHOLE FOODS')))
    result = auto_categorize.run_once()

    assert result.skipped, 'the pass ran on past its own ceiling'

    # Refused on *that* policy, rather than quietly falling back to the
    # interactive one — which would hide a runaway inside the budget this whole
    # change exists to keep clear for the user.
    request = current_ai().build_request(
        [{'role': 'user', 'content': 'hi'}],
        metadata={'surface': 'auto_categorize'})
    with pytest.raises(AIBudgetExceeded) as exhausted:
        current_ai().generate(request)
    assert exhausted.value.policy == 'ai_categorize'
    assert _spent('ai') == 0
