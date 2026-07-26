"""Unit tests for the portfolio intelligence layer.

Pure functions over plain dicts — no app, no database, no fixtures beyond the
small builders below.
"""

from datetime import date, timedelta

import pytest

import investments_intel as ii


# ── Builders ────────────────────────────────────────────────────────────────

def holding(ticker, value, *, name=None, asset_class='Stock', account='Brokerage',
            avg_cost=None, shares=1.0, cost_basis=None, gain=None, gain_pct=None,
            account_id=None, source='sync'):
    return {
        'id': abs(hash(ticker)) % 10_000,
        'ticker': ticker,
        'name': name or f'{ticker} Inc.',
        'shares': shares,
        'current_value': value,
        'asset_class': asset_class,
        'account_name': account,
        'account_id': account_id,
        'source': source,
        'avg_cost': avg_cost,
        'current_price': None,
        'cost_basis': cost_basis,
        'gain_loss': gain,
        'gain_pct': gain_pct,
        'last_synced_at': None,
    }


def snapshots(values, end=date(2026, 7, 25), field='total_investments'):
    """One row per day ending at ``end``, oldest first."""
    start = end - timedelta(days=len(values) - 1)
    return [{'date': (start + timedelta(days=i)).strftime('%Y-%m-%d'), field: v}
            for i, v in enumerate(values)]


# ══════════════════════════════════════════════════════════════════════════
# Classification
# ══════════════════════════════════════════════════════════════════════════

class TestClassification:
    def test_ticker_lookup_beats_name_matching(self):
        # "T" is AT&T. A name-first classifier would call it something else
        # entirely on the strength of a stray keyword.
        assert ii.classify_sector('T', 'AT&T Inc.', 'Stock') == 'Communication'

    def test_broad_index_fund_is_not_forced_into_a_sector(self):
        assert ii.classify_sector('VTI', 'Vanguard Total Stock Market ETF', 'ETF') \
            == 'Diversified Fund'

    def test_name_hints_classify_unknown_tickers(self):
        assert ii.classify_sector('XXHLT', 'Acme Health Care Fund', 'Mutual Fund') == 'Healthcare'
        assert ii.classify_sector('XXBND', 'Acme Treasury Bond Fund', 'Mutual Fund') == 'Fixed Income'

    def test_unknown_stays_unclassified_rather_than_guessed(self):
        assert ii.classify_sector('ZZZQ', 'Zzzq Holdings', 'Stock') == 'Unclassified'

    def test_crypto_and_cash_get_their_own_buckets(self):
        assert ii.classify_sector('BTC', 'Bitcoin', 'Crypto') == 'Digital Assets'
        assert ii.classify_sector('VMFXX', 'Federal Money Market', 'Cash') == 'Cash & Equivalents'

    def test_region_detects_international_funds(self):
        assert ii.classify_region('VXUS', 'Vanguard Total International', 'ETF') == 'International'
        assert ii.classify_region('VT', 'Vanguard Total World', 'ETF') == 'Global'
        assert ii.classify_region('AAPL', 'Apple Inc.', 'Stock') == 'United States'

    def test_market_cap_bands(self):
        assert ii.classify_market_cap('AAPL', 'Apple', 'Stock') == 'Mega Cap'
        assert ii.classify_market_cap('IWM', 'Russell 2000 Small Cap', 'ETF') == 'Small Cap'
        assert ii.classify_market_cap('BND', 'Total Bond', 'ETF') == 'Non-Equity'

    def test_unknown_stock_yield_errs_low(self):
        # A stock we cannot identify is assumed to pay nothing, so the income
        # estimate under-promises rather than over-promises.
        assert ii.estimate_yield('ZZZQ', 'Stock') == 0.0
        assert ii.estimate_yield('SCHD', 'ETF') == 3.4


# ══════════════════════════════════════════════════════════════════════════
# Positions & allocation
# ══════════════════════════════════════════════════════════════════════════

class TestPositions:
    def test_cash_holdings_are_excluded_by_default(self):
        rows = ii.build_positions([
            holding('AAPL', 1000),
            holding('VMFXX', 500, asset_class='Cash'),
        ])
        assert [r['ticker'] for r in rows] == ['AAPL']

    def test_weights_sum_to_one_hundred(self):
        rows = ii.build_positions([holding('A', 300), holding('B', 100), holding('C', 100)])
        assert sum(r['weight'] for r in rows) == pytest.approx(100.0, abs=0.05)
        assert rows[0]['ticker'] == 'A'   # sorted by value

    def test_empty_portfolio_is_not_a_crash(self):
        assert ii.build_positions([]) == []

    def test_allocation_reports_coverage(self):
        rows = ii.build_positions([
            holding('AAPL', 750),
            holding('ZZZQ', 250, name='Zzzq Holdings'),
        ])
        alloc = ii.allocation(rows, 'sector', 'sector_known')
        assert alloc['coverage'] == pytest.approx(75.0, abs=0.1)
        labels = {b['label'] for b in alloc['buckets']}
        assert 'Unclassified' in labels


# ══════════════════════════════════════════════════════════════════════════
# Concentration
# ══════════════════════════════════════════════════════════════════════════

class TestConcentration:
    def test_effective_positions_sees_through_a_long_tail(self):
        # Nine holdings, but one is 92% of the money. The honest count is
        # nearer to one position than to nine.
        rows = ii.build_positions(
            [holding('BIG', 9200)] + [holding(f'T{i}', 100) for i in range(8)])
        conc = ii.concentration(rows)
        assert conc['positions'] == 9
        assert conc['top1_pct'] == pytest.approx(92.0, abs=0.1)
        assert conc['effective_positions'] < 1.3

    def test_equal_weights_give_effective_count_equal_to_real_count(self):
        rows = ii.build_positions([holding(f'T{i}', 100) for i in range(5)])
        assert ii.concentration(rows)['effective_positions'] == pytest.approx(5.0, abs=0.05)

    def test_empty_is_safe(self):
        conc = ii.concentration([])
        assert conc['largest'] is None and conc['positions'] == 0
        assert conc['single_name_largest'] is None

    def test_a_broad_index_fund_is_not_single_name_risk(self):
        # 80% in VTI is the shape this app should be encouraging, not the
        # failure mode it warns about. The position-level fact stays true;
        # the risk figure looks through to the companies inside.
        rows = ii.build_positions([
            holding('VTI', 8000, asset_class='ETF', name='Vanguard Total Stock Market ETF'),
            holding('AAPL', 2000),
        ])
        conc = ii.concentration(rows)
        assert conc['top1_pct'] == pytest.approx(80.0, abs=0.1)
        assert conc['single_name_top1_pct'] == pytest.approx(20.0, abs=0.1)
        assert conc['single_name_largest']['ticker'] == 'AAPL'
        assert conc['diversified_pct'] == pytest.approx(80.0, abs=0.1)
        # ...and the effective count reflects the look-through, not the rows.
        assert conc['effective_positions'] > 10

    def test_an_all_fund_portfolio_reports_no_single_name(self):
        rows = ii.build_positions([
            holding('VTI', 6000, asset_class='ETF', name='Vanguard Total Stock Market'),
            holding('VXUS', 4000, asset_class='ETF', name='Vanguard Total International'),
        ])
        conc = ii.concentration(rows)
        assert conc['single_name_largest'] is None
        assert conc['single_name_top1_pct'] == 0.0

    def test_a_single_stock_still_registers_at_full_weight(self):
        rows = ii.build_positions([holding('AAPL', 9000), holding('MSFT', 1000)])
        conc = ii.concentration(rows)
        assert conc['single_name_top1_pct'] == pytest.approx(90.0, abs=0.1)
        assert conc['effective_positions'] < 1.3


# ══════════════════════════════════════════════════════════════════════════
# Performance
# ══════════════════════════════════════════════════════════════════════════

class TestPerformance:
    def test_windows_measure_against_the_right_snapshot(self):
        today = date(2026, 7, 25)
        perf = ii.performance(snapshots([100.0] * 39 + [110.0], end=today), today)
        assert perf['current'] == 110.0
        day = perf['windows']['day']
        assert day['available'] and day['change'] == pytest.approx(10.0)
        assert day['change_pct'] == pytest.approx(10.0)
        month = perf['windows']['month']
        assert month['available'] and month['change'] == pytest.approx(10.0)

    def test_windows_without_history_report_unavailable_not_zero(self):
        # Showing 0.0% for a window we cannot measure reads as "flat", which
        # is a different and wrong claim.
        today = date(2026, 7, 25)
        perf = ii.performance(snapshots([100.0, 101.0], end=today), today)
        assert perf['windows']['year']['available'] is False
        assert perf['windows']['day']['available'] is True

    def test_no_history_returns_empty_rather_than_raising(self):
        perf = ii.performance([], date(2026, 7, 25))
        assert perf['points'] == 0 and perf['windows'] == {}

    def test_annualized_return_refuses_short_spans(self):
        today = date(2026, 7, 25)
        assert ii.annualized_return(snapshots([100.0] * 30, end=today)) is None
        grown = snapshots([100.0 + i * 0.1 for i in range(400)], end=today)
        assert ii.annualized_return(grown) is not None

    def test_volatility_reports_when_it_is_not_measured(self):
        today = date(2026, 7, 25)
        value, measured = ii.volatility(snapshots([100.0, 101.0, 99.0], end=today))
        assert value is None and measured is False

    def test_volatility_of_a_flat_series_is_zero(self):
        today = date(2026, 7, 25)
        value, measured = ii.volatility(snapshots([100.0] * 60, end=today))
        assert measured is True and value == pytest.approx(0.0, abs=0.01)

    def test_drawdown_finds_the_deepest_trough(self):
        today = date(2026, 7, 25)
        dd = ii.drawdown(snapshots([100.0, 120.0, 90.0, 110.0], end=today))
        assert dd['max_pct'] == pytest.approx(-25.0, abs=0.01)
        assert dd['peak']['value'] == 120.0
        assert dd['trough']['value'] == 90.0
        assert dd['all_time_high'] == 120.0


# ══════════════════════════════════════════════════════════════════════════
# Risk & diversification
# ══════════════════════════════════════════════════════════════════════════

class TestRisk:
    def test_all_equity_scores_higher_than_all_bonds(self):
        equity = ii.build_positions([holding('AAPL', 10_000)])
        bonds = ii.build_positions([holding('BND', 10_000, asset_class='Bond')])
        assert ii.risk_score(equity)['score'] > ii.risk_score(bonds)['score']

    def test_crypto_weight_is_surfaced(self):
        rows = ii.build_positions([holding('BTC', 3000, asset_class='Crypto'),
                                   holding('AAPL', 7000)])
        assert ii.risk_score(rows)['crypto_pct'] == pytest.approx(30.0, abs=0.1)

    def test_measured_volatility_is_flagged_as_such(self):
        rows = ii.build_positions([holding('AAPL', 1000)])
        assert ii.risk_score(rows)['volatility_measured'] is False
        assert ii.risk_score(rows, measured_vol=12.0)['volatility_measured'] is True

    def test_empty_portfolio_is_defensive_not_a_crash(self):
        assert ii.risk_score([], cash=0)['score'] == 0


class TestDiversification:
    def _score(self, rows):
        return ii.diversification_score(
            rows,
            ii.allocation(rows, 'sector', 'sector_known'),
            ii.allocation(rows, 'region', 'region_known'),
            ii.concentration(rows))

    def test_a_spread_portfolio_beats_a_single_stock(self):
        single = ii.build_positions([holding('AAPL', 10_000)])
        spread = ii.build_positions([
            holding('VTI', 4000, asset_class='ETF', name='Vanguard Total Stock Market'),
            holding('VXUS', 3000, asset_class='ETF', name='Vanguard Total International'),
            holding('BND', 3000, asset_class='Bond', name='Vanguard Total Bond'),
        ])
        assert self._score(spread)['score'] > self._score(single)['score']

    def test_factor_weights_sum_to_one_hundred(self):
        rows = ii.build_positions([holding('AAPL', 1000), holding('MSFT', 1000)])
        assert sum(f['weight'] for f in self._score(rows)['factors']) == 100

    def test_broad_index_funds_are_not_penalised_as_a_sector_bet(self):
        # A 100% total-market position is diversification, even though it is a
        # single "sector" label — scoring it like six tech names would be wrong.
        index = ii.build_positions([
            holding('VTI', 10_000, asset_class='ETF', name='Vanguard Total Stock Market')])
        tech = ii.build_positions([holding(t, 2000) for t in
                                   ('AAPL', 'MSFT', 'NVDA', 'AMD', 'INTC')])
        sector_factor = lambda s: next(f for f in s['factors'] if f['key'] == 'sector')
        assert sector_factor(self._score(index))['score'] > \
            sector_factor(self._score(tech))['score']


# ══════════════════════════════════════════════════════════════════════════
# Health score
# ══════════════════════════════════════════════════════════════════════════

class TestHealthScore:
    def _health(self, rows, cash, net_worth, monthly=None):
        div = ii.diversification_score(
            rows,
            ii.allocation(rows, 'sector', 'sector_known'),
            ii.allocation(rows, 'region', 'region_known'),
            ii.concentration(rows))
        return ii.health_score(positions=rows, cash=cash, net_worth=net_worth,
                               diversification=div, conc=ii.concentration(rows),
                               monthly_expenses=monthly)

    def test_balanced_portfolio_outscores_a_concentrated_one(self):
        good = ii.build_positions([
            holding('VTI', 4000, asset_class='ETF', name='Vanguard Total Stock Market',
                    cost_basis=3000, gain=1000, gain_pct=33.3),
            holding('VXUS', 3000, asset_class='ETF', name='Vanguard Total International',
                    cost_basis=2800, gain=200, gain_pct=7.1),
            holding('BND', 3000, asset_class='Bond', name='Vanguard Total Bond',
                    cost_basis=3100, gain=-100, gain_pct=-3.2),
        ])
        bad = ii.build_positions([holding('AAPL', 10_000, cost_basis=8000, gain=2000)])
        assert self._health(good, 2000, 12_000, 800)['score'] > \
            self._health(bad, 2000, 12_000, 800)['score']

    def test_recommendations_target_the_weakest_factors(self):
        rows = ii.build_positions([holding('AAPL', 10_000)])
        health = self._health(rows, 100, 10_100, 2000)
        keys = {r['factor'] for r in health['recommendations']}
        assert keys and keys <= {'diversification', 'concentration', 'cash',
                                 'liquidity', 'visibility'}
        assert all(r['lift'] > 0 for r in health['recommendations'])

    def test_a_healthy_portfolio_has_nothing_urgent_to_suggest(self):
        rows = ii.build_positions([
            holding(t, 1000, asset_class='ETF',
                    name={'VTI': 'Vanguard Total Stock Market',
                          'VXUS': 'Vanguard Total International',
                          'BND': 'Vanguard Total Bond'}.get(t, t),
                    cost_basis=900, gain=100, gain_pct=11.1)
            for t in ('VTI', 'VXUS', 'BND', 'SCHD', 'VNQ', 'VEA', 'VWO', 'VTEB')
        ])
        health = self._health(rows, 12_000, 20_000, 2000)
        assert health['score'] >= 62
        assert len(health['recommendations']) <= 3

    def test_factor_weights_sum_to_one_hundred(self):
        rows = ii.build_positions([holding('AAPL', 1000)])
        assert sum(f['weight'] for f in self._health(rows, 500, 1500)['factors']) == 100

    def test_months_of_cash_is_none_without_spending_history(self):
        rows = ii.build_positions([holding('AAPL', 1000)])
        assert self._health(rows, 500, 1500, None)['months_of_cash'] is None
        assert self._health(rows, 6000, 7000, 1000)['months_of_cash'] == pytest.approx(6.0)


# ══════════════════════════════════════════════════════════════════════════
# Income, projection, benchmark
# ══════════════════════════════════════════════════════════════════════════

class TestDividends:
    def test_income_and_yield_come_from_the_reference_table(self):
        rows = ii.build_positions([holding('SCHD', 10_000, asset_class='ETF')])
        d = ii.dividend_forecast(rows)
        assert d['annual'] == pytest.approx(340.0, abs=0.5)   # 3.4%
        assert d['monthly'] == pytest.approx(28.33, abs=0.1)
        assert d['portfolio_yield'] == pytest.approx(3.4, abs=0.05)

    def test_coverage_distinguishes_known_yields_from_defaults(self):
        rows = ii.build_positions([
            holding('SCHD', 5000, asset_class='ETF'),
            holding('ZZZQ', 5000, asset_class='ETF', name='Unknown Fund'),
        ])
        assert ii.dividend_forecast(rows)['coverage'] == pytest.approx(50.0, abs=0.1)

    def test_no_payers_is_zero_not_an_error(self):
        rows = ii.build_positions([holding('NFLX', 5000)])
        assert ii.dividend_forecast(rows)['annual'] == 0.0


class TestProjection:
    def test_band_widens_with_time_and_brackets_the_expected_path(self):
        p = ii.projection(value=100_000, years=10, monthly_contribution=0)
        first, last = p['points'][0], p['points'][-1]
        assert first['low'] < first['expected'] < first['high']
        assert (last['high'] - last['low']) > (first['high'] - first['low'])

    def test_contributions_are_tracked_separately_from_growth(self):
        p = ii.projection(value=10_000, years=5, monthly_contribution=500)
        assert p['final']['contributed'] == pytest.approx(10_000 + 500 * 60, abs=1)
        assert p['growth'] > 0

    def test_real_value_is_below_nominal_under_inflation(self):
        p = ii.projection(value=100_000, years=20, inflation_pct=3.0)
        assert p['final']['real'] < p['final']['expected']

    def test_assumptions_ride_along_with_the_numbers(self):
        p = ii.projection(value=1000, years=1, annual_return_pct=6.0)
        assert p['assumptions']['annual_return_pct'] == 6.0
        assert 'Modelled' in p['basis']


class TestBenchmark:
    def test_a_short_span_refuses_to_compare(self):
        # Two weeks of snapshots is mostly accounts being linked. Reporting
        # that as "-20% versus the S&P" is a confident-looking lie.
        today = date(2026, 7, 25)
        hist = snapshots([100_000.0 - i * 500 for i in range(14)], end=today)
        cmp = ii.benchmark_compare(hist, 'sp500')
        assert cmp['available'] is False
        assert 'connected' in cmp['basis']
        assert 'excess_pct' not in cmp

    def test_excess_return_is_portfolio_minus_reference(self):
        today = date(2026, 7, 25)
        # 400 days of steady 20% total growth against a 10.5%/yr reference.
        hist = snapshots([100_000 * (1 + 0.20 * i / 399) for i in range(400)], end=today)
        cmp = ii.benchmark_compare(hist, 'sp500')
        assert cmp['available']
        assert cmp['excess_pct'] == pytest.approx(
            cmp['portfolio_pct'] - cmp['benchmark_pct'], abs=0.01)
        assert len(cmp['portfolio']) == len(cmp['reference'])

    def test_short_history_says_so_rather_than_inventing_a_line(self):
        assert ii.benchmark_compare([], 'sp500')['available'] is False

    def test_unknown_benchmark_falls_back_without_raising(self):
        today = date(2026, 7, 25)
        hist = snapshots([100.0, 105.0, 110.0], end=today)
        assert ii.benchmark_compare(hist, 'nonesuch')['benchmark'] == 'S&P 500'


# ══════════════════════════════════════════════════════════════════════════
# Story & insights
# ══════════════════════════════════════════════════════════════════════════

def _full_context(rows, cash=5000, net_worth=None, history=None, today=date(2026, 7, 25)):
    history = history if history is not None else snapshots([9000.0, 10_000.0], end=today)
    total = sum(r['value'] for r in rows)
    return {
        'positions': rows,
        'perf': ii.performance(history, today),
        'conc': ii.concentration(rows),
        'sector_alloc': ii.allocation(rows, 'sector', 'sector_known'),
        'region_alloc': ii.allocation(rows, 'region', 'region_known'),
        'dividends': ii.dividend_forecast(rows),
        'cash': cash,
        'net_worth': net_worth if net_worth is not None else total + cash,
    }


class TestStory:
    def test_leads_with_the_daily_move(self):
        rows = ii.build_positions([holding('AAPL', 10_000, gain=1000, gain_pct=11.1)])
        beats = ii.portfolio_story(today=date(2026, 7, 25), **_full_context(rows))
        assert beats[0]['key'] == 'today'
        assert 'up' in beats[0]['headline'].lower()

    def test_says_so_when_there_is_no_prior_snapshot(self):
        rows = ii.build_positions([holding('AAPL', 10_000)])
        ctx = _full_context(rows, history=[])
        beats = ii.portfolio_story(today=date(2026, 7, 25), **ctx)
        assert beats[0]['tone'] == 'neutral'
        assert 'No prior snapshot' in beats[0]['detail']

    def test_names_the_best_and_worst_positions(self):
        rows = ii.build_positions([
            holding('AAPL', 6000, cost_basis=4000, gain=2000, gain_pct=50.0),
            holding('INTC', 4000, cost_basis=6000, gain=-2000, gain_pct=-33.3),
        ])
        beats = {b['key']: b for b in ii.portfolio_story(today=date(2026, 7, 25),
                                                         **_full_context(rows))}
        assert 'AAPL' in beats['best']['headline']
        assert 'INTC' in beats['worst']['headline']


class TestInsights:
    def _insights(self, rows, cash=5000, **kw):
        ctx = _full_context(rows, cash=cash)
        return ii.insights(risk=ii.risk_score(rows, cash), **ctx, **kw)

    def test_critical_concentration_ranks_first(self):
        rows = ii.build_positions([holding('AAPL', 9000), holding('MSFT', 1000)])
        items = self._insights(rows)
        assert items[0]['severity'] == 'critical'
        assert 'AAPL' in items[0]['title']

    def test_a_dominant_index_fund_is_reassurance_not_an_alarm(self):
        rows = ii.build_positions([
            holding('VTI', 9000, asset_class='ETF', name='Vanguard Total Stock Market ETF'),
            holding('AAPL', 1000),
        ])
        items = self._insights(rows)
        assert not any(i['severity'] == 'critical' for i in items)
        reassurance = [i for i in items if 'that is fine' in i['title']]
        assert reassurance and reassurance[0]['severity'] == 'positive'
        assert 'VTI' in reassurance[0]['title']

    def test_every_item_explains_itself(self):
        rows = ii.build_positions([holding('AAPL', 9000), holding('MSFT', 1000)])
        assert all(i['why'] for i in self._insights(rows))

    def test_idle_cash_needs_both_a_share_and_real_dollars(self):
        rows = ii.build_positions([holding('VTI', 1000, asset_class='ETF')])
        # 50% of net worth, but only $1,000 — not worth nagging about.
        titles = [i['title'] for i in self._insights(rows, cash=1000)]
        assert not any('sitting in cash' in t for t in titles)
        rows = ii.build_positions([holding('VTI', 20_000, asset_class='ETF')])
        titles = [i['title'] for i in self._insights(rows, cash=40_000)]
        assert any('sitting in cash' in t for t in titles)

    def test_sector_drift_is_reported_against_a_prior_allocation(self):
        rows = ii.build_positions([holding('AAPL', 6000), holding('BND', 4000,
                                                                  asset_class='Bond')])
        prev = {'buckets': [{'label': 'Technology', 'pct': 40.0, 'value': 4000}]}
        titles = [i['title'] for i in self._insights(rows, prev_sector_alloc=prev)]
        assert any('Technology exposure rose' in t for t in titles)

    def test_wins_rank_below_problems(self):
        rows = ii.build_positions([holding(f'T{i}', 1000) for i in range(10)])
        items = self._insights(rows)
        severities = [i['severity'] for i in items]
        assert severities == sorted(severities,
                                    key=lambda s: {'critical': 0, 'warning': 1,
                                                   'info': 2, 'positive': 3}[s])

    def test_empty_portfolio_produces_no_insights_rather_than_noise(self):
        assert ii.insights(risk=ii.risk_score([], 0), **_full_context([], cash=0,
                                                                      net_worth=0)) == []


# ══════════════════════════════════════════════════════════════════════════
# Account rollup
# ══════════════════════════════════════════════════════════════════════════

class TestAccountRollup:
    def test_synced_and_manual_holdings_land_in_separate_cards(self):
        rows = ii.build_positions([
            holding('AAPL', 5000, account_id=1, account='Fidelity Brokerage'),
            holding('GOLD', 2000, account_id=None, account='Safe deposit', source='manual'),
        ])
        accounts = [{'id': 1, 'connection_id': 7, 'name': 'Fidelity Brokerage',
                     'account_type': 'brokerage', 'balance': 5000, 'mask': '4321',
                     'institution': 'plaid', 'last_synced_at': '2026-07-25 09:00:00'}]
        conns = [{'id': 7, 'status': 'connected', 'last_sync_at': '2026-07-25 09:00:00'}]
        cards = ii.account_rollup(rows, accounts, conns)
        assert len(cards) == 2
        synced = next(c for c in cards if c['synced'])
        manual = next(c for c in cards if not c['synced'])
        assert synced['positions'] == 1 and synced['status'] == 'connected'
        assert manual['name'] == 'Safe deposit' and manual['status'] == 'manual'

    def test_cards_are_ordered_by_value(self):
        rows = ii.build_positions([
            holding('A', 1000, account='Small', source='manual'),
            holding('B', 9000, account='Large', source='manual'),
        ])
        cards = ii.account_rollup(rows, [], [])
        assert [c['name'] for c in cards] == ['Large', 'Small']

    def test_connection_trouble_is_carried_onto_the_card(self):
        rows = ii.build_positions([holding('AAPL', 5000, account_id=1)])
        accounts = [{'id': 1, 'connection_id': 7, 'name': 'Broker',
                     'account_type': 'brokerage', 'balance': 5000, 'mask': None,
                     'institution': 'plaid', 'last_synced_at': None}]
        cards = ii.account_rollup(rows, accounts, [{'id': 7, 'status': 'expired'}])
        assert cards[0]['status'] == 'expired'
