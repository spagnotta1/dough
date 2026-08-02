"""`dough/ai/copilot.py` — the orchestrator.

An orchestration layer is easy to write and easy to write pointlessly: a class
that forwards each call to the service that already did the work adds a file and
buys nothing. So the tests here are mostly about whether it is *earning its
place* — that the coordinated pass really does collapse the duplicate work, that
the cache really is scoped to a household, and that a question really is
answered from retrieved figures rather than from the model's own arithmetic.

The two that matter most:

- `test_one_pass_calls_each_expensive_service_once` — the reason the module
  exists. It counts calls, so a surface added to `analytics()` later cannot
  quietly reintroduce the duplication.
- `test_the_analytics_cache_is_scoped_to_a_household` — SEC-0003 territory. A
  cache holding one family's computed figures must be invisible to another.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.ai.base import EchoAdapter
from dough.ai.copilot import FinancialCopilot, current_copilot
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import AIService

TODAY = date(2026, 8, 15)


@pytest.fixture()
def post():
    from models import Transaction, db

    def _post(when, description, amount, category='Groceries',
              account='checking'):
        db.session.add(Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category))
        db.session.commit()
    return _post


@pytest.fixture()
def household(app, post):
    """Six months of history with one deliberate story: Netflix reprices."""
    for month in range(2, 9):
        post(date(2026, month, 1), 'Payroll ACME', 5000.00, 'Income')
        post(date(2026, month, 2), 'Landlord', -1800.00, 'Rent')
        post(date(2026, month, 4), 'Whole Foods', -(300 + month * 30),
             'Groceries')
        post(date(2026, month, 6), 'Netflix', -15.99, 'Streaming')
    post(date(2026, 8, 10), 'Netflix', -24.99, 'Streaming')
    return app


def _copilot(configured=False, **kwargs):
    """A copilot over an EchoAdapter, which never reaches the network."""
    ai = AIService(EchoAdapter(configured=configured))
    return FinancialCopilot(ai, anchor=TODAY, **kwargs)


# ── The coordinated pass: the reason this module exists ─────────────────────

def test_one_pass_calls_each_expensive_service_once(household, monkeypatch):
    """The claim the whole orchestrator rests on.

    `anomalies.detect` is the costliest call in the analytics layer, and three
    of the things this page needs run it internally. If this ever counts more
    than one, the duplication is back and nothing else will report it.
    """
    from dough.services import anomalies, periods

    detect_calls, compare_calls = [], []
    original_detect, original_compare = anomalies.detect, periods.compare

    def counting_detect(*args, **kwargs):
        detect_calls.append(1)
        return original_detect(*args, **kwargs)

    def counting_compare(*args, **kwargs):
        compare_calls.append(1)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(anomalies, 'detect', counting_detect)
    monkeypatch.setattr(periods, 'compare', counting_compare)

    run = _copilot().analytics()

    assert len(detect_calls) == 1, f'detect ran {len(detect_calls)} times'
    assert len(compare_calls) == 1, f'compare ran {len(compare_calls)} times'
    assert run['findings'] and run['insights'] is not None


def test_the_pass_carries_everything_a_surface_needs(household):
    run = _copilot().analytics()

    assert set(run) >= {'window', 'summary', 'comparison', 'findings',
                        'anomaly_summary', 'trends', 'health', 'insights'}
    assert run['window']['label'] == 'August 2026'
    assert 0 <= run['health']['score'] <= 100


def test_building_a_context_does_not_rerun_the_detector(household, monkeypatch):
    """The precomputed findings are threaded into `ai_context.build`."""
    from dough.services import anomalies

    calls = []
    original = anomalies.detect

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(anomalies, 'detect', counting)

    copilot = _copilot()
    copilot.analytics()
    copilot.context('brief')
    copilot.context('ask')

    assert len(calls) == 1


def test_the_pass_is_memoised_within_one_instance(household, monkeypatch):
    from dough.services import anomalies

    calls = []
    original = anomalies.detect
    monkeypatch.setattr(anomalies, 'detect',
                        lambda *a, **k: (calls.append(1), original(*a, **k))[1])

    copilot = _copilot()
    first, second = copilot.analytics(), copilot.analytics()

    assert first is second
    assert len(calls) == 1


def test_refresh_recomputes(household, monkeypatch):
    from dough.services import anomalies

    calls = []
    original = anomalies.detect
    monkeypatch.setattr(anomalies, 'detect',
                        lambda *a, **k: (calls.append(1), original(*a, **k))[1])

    copilot = _copilot()
    copilot.analytics()
    copilot.analytics(refresh=True)

    assert len(calls) == 2


def test_invalidate_drops_the_cached_pass(household):
    """A user who has just uploaded a statement must not see stale figures."""
    from dough.services.cache import TenantScopedTTLCache

    cache = TenantScopedTTLCache(ttl=600)
    copilot = _copilot(cache=cache)
    copilot.analytics()
    assert cache.get('analytics', 'default|6') is not None

    copilot.invalidate()
    assert cache.get('analytics', 'default|6') is None


# ── Caching and tenancy ─────────────────────────────────────────────────────

def test_the_analytics_cache_is_scoped_to_a_household(tenant_two_households):
    """SEC-0003 territory: computed figures are as disclosing as prose.

    Household A's cached pass must be invisible from B, even though both use
    the same cache instance and the same variant key.
    """
    from dough.services.cache import TenantScopedTTLCache
    from dough.tenancy import tenant_scope

    a_id, b_id = tenant_two_households
    cache = TenantScopedTTLCache(ttl=600)

    with tenant_scope(a_id):
        a_run = _copilot(cache=cache).analytics()
        assert a_run['summary']['spending'] == 111.11

    with tenant_scope(b_id):
        b_run = _copilot(cache=cache).analytics()
        assert b_run['summary']['spending'] == 999.99

    with tenant_scope(a_id):
        assert _copilot(cache=cache).analytics()['summary']['spending'] == 111.11


def test_a_cache_variant_distinguishes_windows(household):
    """A pass about one window must not be served for another."""
    from dough.services.analytics import resolve_window
    from dough.services.cache import TenantScopedTTLCache

    cache = TenantScopedTTLCache(ttl=600)
    copilot = _copilot(cache=cache)

    august = copilot.analytics()
    july = copilot.analytics(window=resolve_window('month', date(2026, 7, 15)))

    assert august['window']['label'] == 'August 2026'
    assert july['window']['label'] == 'July 2026'


# ── Context assembly ────────────────────────────────────────────────────────

def test_each_surface_gets_only_the_sections_it_needs(household):
    """A budget coach shipping the portfolio pays tokens to be slower."""
    budgets = _copilot().context('budgets')
    ask = _copilot().context('ask')

    assert 'budgets' in budgets
    assert 'portfolio' not in budgets
    assert 'top_merchants' not in budgets
    assert 'top_merchants' in ask and 'recurring' in ask


def test_an_explicit_section_list_overrides_the_surface(household):
    context = _copilot().context('ask', sections=['period'])
    assert 'period' in context
    assert 'trends' not in context


def test_the_context_is_json_serialisable(household):
    import json

    assert json.loads(json.dumps(_copilot().context('review'), default=str))


# ── Retrieval before generation ─────────────────────────────────────────────

def test_a_matched_question_puts_retrieved_figures_in_the_prompt(household):
    """The point of the retrieval step.

    The model must be handed the total rather than a pile of transactions to
    add up, because a total computed in prose is indistinguishable from a real
    one and is wrong often enough to matter.
    """
    prompt = _copilot().system_prompt('How much did I spend on groceries this year?')

    assert 'Figures retrieved for this specific question' in prompt
    assert 'total_spent' in prompt


def test_an_unmatched_question_omits_the_retrieval_block(household):
    """An empty result presented as an answer is how "$0" gets said wrongly."""
    prompt = _copilot().system_prompt('is the moon made of cheese')

    assert 'Figures retrieved for this specific question' not in prompt
    assert 'General financial snapshot' in prompt


def test_the_prompt_always_carries_the_grounding_rules(household):
    prompt = _copilot().system_prompt('how much did I spend')

    assert 'null` means the figure could not be computed' in prompt
    assert 'Never add up a list of transactions' in prompt


def test_retrieval_failure_degrades_to_the_general_snapshot(household, monkeypatch):
    """A parser bug must not fail the question."""
    from dough.services import finsearch

    monkeypatch.setattr(finsearch, 'search',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('boom')))

    prompt = _copilot().system_prompt('how much did I spend on groceries')
    assert 'General financial snapshot' in prompt


# ── Availability: briefings degrade, questions refuse ───────────────────────

def test_a_briefing_degrades_when_no_model_is_configured(household):
    """Optional furniture on a page. The card is omitted, not an error."""
    copilot = _copilot(configured=False)

    assert copilot.brief() == {'available': False}
    assert copilot.monthly_review() == {'available': False}
    assert copilot.budget_coaching() == {'available': False}


def test_a_question_refuses_when_no_model_is_configured(household):
    """Somebody typed this and pressed send; silence is worse than a message."""
    with pytest.raises(AIConfigurationError):
        list(_copilot(configured=False).answer('how much did I spend?'))


def test_analytics_never_need_a_model(household):
    """The whole read-only half must work with the AI switched off."""
    copilot = _copilot(configured=False)

    assert copilot.analytics()['health']['score'] >= 0
    assert copilot.insights() is not None
    assert copilot.unusual_activity() is not None
    assert copilot.context('brief')


def test_a_generation_failure_degrades_rather_than_raising(household, monkeypatch):
    copilot = _copilot(configured=True)

    def boom(*args, **kwargs):
        raise AIError('provider exploded')

    monkeypatch.setattr(copilot.ai, 'generate_json', boom)
    result = copilot.brief()

    assert result['available'] is False


# ── Budget projection: arithmetic, not a model output ───────────────────────

def test_the_budget_projection_is_computed_not_generated(household):
    """"On track to finish at $612" is exactly what a model produces wrongly."""
    from dough.ai.copilot import _project_budgets

    projected = _project_budgets({
        'available': True, 'month_progress_pct': 50,
        'budgets': [{'category': 'Groceries', 'limit': 500.0, 'spent': 300.0}],
    })

    assert projected['available'] is True
    row = projected['budgets'][0]
    assert row['projected_month_end'] == 600.0      # 300 spent at the halfway mark
    assert row['projected_pct_of_limit'] == 120.0
    assert row['on_track'] is False


def test_an_early_projection_says_it_is_low_confidence(household):
    """Day-three arithmetic must not be presented as a forecast."""
    from dough.ai.copilot import _project_budgets

    early = _project_budgets({
        'available': True, 'month_progress_pct': 10,
        'budgets': [{'category': 'Groceries', 'limit': 500.0, 'spent': 100.0}],
    })
    assert early['budgets'][0]['confidence'] == 'low'

    late = _project_budgets({
        'available': True, 'month_progress_pct': 80,
        'budgets': [{'category': 'Groceries', 'limit': 500.0, 'spent': 400.0}],
    })
    assert late['budgets'][0]['confidence'] == 'high'


def test_no_budgets_projects_nothing_rather_than_an_empty_list(household):
    from dough.ai.copilot import _project_budgets

    assert _project_budgets({'available': False})['available'] is False
    assert _project_budgets(None)['available'] is False


def test_budgets_ranked_by_how_far_over_they_project(household):
    from dough.ai.copilot import _project_budgets

    projected = _project_budgets({
        'available': True, 'month_progress_pct': 50,
        'budgets': [
            {'category': 'Safe', 'limit': 1000.0, 'spent': 200.0},
            {'category': 'Blown', 'limit': 100.0, 'spent': 200.0},
        ],
    })
    assert [b['category'] for b in projected['budgets']] == ['Blown', 'Safe']


# ── Wiring ──────────────────────────────────────────────────────────────────

def test_current_copilot_uses_the_apps_ai_service(household):
    copilot = current_copilot(anchor=TODAY)

    from dough.ai.service import current_ai
    assert copilot.ai is current_ai()


def test_answer_streams_text_chunks(household):
    """The Echo adapter returns the prompt, which is enough to prove the path."""
    copilot = _copilot(configured=True)
    chunks = list(copilot.answer('how much did I spend on groceries?'))

    assert chunks
    assert all(isinstance(chunk, str) for chunk in chunks)


# ── Fixtures for the tenancy test ───────────────────────────────────────────

@pytest.fixture()
def tenant_two_households(tmp_path):
    """Two households with different figures and no ambient scope.

    Built here rather than reusing the shared `app` fixture for the reason
    `tests/test_tenancy_boundary.py` documents: with a household already bound
    for the whole test, an isolation assertion passes for the wrong reason.
    """
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'copilot.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': EchoAdapter(configured=False),
    })

    from dough.tenancy import tenant_scope, unscoped
    from models import Household, Transaction, db

    with application.app_context():
        with unscoped():
            a = Household(name='A', plaid_user_id='cp-a')
            b = Household(name='B', plaid_user_id='cp-b')
            db.session.add_all([a, b])
            db.session.commit()
            a_id, b_id = a.id, b.id

        for hid, amount, payee in ((a_id, '-111.11', 'Alpha Grocers'),
                                   (b_id, '-999.99', 'Beta Boutique')):
            with tenant_scope(hid):
                db.session.add(Transaction(
                    account_name='checking', date=date(2026, 8, 4),
                    description=payee, amount=Decimal(amount),
                    category='Groceries'))
                db.session.commit()

        yield a_id, b_id
        db.session.remove()

    scheduler_module._scheduler = None
