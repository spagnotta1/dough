"""Tests for the dashboard's reasoning layer.

These cover the judgement calls rather than the arithmetic: that a thin
runway outranks a budget warning, that a stale bill rolls forward to its next
real occurrence, that the forecast's uncertainty widens with the horizon, and
that the greeting picks the most specific signal available.
"""

from datetime import date, datetime, timedelta

import dashboard_intel as di


# ── Health score ────────────────────────────────────────────────────────────

def test_health_score_rewards_saving_and_punishes_overspend():
    saver = di.health_score(income=10000, outgo=7000, runway_months=8.0,
                            budget_map={}, category_stats={}, period_months=1.0,
                            prev_outgo=7500)
    spender = di.health_score(income=10000, outgo=12000, runway_months=0.5,
                              budget_map={}, category_stats={}, period_months=1.0,
                              prev_outgo=8000)
    assert saver['score'] > spender['score']
    assert saver['band'] == 'strong'
    assert spender['band'] == 'strained'


def test_health_score_drops_budget_factor_when_none_are_set():
    """Someone who has never used budgets should not be marked down for it."""
    result = di.health_score(income=10000, outgo=8000, runway_months=6.0,
                             budget_map={}, category_stats={}, period_months=1.0,
                             prev_outgo=8000)
    budgets = next(f for f in result['factors'] if f['key'] == 'budgets')
    assert budgets['weight'] == 0
    assert budgets['status'] == 'unset'
    # The remaining factors still have to add up to a usable score.
    assert 0 <= result['score'] <= 100


def test_health_score_counts_budgets_that_are_kept():
    result = di.health_score(
        income=10000, outgo=5000, runway_months=6.0,
        budget_map={'Food': 500, 'Gas': 200},
        category_stats={'Food': {'inbound': 0, 'outbound': 400},
                        'Gas': {'inbound': 0, 'outbound': 900}},
        period_months=1.0, prev_outgo=5000)
    budgets = next(f for f in result['factors'] if f['key'] == 'budgets')
    assert budgets['score'] == 50          # one of two on track
    assert '1 of 2' in budgets['detail']


def test_health_score_survives_a_period_with_no_income():
    result = di.health_score(income=0, outgo=0, runway_months=None,
                             budget_map={}, category_stats={}, period_months=1.0,
                             prev_outgo=0)
    assert 0 <= result['score'] <= 100


# ── Upcoming bills ──────────────────────────────────────────────────────────

def _bill(next_expected, gap=30, amount=-100.0, desc='RENT'):
    return {'description': desc, 'category': 'Rent', 'monthly_amount': amount,
            'median_gap_days': gap, 'next_expected': next_expected,
            'desc_keys': [desc.lower()]}


def test_upcoming_bills_rolls_a_stale_expectation_forward():
    """A bill that was due months ago must reappear on its next real date.

    May 1 on a 30-day cadence steps 05-31 → 06-30 → 07-30, which is the first
    occurrence at or after the 20th.
    """
    rec = {'bills': [_bill('2026-05-01')], 'subscriptions': []}
    out = di.upcoming_bills(rec, date(2026, 7, 20), horizon_days=14)
    assert len(out) == 1
    assert out[0]['due_date'] == '2026-07-30'
    assert out[0]['days_away'] == 10


def test_upcoming_bills_emits_every_occurrence_in_the_window():
    """A weekly charge falls due more than once inside a fortnight.

    The fourth occurrence (Aug 4) sits one day past the Aug 3 horizon and is
    correctly left out.
    """
    rec = {'bills': [_bill('2026-07-21', gap=7, desc='GYM')], 'subscriptions': []}
    out = di.upcoming_bills(rec, date(2026, 7, 20), horizon_days=14)
    assert [b['due_date'] for b in out] == ['2026-07-21', '2026-07-28']


def test_upcoming_bills_excludes_anything_past_the_horizon():
    rec = {'bills': [_bill('2026-09-01')], 'subscriptions': []}
    assert di.upcoming_bills(rec, date(2026, 7, 20), horizon_days=14) == []


def test_upcoming_bills_reports_amounts_as_positive():
    rec = {'bills': [_bill('2026-07-25', amount=-1800.0)], 'subscriptions': []}
    out = di.upcoming_bills(rec, date(2026, 7, 20))
    assert out[0]['amount'] == 1800.0


def test_upcoming_bills_tolerates_a_missing_date():
    rec = {'bills': [{'description': 'X', 'monthly_amount': -10}], 'subscriptions': []}
    assert di.upcoming_bills(rec, date(2026, 7, 20)) == []


# ── Attention items ─────────────────────────────────────────────────────────

def _attention(**kwargs):
    base = dict(budget_alerts=[], category_stats={}, prev_category_stats={},
                income=5000, outgo=3000, prev_income=5000, prev_outgo=3000,
                cash=20000, period_months=1.0, runway_months=6.0,
                bills=[], hikes=[], anomaly_count=0, portfolio=None)
    base.update(kwargs)
    return di.attention_items(**base)


def test_attention_ranks_critical_above_everything_else():
    items = _attention(
        runway_months=0.8, cash=1500, outgo=2000,
        budget_alerts=[{'category': 'Food', 'pct': 90, 'level': 'warning',
                        'limit': 500, 'monthly_avg': 450, 'spent': 450}],
        anomaly_count=4)
    assert items[0]['severity'] == 'critical'
    assert items[0]['key'] == 'runway'
    severities = [i['severity'] for i in items]
    assert severities == sorted(severities, key=lambda s: di._SEVERITY_RANK[s])


def test_attention_flags_spending_above_income():
    items = _attention(income=3000, outgo=5000)
    assert any(i['key'] == 'cashflow' for i in items)


def test_attention_ignores_a_large_percentage_on_a_trivial_amount():
    """A category going from $2 to $8 is 300% and worth nobody's attention."""
    items = _attention(category_stats={'Coffee': {'inbound': 0, 'outbound': 8}},
                       prev_category_stats={'Coffee': {'inbound': 0, 'outbound': 2}})
    assert not any(i['key'] == 'unusual-spend' for i in items)


def test_attention_flags_a_material_category_swing():
    items = _attention(category_stats={'Dining': {'inbound': 0, 'outbound': 900}},
                       prev_category_stats={'Dining': {'inbound': 0, 'outbound': 300}})
    swing = next(i for i in items if i['key'] == 'unusual-spend')
    assert 'Dining' in swing['title']
    assert swing['severity'] == 'warning'


def test_attention_says_so_when_nothing_is_wrong():
    items = _attention()
    assert all(i['severity'] == 'positive' for i in items)


def test_attention_skips_portfolio_movement_without_a_cost_basis():
    """A manually entered value with no basis cannot report a gain."""
    items = _attention(portfolio={'value': 50000, 'cost_basis': None,
                                  'gain': None, 'gain_pct': None})
    assert not any(i['key'] == 'portfolio' for i in items)


def test_attention_reports_portfolio_movement_when_basis_is_known():
    items = _attention(portfolio={'value': 50000, 'cost_basis': 40000,
                                  'gain': 10000, 'gain_pct': 25.0})
    item = next(i for i in items if i['key'] == 'portfolio')
    assert item['severity'] == 'positive'


def test_attention_warns_when_due_bills_exceed_available_cash():
    bills = [{'description': 'RENT', 'category': 'Rent', 'amount': 2000,
              'due_date': '2026-07-28', 'days_away': 3, 'kind': 'bill'}]
    items = _attention(cash=500, bills=bills)
    bill_item = next(i for i in items if i['key'] == 'bills')
    assert bill_item['severity'] == 'warning'


# ── Forecast ────────────────────────────────────────────────────────────────

def _daily_txns(start, days, amount=-50.0):
    return [{'date': start + timedelta(days=i), 'amount': amount, 'description': 'x'}
            for i in range(days)]


def test_forecast_band_widens_with_the_horizon():
    """Uncertainty compounds as a random walk, so later days are less certain."""
    txns = [{'date': date(2026, 6, 1) + timedelta(days=i),
             'amount': -50.0 if i % 3 else 400.0, 'description': 'x'}
            for i in range(60)]
    f = di.cash_flow_forecast(transactions=txns, cash=10000, bills=[],
                              today=date(2026, 7, 31), days=30)
    spreads = [p['high'] - p['low'] for p in f['points']]
    assert spreads[0] == 0
    assert spreads[-1] > spreads[10] > spreads[1]


def test_forecast_subtracts_scheduled_bills_on_their_due_dates():
    txns = _daily_txns(date(2026, 6, 1), 30, amount=-10.0)
    bills = [{'description': 'RENT', 'category': 'Rent', 'amount': 2000,
              'due_date': '2026-07-05', 'days_away': 4, 'kind': 'bill'}]
    with_bill = di.cash_flow_forecast(transactions=txns, cash=10000, bills=bills,
                                      today=date(2026, 7, 1), days=20)
    without = di.cash_flow_forecast(transactions=txns, cash=10000, bills=[],
                                    today=date(2026, 7, 1), days=20)
    assert with_bill['projected_balance'] < without['projected_balance']
    assert with_bill['bills_total'] == 2000


def test_forecast_detects_a_balance_going_negative():
    txns = _daily_txns(date(2026, 6, 1), 30, amount=-200.0)
    f = di.cash_flow_forecast(transactions=txns, cash=500, bills=[],
                              today=date(2026, 7, 1), days=30)
    assert f['goes_negative']
    assert f['low_point']['balance'] < 0


def test_forecast_handles_an_account_with_no_history():
    f = di.cash_flow_forecast(transactions=[], cash=1000, bills=[],
                              today=date(2026, 7, 1), days=10)
    assert f['projected_balance'] == 1000
    assert f['confident'] is False
    assert len(f['points']) == 11


def test_forecast_starts_at_todays_cash():
    txns = _daily_txns(date(2026, 6, 1), 30)
    f = di.cash_flow_forecast(transactions=txns, cash=7500, bills=[],
                              today=date(2026, 7, 1), days=15)
    assert f['points'][0]['balance'] == 7500
    assert f['points'][0]['date'] == '2026-07-01'


# ── Personalization ─────────────────────────────────────────────────────────

def test_greeting_changes_with_the_hour():
    def salutation(hour):
        return di.personalize(now=datetime(2026, 7, 15, hour), name=None,
                              transactions=[], income=0, outgo=0, prev_outgo=0,
                              net_worth=0)['salutation']
    assert salutation(9) == 'Good morning'
    assert salutation(14) == 'Good afternoon'
    assert salutation(20) == 'Good evening'
    assert salutation(3) == 'Working late'


def test_greeting_prefers_payday_over_a_spending_trend():
    """Payday is the more specific signal, so it wins the single slot."""
    txns = [{'date': date(2026, 7, 14), 'amount': 3200.0, 'description': 'PAYROLL'}]
    result = di.personalize(now=datetime(2026, 7, 15, 10), name='Sal Kim',
                            transactions=txns, income=3200, outgo=1000,
                            prev_outgo=5000, net_worth=50000)
    assert result['tag']['kind'] == 'payday'
    assert result['name'] == 'Sal'


def test_greeting_falls_back_to_the_health_read():
    result = di.personalize(now=datetime(2026, 7, 15, 10), name=None,
                            transactions=[], income=100, outgo=100, prev_outgo=100,
                            net_worth=0, health={'band': 'steady', 'label': 'Steady'})
    assert result['tag']['kind'] == 'health'
    # The phrase has to complete the sentence grammatically.
    assert result['tag']['text'] == 'Your finances are holding steady.'


def test_greeting_tolerates_no_signal_at_all():
    result = di.personalize(now=datetime(2026, 7, 15, 10), name=None,
                            transactions=[], income=0, outgo=0, prev_outgo=0,
                            net_worth=0)
    assert result['tag'] is None
    assert result['date_label'] == 'Wednesday, July 15'


# ── Subscription price rises ────────────────────────────────────────────────

def test_subscription_hike_detected_against_the_payees_own_history():
    rec = {'subscriptions': [{'description': 'STREAMING CO',
                              'desc_keys': ['streaming co'],
                              'monthly_amount': -21.99}]}
    txns = [{'date': date(2026, m, 5), 'amount': -9.99, 'description': 'STREAMING CO'}
            for m in range(1, 6)]
    txns.append({'date': date(2026, 6, 5), 'amount': -21.99, 'description': 'STREAMING CO'})
    hikes = di.subscription_hikes(rec, txns)
    assert len(hikes) == 1
    assert hikes[0]['was'] == 9.99 and hikes[0]['now'] == 21.99
    assert hikes[0]['annual_impact'] == 144


def test_subscription_at_a_steady_price_is_not_a_hike():
    rec = {'subscriptions': [{'description': 'STREAMING CO',
                              'desc_keys': ['streaming co'],
                              'monthly_amount': -9.99}]}
    txns = [{'date': date(2026, m, 5), 'amount': -9.99, 'description': 'STREAMING CO'}
            for m in range(1, 7)]
    assert di.subscription_hikes(rec, txns) == []
