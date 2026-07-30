"""Net worth, portfolio valuation, and the wealth snapshot the Investments page reasons over.

Moved verbatim out of `create_app()`'s closures in Phase 3. Every function here
is framework-free apart from needing an app context for `Model.query`, which
means all of it is callable from the sync scheduler thread and directly from a
test without a request.

`wealth_snapshot` is the widest of these: it derives one dictionary that the
route renders, the two copilot endpoints send to the model, and the tests assert
on. Keeping that single derivation is the reason the AI cannot narrate a figure
the page does not display, so it must stay one function with one set of inputs.

Allowed:   models, `investments_intel`, `finance_sync.repository`, SQLAlchemy,
           stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`
"""

from datetime import date, datetime, timedelta

from sqlalchemy import func

import investments_intel
from finance_sync.repository import SyncRepository
from models import Holding, PortfolioSnapshotRow, Transaction, db


def compute_net_worth():
    """Net-worth breakdown from synced accounts + holdings.

    Prefers automatically synchronized balances (finance_sync) and falls
    back to the manually entered AccountBalance rows for account types
    that have never been synced. Adds brokerage/crypto detail on top of
    the original keys.
    """
    return SyncRepository.compute_totals()


def portfolio_snapshot():
    """Portfolio value with unrealized gain, where a cost basis exists.

    Holdings entered by hand often have no basis. Those still count toward
    the value but are left out of the gain, so the percentage reflects only
    the positions it can actually be computed for rather than silently
    treating cost as zero.
    """
    holdings = Holding.query.all()
    value = sum(float(h.current_value) for h in holdings)
    with_basis = [h for h in holdings if h.cost_basis is not None]
    basis = sum(h.cost_basis for h in with_basis)
    covered = sum(float(h.current_value) for h in with_basis)
    gain = covered - basis if with_basis else 0.0
    return {
        'value': round(value, 2),
        'cost_basis': round(basis, 2) if with_basis else None,
        'gain': round(gain, 2) if with_basis else None,
        'gain_pct': round(gain / basis * 100, 2) if basis else None,
        'positions': len(holdings),
    }


def monthly_outgo(months=6):
    """Typical monthly spending, for the emergency-liquidity factor.

    Transfers between the user's own accounts are movement, not spending,
    so they are excluded — counting them would make the buffer look far
    thinner than it is.
    """
    cutoff = datetime.now() - timedelta(days=months * 30)
    total = float(db.session.query(func.sum(func.abs(Transaction.amount)))
                  .filter(Transaction.date >= cutoff, Transaction.amount < 0,
                          func.lower(Transaction.category).notin_(('transfer', 'transfers')))
                  .scalar() or 0.0)
    return round(total / months, 2) if total else None


def snapshot_history(days=730):
    """Daily net-worth snapshots, oldest first — the only real price history."""
    cutoff = date.today() - timedelta(days=days)
    rows = (PortfolioSnapshotRow.query
            .filter(PortfolioSnapshotRow.snapshot_date >= cutoff)
            .order_by(PortfolioSnapshotRow.snapshot_date).all())
    return [r.to_dict() for r in rows]


def wealth_snapshot(benchmark='sp500', horizon=10, contribution=0.0):
    """Everything the Investments page reasons over, in one place.

    The route renders it, the copilot endpoints send it to the model, and
    the tests can call it directly — one derivation, three consumers, no
    chance of the AI narrating figures the page does not show.
    """
    holding_rows = [h.to_dict() for h in
                    Holding.query.order_by(Holding.asset_class, Holding.ticker).all()]
    nw = compute_net_worth()
    history = snapshot_history()
    today = date.today()

    positions = investments_intel.build_positions(holding_rows)
    sector_alloc = investments_intel.allocation(positions, 'sector', 'sector_known')
    region_alloc = investments_intel.allocation(positions, 'region', 'region_known')
    class_alloc = investments_intel.allocation(positions, 'asset_class')
    cap_alloc = investments_intel.allocation(positions, 'market_cap', 'market_cap_known')
    account_alloc = investments_intel.allocation(positions, 'account')
    conc = investments_intel.concentration(positions)
    perf = investments_intel.performance(history, today)
    measured_vol, vol_is_measured = investments_intel.volatility(history)
    risk = investments_intel.risk_score(positions, nw['cash'],
                                        measured_vol if vol_is_measured else None)
    div = investments_intel.diversification_score(positions, sector_alloc,
                                                  region_alloc, conc)
    health = investments_intel.health_score(
        positions=positions, cash=nw['cash'], net_worth=nw['net_worth'],
        diversification=div, conc=conc, monthly_expenses=monthly_outgo())
    dividends = investments_intel.dividend_forecast(positions)

    # Allocation "before" is the cost-basis-weighted shape: what the money
    # looked like when it was put in, versus what it looks like now. That
    # difference is exactly the drift winners cause on their own.
    basis_rows = [dict(p, value=p['cost_basis']) for p in positions
                  if p.get('cost_basis')]
    prev_sector = (investments_intel.allocation(basis_rows, 'sector', 'sector_known')
                   if len(basis_rows) == len(positions) and basis_rows else None)

    story = investments_intel.portfolio_story(
        positions=positions, perf=perf, conc=conc, sector_alloc=sector_alloc,
        region_alloc=region_alloc, dividends=dividends, cash=nw['cash'],
        net_worth=nw['net_worth'], today=today)
    feed = investments_intel.insights(
        positions=positions, conc=conc, sector_alloc=sector_alloc,
        region_alloc=region_alloc, dividends=dividends, cash=nw['cash'],
        net_worth=nw['net_worth'], risk=risk, perf=perf,
        prev_sector_alloc=prev_sector)

    return {
        'holdings': holding_rows,
        'positions': positions,
        'nw': nw,
        'history': history,
        'performance': perf,
        'annualized_return': investments_intel.annualized_return(history),
        'drawdown': investments_intel.drawdown(history),
        'volatility': {'value': measured_vol if vol_is_measured
                       else investments_intel.estimated_volatility(positions),
                       'measured': vol_is_measured},
        'allocation': {
            'asset_class': class_alloc, 'sector': sector_alloc,
            'region': region_alloc, 'market_cap': cap_alloc,
            'account': account_alloc,
        },
        'concentration': conc,
        'risk': risk,
        'diversification': div,
        'health': health,
        'dividends': dividends,
        'story': story,
        'insights': feed,
        'benchmark': investments_intel.benchmark_compare(history, benchmark),
        'projection': investments_intel.projection(
            value=nw['investments'], years=horizon,
            monthly_contribution=contribution,
            volatility_pct=measured_vol if vol_is_measured else None),
    }
