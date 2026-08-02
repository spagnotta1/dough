"""`dough/services/periods.py` — the "what changed?" engine.

The engine's whole value is that it does the subtraction so the model does not,
so these tests are about the *judgement* around the subtraction rather than the
subtraction itself: what clears the threshold, what is suppressed as noise, what
gets a percentage and what must not, and which way a comparison points.

The ledger is built per test rather than shared, because most of these need a
specific shape — a category that vanished, a category with no baseline, a swing
that is large in dollars and small in percent — and a single fixture carrying
all of them is a fixture nobody can read.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import analytics, periods

FEB = analytics.resolve_window('month', date(2026, 2, 15))
MAR = analytics.resolve_window('month', date(2026, 3, 15))


@pytest.fixture()
def post():
    """Commit one transaction. Returns the poster so tests read as a ledger."""
    from models import Transaction, db

    def _post(when, description, amount, category, account='checking'):
        db.session.add(Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category))
        db.session.commit()
    return _post


# ── Direction and materiality ───────────────────────────────────────────────

def test_a_material_increase_is_reported(app, post):
    post(date(2026, 2, 5), 'Olive Garden', -200.00, 'Dining')
    post(date(2026, 3, 5), 'Olive Garden', -400.00, 'Dining')

    result = periods.compare(MAR)
    dining = next(f for f in result['categories'] if f['category'] == 'Dining')
    assert dining['direction'] == 'increase'
    assert dining['delta'] == 200.00
    assert dining['pct'] == 100.0


def test_a_small_dollar_swing_is_suppressed_however_large_the_percentage(app, post):
    """$4 to $8 doubled and is not worth a sentence.

    This is the finding that makes an assistant look broken: technically a 100%
    rise, actually a second coffee.
    """
    post(date(2026, 2, 5), 'Blue Bottle', -4.00, 'Dining')
    post(date(2026, 3, 5), 'Blue Bottle', -8.00, 'Dining')

    assert periods.compare(MAR)['categories'] == []


def test_a_small_percentage_swing_is_suppressed_however_large_the_dollars(app, post):
    """$2,000 to $2,090 is $90 of real money and a 4.5% wobble."""
    post(date(2026, 2, 5), 'Landlord', -2000.00, 'Rent')
    post(date(2026, 3, 5), 'Landlord', -2090.00, 'Rent')

    assert periods.compare(MAR)['categories'] == []


def test_a_swing_clearing_both_thresholds_survives(app, post):
    post(date(2026, 2, 5), 'Landlord', -2000.00, 'Rent')
    post(date(2026, 3, 5), 'Landlord', -2800.00, 'Rent')

    findings = periods.compare(MAR)['categories']
    assert [f['category'] for f in findings] == ['Rent']
    assert findings[0]['pct'] == 40.0


# ── No baseline ─────────────────────────────────────────────────────────────

def test_a_new_category_appears_and_carries_no_percentage(app, post):
    """`pct` must be None, not 100, and not omitted.

    A formatter that prints a percentage would otherwise print a fabricated one.
    """
    post(date(2026, 2, 5), 'Whole Foods', -300.00, 'Groceries')
    post(date(2026, 3, 5), 'Whole Foods', -300.00, 'Groceries')
    post(date(2026, 3, 11), 'Delta Air Lines', -340.00, 'Travel')

    travel = next(f for f in periods.compare(MAR)['categories']
                  if f['category'] == 'Travel')
    assert travel['direction'] == 'appeared'
    assert travel['pct'] is None
    assert travel['previous'] == 0.0


def test_a_category_that_stopped_is_reported_as_disappeared(app, post):
    post(date(2026, 2, 11), 'Delta Air Lines', -340.00, 'Travel')

    travel = next(f for f in periods.compare(MAR)['categories']
                  if f['category'] == 'Travel')
    assert travel['direction'] == 'disappeared'
    assert travel['current'] == 0.0
    assert travel['delta'] == -340.00


def test_an_appearance_below_the_dollar_floor_is_still_suppressed(app, post):
    """Judged on dollars alone, but still judged."""
    post(date(2026, 3, 5), 'Newsstand', -12.00, 'Media')
    assert periods.compare(MAR)['categories'] == []


# ── Ordering and headline ───────────────────────────────────────────────────

def test_findings_rank_by_dollars_not_percentage(app, post):
    """A $600 swing outranks a tripling worth $150."""
    post(date(2026, 2, 5), 'Landlord', -2000.00, 'Rent')
    post(date(2026, 3, 5), 'Landlord', -2600.00, 'Rent')
    post(date(2026, 2, 7), 'Olive Garden', -75.00, 'Dining')
    post(date(2026, 3, 7), 'Olive Garden', -225.00, 'Dining')

    findings = periods.compare(MAR)['categories']
    assert [f['category'] for f in findings] == ['Rent', 'Dining']
    assert findings[1]['pct'] == 200.0     # the bigger percentage, ranked second


def test_headline_names_the_driver_and_how_much_it_explains(app, post):
    post(date(2026, 2, 11), 'Delta Air Lines', 0.00, 'Travel')
    post(date(2026, 3, 11), 'Delta Air Lines', -400.00, 'Travel')

    headline = periods.compare(MAR)['headline']
    assert headline['category'] == 'Travel'
    assert headline['share_of_total_change'] == 100.0


def test_a_flat_month_has_no_headline(app, post):
    """Not the largest rounding error promoted to a finding."""
    post(date(2026, 2, 5), 'Whole Foods', -300.00, 'Groceries')
    post(date(2026, 3, 5), 'Whole Foods', -305.00, 'Groceries')

    result = periods.compare(MAR)
    assert result['headline'] is None
    assert result['categories'] == []


# ── Totals ──────────────────────────────────────────────────────────────────

def test_totals_are_always_returned_even_when_flat(app, post):
    """"Income was flat" is a useful sentence and needs the numbers to say it."""
    post(date(2026, 2, 28), 'Paycheck', 3000.00, 'Income')
    post(date(2026, 3, 31), 'Paycheck', 3000.00, 'Income')

    totals = periods.compare(MAR)['totals']
    assert totals['income']['delta'] == 0.0
    assert totals['income']['material'] is False


def test_savings_rate_change_is_in_points_not_percent(app, post):
    """10% to 20% is +10 points. Calling it "up 100%" is reliably misread."""
    post(date(2026, 2, 28), 'Paycheck', 1000.00, 'Income')
    post(date(2026, 2, 5), 'Whole Foods', -900.00, 'Groceries')
    post(date(2026, 3, 31), 'Paycheck', 1000.00, 'Income')
    post(date(2026, 3, 5), 'Whole Foods', -800.00, 'Groceries')

    rate = periods.compare(MAR)['totals']['savings_rate']
    assert rate['previous'] == 10.0
    assert rate['current'] == 20.0
    assert rate['delta_points'] == 10.0
    assert rate['pct'] is None


def test_savings_rate_delta_is_none_when_a_period_had_no_income(app, post):
    post(date(2026, 3, 31), 'Paycheck', 1000.00, 'Income')
    post(date(2026, 3, 5), 'Whole Foods', -800.00, 'Groceries')

    rate = periods.compare(MAR)['totals']['savings_rate']
    assert rate['previous'] is None
    assert rate['delta_points'] is None


# ── Window selection ────────────────────────────────────────────────────────

def test_default_comparison_is_the_preceding_period(app, post):
    post(date(2026, 2, 5), 'Whole Foods', -100.00, 'Groceries')
    result = periods.compare(MAR)
    assert result['previous']['window']['label'] == 'February 2026'


def test_compare_kind_resolves_a_named_period(app, post):
    post(date(2026, 5, 5), 'Whole Foods', -100.00, 'Groceries')
    result = periods.compare_kind('quarter', date(2026, 5, 20))
    assert result['current']['window']['label'] == 'Q2 2026'
    assert result['previous']['window']['label'] == 'Q1 2026'


def test_compare_ranges_orders_forwards_regardless_of_argument_order(app, post):
    """The older range is the baseline even when it is passed second."""
    post(date(2026, 2, 5), 'Whole Foods', -100.00, 'Groceries')
    post(date(2026, 3, 5), 'Whole Foods', -400.00, 'Groceries')

    backwards = periods.compare_ranges(
        date(2026, 3, 1), date(2026, 3, 31),   # newer, passed first
        date(2026, 2, 1), date(2026, 2, 28))
    assert backwards['current']['spending'] == 400.00
    assert backwards['previous']['spending'] == 100.00
    assert backwards['categories'][0]['direction'] == 'increase'


def test_transfers_do_not_appear_as_a_change(app, post):
    """Moving money to savings is not a spending increase."""
    post(date(2026, 3, 28), 'To savings', -900.00, 'Transfer')
    assert periods.compare(MAR)['categories'] == []
