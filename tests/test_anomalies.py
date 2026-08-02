"""`dough/services/anomalies.py` — unusual activity, and the reason for it.

Each detector gets a ledger shaped to trigger exactly it, plus at least one
test that it stays quiet when it should. The quiet cases carry most of the
weight: a detector that fires on ordinary spending is worse than no detector,
because a user learns to dismiss the whole surface rather than the one rule.

Two properties are asserted repeatedly and deliberately:

- **A finding carries the figures behind it.** Not just "unusual" but the
  typical amount, the sample size, the multiple. That is what a formatter needs
  to write a sentence the user can check, and what stops the model inventing the
  supporting number itself.
- **The statistics survive a fat tail.** `test_one_annual_payment_does_not_hide_
  every_later_outlier` is the whole argument for the median absolute deviation
  in one test, and it fails against a standard deviation.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from dough.services import anomalies

TODAY = date(2026, 8, 15)


@pytest.fixture()
def post():
    from models import Transaction, db

    def _post(when, description, amount, category='Shopping',
              account='checking'):
        row = Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category)
        db.session.add(row)
        db.session.commit()
        return row
    return _post


def _kinds(found):
    return sorted({f['kind'] for f in found})


def _of_kind(found, kind):
    return [f for f in found if f['kind'] == kind]


# ── Large purchases ─────────────────────────────────────────────────────────

def test_a_charge_far_above_the_category_norm_is_flagged(app, post):
    for day in range(1, 13):
        post(date(2026, 8, 1) + timedelta(days=-day), 'Corner Store', -20.00,
             'Groceries')
    post(date(2026, 8, 10), 'Whole Foods', -640.00, 'Groceries')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase')
    assert len(found) == 1
    assert found[0]['description'] == 'Whole Foods'
    assert found[0]['amount'] == 640.00
    assert found[0]['detail']['typical'] == 20.00
    assert found[0]['detail']['times_typical'] == 32.0
    assert found[0]['detail']['sample_size'] == 13


def test_a_large_charge_is_judged_against_its_own_category(app, post):
    """$400 of groceries is odd. $400 of airfare is Tuesday.

    A single global threshold cannot tell these apart, which is why the
    baseline is per category.
    """
    for day in range(1, 13):
        post(date(2026, 8, 1) - timedelta(days=day), 'Corner Store', -20.00,
             'Groceries')
    for day in range(1, 13):
        post(date(2026, 8, 1) - timedelta(days=day), 'Delta', -420.00, 'Travel')

    post(date(2026, 8, 10), 'Delta', -430.00, 'Travel')
    post(date(2026, 8, 11), 'Whole Foods', -430.00, 'Groceries')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase')
    assert [f['category'] for f in found] == ['Groceries']


def test_too_few_charges_is_not_a_baseline(app, post):
    """"Unusual compared to two other purchases" must never reach a user."""
    post(date(2026, 8, 1), 'Corner Store', -20.00, 'Groceries')
    post(date(2026, 8, 2), 'Corner Store', -22.00, 'Groceries')
    post(date(2026, 8, 3), 'Whole Foods', -900.00, 'Groceries')

    assert _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase') == []


def test_a_statistical_outlier_below_the_dollar_floor_is_ignored(app, post):
    """A $12 sandwich among $4 coffees is an outlier and is not news."""
    for day in range(1, 13):
        post(date(2026, 8, 1) - timedelta(days=day), 'Blue Bottle', -4.00,
             'Dining')
    post(date(2026, 8, 10), 'Blue Bottle', -12.00, 'Dining')

    assert _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase') == []


def test_a_fixed_subscription_never_produces_an_outlier(app, post):
    """Identical charges have no spread; dividing by it would flag them all."""
    for month in range(1, 9):
        post(date(2026, month, 5), 'Netflix', -15.99, 'Streaming')
    assert _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase') == []


def test_one_annual_payment_does_not_hide_every_later_outlier(app, post):
    """The whole argument for the median absolute deviation, in one test.

    A $6,000 insurance payment inflates a standard deviation so far that a
    genuine $800 outlier falls inside one sigma and is never flagged again all
    year. The MAD is unmoved by it.
    """
    for day in range(1, 15):
        post(date(2026, 8, 1) - timedelta(days=day), 'Corner Store', -30.00,
             'Insurance')
    post(date(2026, 2, 1), 'Annual premium', -6000.00, 'Insurance')
    post(date(2026, 8, 12), 'Broker fee', -800.00, 'Insurance')

    flagged = {f['description']
               for f in _of_kind(anomalies.detect(anchor=TODAY), 'large_purchase')}
    assert 'Broker fee' in flagged


# ── Duplicates ──────────────────────────────────────────────────────────────

def test_the_same_charge_twice_in_two_days_is_reported(app, post):
    post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    post(date(2026, 8, 12), 'Acme Hardware', -149.99)

    found = _of_kind(anomalies.detect(anchor=TODAY), 'duplicate')
    assert len(found) == 1
    assert found[0]['detail']['days_apart'] == 2
    assert found[0]['detail']['first_date'] == '2026-08-10'


def test_the_same_charge_weeks_apart_is_not_a_duplicate(app, post):
    post(date(2026, 7, 1), 'Acme Hardware', -149.99)
    post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    assert _of_kind(anomalies.detect(anchor=TODAY), 'duplicate') == []


def test_a_duplicate_is_reported_as_info_not_as_a_certainty(app, post):
    """A double-post and a repeat purchase are identical in the ledger.

    Reported as `info` and worded as an observation, so nobody is sent to their
    bank over a coffee they bought twice.

    The two charges are a day apart because they have to be: `idx_transaction_
    unique` covers (household, account, date, description, amount), so a
    same-day exact repeat cannot exist in this schema at all. That is worth
    knowing — it means the detector's real quarry is a re-post a day or two
    later, not a literal double-charge, which the ledger rejects on import.
    """
    post(date(2026, 8, 10), 'Blue Bottle', -4.50, 'Dining')
    post(date(2026, 8, 11), 'Blue Bottle', -4.50, 'Dining')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'duplicate')
    assert found[0]['severity'] == 'info'


def test_different_amounts_at_one_merchant_are_not_duplicates(app, post):
    post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    post(date(2026, 8, 11), 'Acme Hardware', -150.00)
    assert _of_kind(anomalies.detect(anchor=TODAY), 'duplicate') == []


# ── Category spikes ─────────────────────────────────────────────────────────

def test_a_category_costing_far_more_this_month_is_flagged(app, post):
    for month in range(1, 8):
        post(date(2026, month, 5), 'Corner Store', -200.00, 'Groceries')
    post(date(2026, 8, 5), 'Whole Foods', -900.00, 'Groceries')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'category_spike')
    assert len(found) == 1
    assert found[0]['detail']['this_month'] == 900.00
    assert found[0]['detail']['typical_month'] == 200.00
    assert found[0]['detail']['over_by'] == 700.00


def test_a_consistently_expensive_category_is_not_a_spike(app, post):
    """Rent is large every month and is never news."""
    for month in range(1, 9):
        post(date(2026, month, 1), 'Landlord', -2400.00, 'Rent')
    assert _of_kind(anomalies.detect(anchor=TODAY), 'category_spike') == []


def test_a_spike_needs_months_of_history_behind_it(app, post):
    post(date(2026, 7, 5), 'Corner Store', -200.00, 'Groceries')
    post(date(2026, 8, 5), 'Whole Foods', -900.00, 'Groceries')
    assert _of_kind(anomalies.detect(anchor=TODAY), 'category_spike') == []


# ── Missing income ──────────────────────────────────────────────────────────

def test_a_paycheck_that_stopped_arriving_is_critical(app, post):
    for month in range(1, 7):
        post(date(2026, month, 28), 'Payroll ACME', 3000.00, 'Income')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'missing_paycheck')
    assert len(found) == 1
    assert found[0]['severity'] == 'critical'
    assert found[0]['detail']['typical_amount'] == 3000.00
    assert found[0]['detail']['usual_gap_days'] in (30, 31)


def test_a_paycheck_arriving_a_few_days_late_is_not_missing(app, post):
    """Payroll shifting off a weekend must not fire an alert every month."""
    for month in range(1, 8):
        post(date(2026, month, 28), 'Payroll ACME', 3000.00, 'Income')
    post(date(2026, 8, 3), 'Payroll ACME', 3000.00, 'Income')

    assert _of_kind(anomalies.detect(anchor=TODAY), 'missing_paycheck') == []


def test_a_one_off_inflow_is_not_a_missing_paycheck(app, post):
    """Seen twice is a coincidence, not a schedule."""
    post(date(2026, 1, 10), 'Bonus', 2000.00, 'Income')
    post(date(2026, 2, 10), 'Bonus', 2000.00, 'Income')
    assert _of_kind(anomalies.detect(anchor=TODAY), 'missing_paycheck') == []


# ── Bills and subscriptions ─────────────────────────────────────────────────

def test_a_subscription_reprice_is_reported_with_its_annual_cost(app, post):
    for month in range(1, 8):
        post(date(2026, month, 5), 'Netflix', -15.99, 'Streaming')
    post(date(2026, 8, 5), 'Netflix', -22.99, 'Streaming')

    found = _of_kind(anomalies.detect(anchor=TODAY), 'subscription_hike')
    assert len(found) == 1
    assert found[0]['detail']['was'] == 15.99
    assert found[0]['detail']['now'] == 22.99
    assert found[0]['detail']['annual_impact'] == 84.00      # 7.00 x 12


def test_an_irregular_bill_going_up_is_not_called_a_subscription(app, post):
    """Cadence is the only thing separating the two, so it is what decides.

    Gaps of 17/41/5/75 days have a median of 24 — outside the 25–35 day band
    that reads as a monthly subscription.
    """
    for day in (2, 19, 60, 65, 140):
        post(date(2026, 3, 1) + timedelta(days=day), 'City Utilities', -80.00,
             'Utilities')
    post(date(2026, 8, 12), 'City Utilities', -140.00, 'Utilities')

    found = anomalies.detect(anchor=TODAY)
    assert _of_kind(found, 'bill_increase')
    assert _of_kind(found, 'subscription_hike') == []


def test_a_monthly_restaurant_visit_is_not_a_subscription(app, post):
    """Cadence alone cannot tell the two apart; a fixed price can.

    Somebody eating at the same restaurant once a month and spending more each
    time matches a subscription's cadence exactly. The rising bill is a real
    finding and deserves different words, so it is reported as `bill_increase`.
    """
    for month, amount in enumerate([100, 160, 220, 280, 340], start=3):
        post(date(2026, month, 8), 'Olive Garden', -amount, 'Dining')
    post(date(2026, 8, 8), 'Olive Garden', -400.00, 'Dining')

    found = anomalies.detect(anchor=TODAY)
    assert _of_kind(found, 'bill_increase')
    assert _of_kind(found, 'subscription_hike') == []


def test_a_penny_rise_is_not_a_price_increase(app, post):
    for month in range(1, 8):
        post(date(2026, month, 5), 'Netflix', -15.99, 'Streaming')
    post(date(2026, 8, 5), 'Netflix', -16.09, 'Streaming')

    assert _of_kind(anomalies.detect(anchor=TODAY), 'subscription_hike') == []


def test_a_stable_bill_is_never_flagged(app, post):
    for month in range(1, 9):
        post(date(2026, month, 5), 'Netflix', -15.99, 'Streaming')
    assert anomalies.detect(anchor=TODAY) == []


# ── Shape, ranking and quiet ledgers ────────────────────────────────────────

def test_an_ordinary_ledger_produces_nothing(app, post):
    """The property that matters most: no noise on normal spending."""
    for month in range(1, 9):
        post(date(2026, month, 1), 'Landlord', -2000.00, 'Rent')
        post(date(2026, month, 3), 'Corner Store', -180.00, 'Groceries')
        post(date(2026, month, 28), 'Payroll ACME', 4000.00, 'Income')

    assert anomalies.detect(anchor=TODAY) == []


def test_an_empty_ledger_produces_nothing(app):
    assert anomalies.detect(anchor=TODAY) == []
    assert anomalies.summary(anchor=TODAY)['total'] == 0


def test_findings_rank_critical_first(app, post):
    for month in range(1, 7):
        post(date(2026, month, 28), 'Payroll ACME', 3000.00, 'Income')
    post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    post(date(2026, 8, 11), 'Acme Hardware', -149.99)

    found = anomalies.detect(anchor=TODAY)
    assert found[0]['kind'] == 'missing_paycheck'
    assert found[0]['severity'] == 'critical'


def test_every_finding_carries_a_checkable_shape(app, post):
    for month in range(1, 8):
        post(date(2026, month, 5), 'Netflix', -15.99, 'Streaming')
    post(date(2026, 8, 5), 'Netflix', -22.99, 'Streaming')

    for finding in anomalies.detect(anchor=TODAY):
        assert set(finding) >= {'kind', 'severity', 'summary', 'amount', 'date',
                                'transaction_id', 'description', 'category',
                                'detail'}
        assert finding['severity'] in anomalies.SEVERITIES
        assert finding['detail']


def test_transfers_are_never_anomalies(app, post):
    """A large move to savings is not a large purchase."""
    for day in range(1, 13):
        post(date(2026, 8, 1) - timedelta(days=day), 'To savings', -50.00,
             'Transfer')
    post(date(2026, 8, 10), 'To savings', -9000.00, 'Transfer')

    assert anomalies.detect(anchor=TODAY) == []


def test_detection_writes_nothing(app, post):
    """Detection is a read. A briefing must not mutate the row it describes."""
    from models import Transaction

    row = post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    post(date(2026, 8, 11), 'Acme Hardware', -149.99)
    row.anomaly_reviewed = False
    before = (row.anomaly_score, row.anomaly_reviewed)

    anomalies.detect(anchor=TODAY)

    fresh = Transaction.query.get(row.id)
    assert (fresh.anomaly_score, fresh.anomaly_reviewed) == before


def test_summary_counts_by_kind_and_severity(app, post):
    for month in range(1, 7):
        post(date(2026, month, 28), 'Payroll ACME', 3000.00, 'Income')
    post(date(2026, 8, 10), 'Acme Hardware', -149.99)
    post(date(2026, 8, 11), 'Acme Hardware', -149.99)

    found = anomalies.summary(anchor=TODAY)
    assert found['total'] == 2
    assert found['by_kind'] == {'missing_paycheck': 1, 'duplicate': 1}
    assert found['by_severity'] == {'critical': 1, 'info': 1}
