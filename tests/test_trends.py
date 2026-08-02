"""`dough/services/trends.py` — direction of travel, and when to refuse to name one.

Most of these tests assert a *negative*. That is the point of the module: a
slope is trivial to compute and the entire difficulty is knowing when it means
nothing. Three data points, a scattered series, a $9 category, one expensive
month — each produces a confident-looking number that must not become "your
restaurant spending is rising".

Dates are anchored on a fixed `TODAY` and the ledger is written backwards from
it, so the suite does not start failing in a month whose length differs.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import trends
from dough.services.analytics import lookback_window

#: Every fixture below posts relative to this, and every window is resolved
#: against it. A test whose meaning depends on the real clock is a test that
#: fails on the first of the month.
TODAY = date(2026, 8, 15)


@pytest.fixture()
def post():
    from models import Transaction, db

    def _post(months_ago, amount, category, description=None, day=10,
              account='checking'):
        """One charge, `months_ago` whole months before TODAY."""
        year, month = TODAY.year, TODAY.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        db.session.add(Transaction(
            account_name=account, date=date(year, month, day),
            description=description or category,
            amount=Decimal(str(-abs(amount))), category=category))
        db.session.commit()
    return _post


def _trend(category, months=6):
    """The trend for one category over the anchored window, or None.

    Anchored through the public `anchor` argument rather than by patching the
    clock. An earlier version of this file monkeypatched `lookback_window`,
    which worked and tested a seam no caller uses; `category_trends` takes an
    anchor for the same reason `anomalies.detect` and `health.score` do, so the
    tests exercise the real signature.
    """
    found = [t for t in trends.category_trends(months, anchor=TODAY)
             if t['category'] == category]
    return found[0] if found else None


# ── Refusing to call it a trend ─────────────────────────────────────────────

def test_two_months_is_not_a_trend(app, post):
    """None, not 'flat'. "Not enough history" and "no movement" differ."""
    post(1, 400, 'Dining')
    post(0, 800, 'Dining')
    assert _trend('Dining') is None


def test_a_tiny_category_is_not_worth_trending(app, post):
    """$9 drifting to $14 must not outrank everything by percentage."""
    for months_ago, amount in enumerate([14, 12, 11, 10, 9, 9]):
        post(months_ago, amount, 'Newsstand')
    assert _trend('Newsstand') is None


def test_a_scattered_series_is_volatile_not_rising(app, post):
    """A real slope through noise has a direction and no meaning."""
    for months_ago, amount in enumerate([100, 900, 150, 800, 200, 850]):
        post(months_ago, amount, 'Shopping')
    found = _trend('Shopping')
    assert found['direction'] == 'volatile'
    assert found['r_squared'] < trends.WEAK_FIT_R2
    assert found['confidence'] == 'low'


def test_one_expensive_month_does_not_make_a_rising_trend(app, post):
    """The birthday-dinner case, stated directly."""
    for months_ago in range(6):
        post(months_ago, 300, 'Dining')
    post(0, 900, 'Dining', description='Birthday dinner', day=12)

    found = _trend('Dining')
    assert found['direction'] in ('flat', 'volatile')


def test_drift_below_the_threshold_reads_as_flat(app, post):
    for months_ago, amount in enumerate([300, 301, 302, 303, 304, 305]):
        post(months_ago, amount, 'Groceries')
    assert _trend('Groceries')['direction'] == 'flat'


# ── Naming a trend when there is one ────────────────────────────────────────

def test_a_clean_rise_is_reported_with_its_slope(app, post):
    """400 → 900 over six months, +100 a month."""
    for months_ago, amount in enumerate([900, 800, 700, 600, 500, 400]):
        post(months_ago, amount, 'Dining')

    found = _trend('Dining')
    assert found['direction'] == 'rising'
    assert found['slope_per_month'] == 100.0
    assert found['r_squared'] == 1.0
    assert found['confidence'] == 'high'
    assert found['first'] == 400.0 and found['last'] == 900.0


def test_a_clean_fall_is_reported_as_falling(app, post):
    for months_ago, amount in enumerate([400, 500, 600, 700, 800, 900]):
        post(months_ago, amount, 'Dining')
    assert _trend('Dining')['direction'] == 'falling'


def test_trends_rank_by_dollars_moved_per_month(app, post):
    for months_ago, amount in enumerate([1200, 1000, 800, 600, 400, 200]):
        post(months_ago, amount, 'Rent')
    for months_ago, amount in enumerate([160, 150, 140, 130, 120, 110]):
        post(months_ago, amount, 'Dining')

    ranked = [t['category'] for t in trends.category_trends(6, anchor=TODAY)]
    assert ranked[0] == 'Rent'


def test_the_series_rides_along_for_the_model_to_quote(app, post):
    """A trend the model can cite is a trend it cannot round wrongly."""
    for months_ago, amount in enumerate([600, 500, 400]):
        post(months_ago, amount, 'Dining')
    series = _trend('Dining', months=3)['series']
    assert list(series.values()) == [400.0, 500.0, 600.0]
    assert len(series) == 3


def test_a_missing_month_counts_as_zero_not_as_absent(app, post):
    """Fitting only the months somebody spent always trends flat.

    Dining stops entirely for the last three months; that is a fall, and it is
    invisible if the empty months are dropped from the fit.
    """
    for months_ago in (5, 4, 3):
        post(months_ago, 600, 'Dining')

    found = _trend('Dining')
    assert found['direction'] == 'falling'
    assert found['last'] == 0.0


def test_the_lookback_window_is_configurable(app, post):
    """A rise confined to the last three months is invisible over twelve."""
    for months_ago, amount in enumerate([900, 600, 300]):
        post(months_ago, amount, 'Dining')
    for months_ago in range(3, 12):
        post(months_ago, 300, 'Dining')

    assert _trend('Dining', months=3)['direction'] == 'rising'
    assert _trend('Dining', months=12)['direction'] in ('flat', 'volatile')


# ── Merchants ───────────────────────────────────────────────────────────────

def test_a_merchant_seen_twice_has_no_trend(app, post):
    post(1, 50, 'Streaming', description='Netflix')
    post(0, 90, 'Streaming', description='Netflix')
    assert [t for t in trends.merchant_trends(6, anchor=TODAY)
            if t['description'] == 'Netflix'] == []


def test_a_merchant_creeping_up_is_reported(app, post):
    for months_ago, amount in enumerate([19, 18, 17, 16, 15, 14]):
        post(months_ago, amount, 'Streaming', description='Netflix')

    netflix = next(t for t in trends.merchant_trends(6, anchor=TODAY)
                   if t['description'] == 'Netflix')
    assert netflix['direction'] == 'rising'
    assert netflix['months_seen'] == 6
    assert netflix['slope_per_month'] == 1.0


# ── Unit cost: the "grocery inflation" question ─────────────────────────────

def test_rising_basket_cost_with_flat_visits_reads_as_prices_rising(app, post):
    """Same number of shops, each one dearer."""
    for months_ago, amount in enumerate([150, 130, 110, 90]):
        for visit in range(2):
            post(months_ago, amount, 'Groceries',
                 description='Whole Foods', day=5 + visit * 10)

    reading = trends.unit_cost_trend('Groceries', months=4, anchor=TODAY)
    assert reading['reading'] == 'prices_rising'
    assert reading['average_purchase']['direction'] == 'rising'
    assert reading['purchase_count']['direction'] == 'flat'


def test_more_visits_at_a_steady_price_reads_as_buying_more_often(app, post):
    """The same basket, bought more times. Different problem, different advice."""
    for months_ago, visits in enumerate([5, 4, 3, 2]):
        for visit in range(visits):
            post(months_ago, 100, 'Groceries',
                 description='Whole Foods', day=3 + visit * 5)

    reading = trends.unit_cost_trend('Groceries', months=4, anchor=TODAY)
    assert reading['reading'] == 'buying_more_often'
    assert reading['average_purchase']['direction'] == 'flat'


def test_unit_cost_needs_enough_months(app, post):
    post(0, 100, 'Groceries', description='Whole Foods')
    assert trends.unit_cost_trend('Groceries', months=6, anchor=TODAY) is None


def test_unit_cost_ignores_months_the_category_did_not_occur(app, post):
    """A zero-spend month has no basket size; feeding it in as 0 fakes a fall."""
    for months_ago in (3, 2, 1):
        post(months_ago, 100, 'Groceries', description='Whole Foods')

    reading = trends.unit_cost_trend('Groceries', months=6, anchor=TODAY)
    assert reading['months'] == ['2026-05', '2026-06', '2026-07']
    assert reading['average_purchase']['direction'] == 'flat'


# ── Window arithmetic ───────────────────────────────────────────────────────

def test_lookback_window_covers_exactly_the_months_asked_for(app):
    window = lookback_window(6, TODAY)
    assert window.start == date(2026, 3, 1)
    assert window.end == TODAY


def test_lookback_of_less_than_a_month_raises(app):
    with pytest.raises(ValueError):
        lookback_window(0)
