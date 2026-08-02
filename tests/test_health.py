"""`dough/services/health.py` — the score, and what it refuses to score.

The arithmetic lives in `dashboard_intel.health_score` and is already covered by
`tests/test_dashboard_intel.py`. What is tested here is everything around it:
that the inputs are measured from the ledger rather than assumed, that an
unmeasurable dimension is dropped rather than guessed, and — the one that
matters most — that the dashboard's existing number did not move when two new
dimensions were added to the shared scorer.
"""

from datetime import date
from decimal import Decimal

import pytest

import dashboard_intel
from dough.services import health

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
def steady(post):
    """Six months of identical income and spending. Nothing to complain about."""
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll', 5000.00, 'Income')
        post(date(2026, month, 5), 'Corner Store', -1000.00, 'Groceries')
    return post


# ── The backwards-compatibility guarantee ───────────────────────────────────

def test_the_dashboards_four_factor_score_is_unchanged():
    """The new dimensions default to absent, so the old call is bit-identical.

    This is the test that made it safe to add to a scorer the dashboard has
    been rendering since Phase 2. If it fails, a number on a page somebody
    already trusts has silently moved.
    """
    inputs = dict(income=5000.0, outgo=3000.0, runway_months=4.0,
                  budget_map={'Groceries': 500.0},
                  category_stats={'Groceries': {'outbound': 480.0, 'inbound': 0.0}},
                  period_months=1.0, prev_outgo=3200.0)

    before = dashboard_intel.health_score(**inputs)
    assert {f['key'] for f in before['factors']} == {
        'savings', 'runway', 'budgets', 'trend'}
    assert sum(f['weight'] for f in before['factors']) == 100

    # Passing the new arguments explicitly as None must also change nothing.
    same = dashboard_intel.health_score(**inputs, cash_flow_stability=None,
                                        debt=None)
    assert same == before


def test_supplying_the_new_dimensions_adds_factors_and_renormalises():
    inputs = dict(income=5000.0, outgo=3000.0, runway_months=4.0,
                  budget_map={'Groceries': 500.0},
                  category_stats={'Groceries': {'outbound': 480.0, 'inbound': 0.0}},
                  period_months=1.0, prev_outgo=3200.0)

    richer = dashboard_intel.health_score(
        **inputs, cash_flow_stability=0.1,
        debt={'balance': 0.0, 'monthly_income': 5000.0})

    assert {f['key'] for f in richer['factors']} == {
        'savings', 'runway', 'budgets', 'trend', 'stability', 'debt'}
    assert sum(f['weight'] for f in richer['factors']) == 120
    assert 0 <= richer['score'] <= 100


# ── Inputs come from the ledger ─────────────────────────────────────────────

def test_the_score_reads_its_figures_from_the_ledger(app, steady):
    result = health.score(months=6, anchor=TODAY)

    assert result['inputs']['income'] == 30000.00      # 6 x 5000
    assert result['inputs']['spending'] == 6000.00     # 6 x 1000
    assert result['inputs']['savings_rate'] == 80.0
    assert 0 <= result['score'] <= 100
    assert result['band'] in ('strong', 'steady', 'watch', 'strained')


def test_every_factor_explains_itself(app, steady):
    """A score the UI cannot explain is a score the user is asked to trust."""
    for factor in health.score(months=6, anchor=TODAY)['factors']:
        assert factor['label'] and factor['detail']
        assert factor['status'] in ('good', 'ok', 'poor', 'unknown', 'unset')
        assert 0 <= factor['score'] <= 100


# ── Stability ───────────────────────────────────────────────────────────────

def test_identical_months_are_perfectly_stable(app, steady):
    stability = health.cash_flow_stability(6, anchor=TODAY)
    assert stability['coefficient_of_variation'] == 0.0
    assert stability['mean_net'] == 4000.00


def test_wildly_swinging_months_are_unstable(app, post):
    for month, (income, spend) in enumerate(
            [(9000, 1000), (1000, 6000), (8000, 500), (500, 7000),
             (9500, 800), (600, 6500)], start=3):
        post(date(2026, month, 1), 'Payroll', income, 'Income')
        post(date(2026, month, 5), 'Corner Store', -spend, 'Groceries')

    stability = health.cash_flow_stability(6, anchor=TODAY)
    assert stability['coefficient_of_variation'] > 0.75
    assert stability['best_month'] > 0 > stability['worst_month']


def test_alternating_months_do_not_divide_by_a_near_zero_mean(app, post):
    """+2000 and -2000 alternating has a signed mean of zero.

    Dividing by it would produce an enormous coefficient — or a crash — from a
    pattern that is lumpy rather than out of control.
    """
    for month in range(3, 9):
        if month % 2:
            post(date(2026, month, 1), 'Payroll', 2000.00, 'Income')
        else:
            post(date(2026, month, 5), 'Corner Store', -2000.00, 'Groceries')

    stability = health.cash_flow_stability(6, anchor=TODAY)
    assert stability['coefficient_of_variation'] is not None
    assert stability['coefficient_of_variation'] < 2.0


def test_two_months_is_not_enough_to_measure_stability(app, post):
    post(date(2026, 7, 1), 'Payroll', 5000.00, 'Income')
    post(date(2026, 8, 1), 'Payroll', 5000.00, 'Income')

    stability = health.cash_flow_stability(2, anchor=TODAY)
    assert stability['coefficient_of_variation'] is None
    assert 'not enough months' in stability['note']


def test_an_unmeasurable_stability_drops_the_factor_entirely(app, post):
    """Not scored as zero, not scored as perfect. Absent."""
    post(date(2026, 8, 1), 'Payroll', 5000.00, 'Income')
    result = health.score(months=2, anchor=TODAY)

    assert 'stability' not in {f['key'] for f in result['factors']}
    assert any(m['key'] == 'stability' for m in result['not_measured'])


# ── Debt ────────────────────────────────────────────────────────────────────

def test_no_credit_accounts_means_debt_is_unknown_not_zero(app, steady):
    """The most important refusal in this module.

    Scoring unlinked debt as debt-free would award the best possible rating to
    the household whose finances are least visible.
    """
    result = health.score(months=6, anchor=TODAY)

    assert result['inputs']['debt'] is None
    assert 'debt' not in {f['key'] for f in result['factors']}
    assert any(m['key'] == 'debt' and 'no credit accounts' in m['reason']
               for m in result['not_measured'])


def test_a_connected_card_is_measured_against_income(app, steady):
    from models import FinancialAccount, InstitutionConnection, db

    connection = InstitutionConnection(institution='test_bank',
                                       display_name='Test Bank', item_id='i-1')
    db.session.add(connection)
    db.session.commit()
    db.session.add(FinancialAccount(
        connection_id=connection.id, external_id='card-1', name='Visa',
        account_type='credit', balance=Decimal('10000.00'), is_active=True))
    db.session.commit()

    result = health.score(months=6, anchor=TODAY)
    debt = result['inputs']['debt']

    assert debt['balance'] == 10000.00
    assert debt['accounts'] == 1
    # 30,000 of income over ~6 months is ~5,000 a month; 10,000 owed is ~2.
    assert 1.8 <= debt['months_of_income'] <= 2.2
    assert 'debt' in {f['key'] for f in result['factors']}


# ── Investment consistency is deliberately absent ───────────────────────────

def test_investment_consistency_is_named_as_unmeasurable(app, steady):
    """Omitted rather than estimated, and the reason is carried to the reader.

    A rising balance in a rising market is indistinguishable from a
    contribution, so scoring it would mean inferring deposits from value
    changes — a guess inside a number labelled "health".
    """
    missing = health.score(months=6, anchor=TODAY)['not_measured']
    investing = next(m for m in missing if m['key'] == 'investing')
    assert 'not recorded' in investing['reason']


# ── Improvements ────────────────────────────────────────────────────────────

def test_improvements_rank_by_weighted_headroom(app, post):
    """Worst weighted gap first, so the advice matches the arithmetic."""
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll', 3000.00, 'Income')
        post(date(2026, month, 5), 'Corner Store', -2900.00, 'Groceries')

    gaps = health.score(months=6, anchor=TODAY)['improvements']
    assert gaps
    assert gaps == sorted(gaps, key=lambda g: -g['points_available'])
    for gap in gaps:
        assert gap['status'] in ('ok', 'poor', 'unknown')
        assert gap['current']


def test_a_healthy_factor_is_not_offered_as_an_improvement(app, steady):
    keys = {g['key'] for g in health.score(months=6, anchor=TODAY)['improvements']}
    good = {f['key'] for f in health.score(months=6, anchor=TODAY)['factors']
            if f['status'] == 'good'}
    assert keys.isdisjoint(good)


def test_an_empty_ledger_still_produces_a_score(app):
    """No income, no spending, no crash — and nothing claimed."""
    result = health.score(months=6, anchor=TODAY)
    assert 0 <= result['score'] <= 100
    assert result['inputs']['income'] == 0.0
    assert result['inputs']['savings_rate'] is None
