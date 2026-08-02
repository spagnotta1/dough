"""`dough/services/affordability.py` — Feature 9.

The module's job is to answer "can I afford this?" from records, and its
discipline is that it never actually says yes. So most of what is tested here is
restraint: that a verdict is a band with a reason, that the worst month is
judged as well as the typical one, that thin history produces `cannot_assess`
rather than a confident guess, and that the assumptions come back with the
answer instead of staying in the module's head.

`TODAY` is the 15th, so the current month is deliberately partial — several
tests depend on it being excluded.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import affordability

TODAY = date(2026, 8, 15)


@pytest.fixture()
def post():
    from models import Transaction, db

    def _post(months_ago, income=0.0, spending=0.0, day=5):
        """One month of income and spending, `months_ago` before TODAY."""
        year, month = TODAY.year, TODAY.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        when = date(year, month, day)
        if income:
            db.session.add(Transaction(
                account_name='checking', date=when, description='Payroll ACME',
                amount=Decimal(str(income)), category='Income'))
        if spending:
            db.session.add(Transaction(
                account_name='checking', date=when, description='Living costs',
                amount=Decimal(str(-abs(spending))), category='Groceries'))
        db.session.commit()
    return _post


@pytest.fixture()
def steady(post):
    """Six complete months: $6,000 in, $4,000 out. $2,000 spare a month."""
    for months_ago in range(1, 7):
        post(months_ago, income=6000, spending=4000)
    return post


@pytest.fixture()
def with_cash(app):
    """$20,000 of checking, so one-off scenarios have something to draw on."""
    from models import AccountBalance, db

    db.session.add(AccountBalance(account_type='checking',
                                  starting_balance=20000.0))
    db.session.commit()
    return app


def _assess(**kwargs):
    kwargs.setdefault('anchor', TODAY)
    return affordability.assess(**kwargs)


# ── Capacity ────────────────────────────────────────────────────────────────

def test_capacity_measures_the_typical_month(app, steady):
    found = affordability.capacity(6, anchor=TODAY)

    assert found['can_assess'] is True
    assert found['median_monthly_income'] == 6000.0
    assert found['median_monthly_spending'] == 4000.0
    assert found['median_monthly_surplus'] == 2000.0


def test_the_partial_current_month_is_excluded(app, post):
    """Including it would drag every figure down by the days not yet lived.

    Asking for six months yields six *complete* months — the window is seven
    keys wide and the current one is dropped. The August spending posted below
    must not appear in either figure.
    """
    for months_ago in range(1, 7):
        post(months_ago, income=6000, spending=4000)
    post(0, income=0, spending=200)          # a few days into August

    found = affordability.capacity(6, anchor=TODAY)
    assert found['median_monthly_income'] == 6000.0
    assert found['median_monthly_spending'] == 4000.0
    assert found['months_measured'] == 6


def test_a_one_off_month_does_not_move_the_median(app, post):
    """A tax refund moves a mean enough to change a verdict. It must not."""
    for months_ago in range(1, 6):
        post(months_ago, income=6000, spending=4000)
    post(6, income=30000, spending=4000)     # a windfall

    found = affordability.capacity(7, anchor=TODAY)
    assert found['median_monthly_income'] == 6000.0
    assert found['mean_monthly_surplus'] > found['median_monthly_surplus']


def test_thin_history_cannot_be_assessed(app, post):
    """Two months is not a life. `cannot_assess`, not a confident guess."""
    post(1, income=6000, spending=4000)
    post(2, income=6000, spending=4000)

    found = affordability.capacity(6, anchor=TODAY)
    assert found['can_assess'] is False
    assert 'three complete months' in found['why_not']


def test_a_scenario_on_thin_history_returns_cannot_assess(app, post):
    post(1, income=6000, spending=4000)
    result = _assess(one_off=500)

    assert result['verdict'] == 'cannot_assess'
    assert result['reason']
    assert result['uncertainties']


# ── One-off costs ───────────────────────────────────────────────────────────

def test_a_small_one_off_against_healthy_cash_is_comfortable(app, steady, with_cash):
    result = _assess(one_off=2000, label='A week away')

    assert result['verdict'] == 'comfortable'
    impact = result['one_off_impact']
    assert impact['covered_by_cash'] is True
    assert impact['cash_after'] == 18000.0
    assert impact['leaves_healthy_buffer'] is True


def test_a_one_off_that_guts_the_buffer_is_tight(app, steady, with_cash):
    """$19,000 of $20,000 leaves under the three-month floor."""
    result = _assess(one_off=19000)

    assert result['verdict'] == 'tight'
    assert result['one_off_impact']['leaves_healthy_buffer'] is False
    assert 'floor' in result['reason']


def test_a_one_off_larger_than_cash_does_not_fit(app, steady, with_cash):
    result = _assess(one_off=45000, label='A car, in cash')

    assert result['verdict'] == 'not_without_changes'
    assert result['one_off_impact']['covered_by_cash'] is False
    assert result['one_off_impact']['months_to_save_from_surplus'] == 22.5


def test_months_to_save_is_none_without_a_surplus(app, post, with_cash):
    """Not a large number. There is no answer, and 999 reads as 'a long wait'."""
    for months_ago in range(1, 7):
        post(months_ago, income=4000, spending=4200)

    impact = _assess(one_off=5000)['one_off_impact']
    assert impact['months_to_save_from_surplus'] is None


# ── Recurring commitments ───────────────────────────────────────────────────

def test_a_small_payment_fits_comfortably(app, steady, with_cash):
    result = _assess(monthly=400, label='A car payment')

    assert result['verdict'] == 'comfortable'
    impact = result['monthly_impact']
    assert impact['surplus_after'] == 1600.0
    assert impact['share_of_surplus_pct'] == 20.0
    assert impact['annual_cost'] == 4800.0


def test_a_payment_taking_most_of_the_surplus_is_tight(app, steady, with_cash):
    result = _assess(monthly=1400)          # 70% of a $2,000 surplus

    assert result['verdict'] == 'tight'
    assert result['monthly_impact']['share_of_surplus_pct'] == 70.0


def test_a_payment_larger_than_the_surplus_does_not_fit(app, steady, with_cash):
    result = _assess(monthly=2500)

    assert result['verdict'] == 'not_without_changes'
    assert result['monthly_impact']['fits_in_surplus'] is False


def test_a_commitment_the_worst_month_could_not_carry_is_tight(app, post, with_cash):
    """The month that actually decides it.

    Five comfortable months and one bad one: the median says yes and the worst
    month says no, and the worst month is when a household can least absorb a
    missed payment.
    """
    for months_ago in range(1, 6):
        post(months_ago, income=6000, spending=4000)
    post(6, income=6000, spending=5800)     # a $200 month

    result = _assess(monthly=600, months=7)
    impact = result['monthly_impact']

    assert impact['fits_in_surplus'] is True
    assert impact['survives_worst_month'] is False
    assert result['verdict'] == 'tight'
    assert 'worst month' in result['reason']


def test_no_surplus_means_no_room_for_a_commitment(app, post, with_cash):
    for months_ago in range(1, 7):
        post(months_ago, income=4000, spending=4100)

    result = _assess(monthly=200)
    assert result['verdict'] == 'not_without_changes'
    assert 'no surplus' in result['reason']


def test_a_deposit_and_a_payment_are_assessed_together(app, steady, with_cash):
    """A car is both shapes at once."""
    result = _assess(one_off=5000, monthly=450, label='A car')

    assert result['one_off_impact'] is not None
    assert result['monthly_impact'] is not None
    assert result['verdict'] in affordability.VERDICTS


def test_the_worse_of_the_two_shapes_decides_the_verdict(app, steady, with_cash):
    """An affordable payment does not rescue an unaffordable deposit."""
    result = _assess(one_off=45000, monthly=100)
    assert result['verdict'] == 'not_without_changes'


# ── Never a yes ─────────────────────────────────────────────────────────────

def test_every_verdict_is_a_band_with_a_reason(app, steady, with_cash):
    for scenario in ({'one_off': 500}, {'monthly': 300}, {'one_off': 99000},
                     {'monthly': 9000}):
        result = _assess(**scenario)
        assert result['verdict'] in affordability.VERDICTS
        assert result['reason']


def test_the_answer_always_carries_its_assumptions(app, steady, with_cash):
    """The honest half of an affordability answer."""
    result = _assess(one_off=2000)

    joined = ' '.join(result['assumptions'])
    assert 'median month' in joined
    assert 'Transfers' in joined
    assert 'look broadly like the months behind' in joined


def test_uncertainties_are_never_empty(app, steady, with_cash):
    result = _assess(monthly=300)
    assert result['uncertainties']
    assert any('not advice' in note for note in result['uncertainties'])


def test_deficit_months_are_surfaced(app, post, with_cash):
    for months_ago in (1, 2, 4, 5, 6):
        post(months_ago, income=6000, spending=4000)
    post(3, income=6000, spending=7000)

    result = _assess(monthly=300, months=7)
    assert any('spent more than they earned' in note
               for note in result['uncertainties'])


def test_a_wide_swing_is_reported_as_making_the_median_a_weak_guide(app, post, with_cash):
    for months_ago, spending in enumerate([1000, 8000, 1500, 7500, 2000, 7000], start=1):
        post(months_ago, income=6000, spending=spending)

    result = _assess(monthly=300)
    assert any('weak guide' in note for note in result['uncertainties'])


def test_volatile_history_lowers_confidence_and_the_verdict(app, post, with_cash):
    for months_ago, spending in enumerate([500, 9000, 800, 8500, 1000, 9500], start=1):
        post(months_ago, income=6000, spending=spending)

    result = _assess(monthly=200)
    assert result['confidence'] in ('low', 'moderate')


def test_steady_history_earns_high_confidence(app, steady, with_cash):
    assert _assess(monthly=200)['confidence'] == 'high'


def test_nothing_to_assess_is_not_an_error(app, steady, with_cash):
    result = _assess()
    assert result['verdict'] == 'comfortable'
    assert result['reason'] == 'nothing to assess'
