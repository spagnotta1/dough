"""Feature 4 — the financial health score, and where every input comes from.

The scoring arithmetic is **not here**. It lives in
`dashboard_intel.health_score`, which the dashboard has used since Phase 2, and
this module's job is to gather the measurable inputs that function needs and
hand them over. Writing a second scorer would put two different numbers called
"your financial health" in one product, and the one thing worse than a score
somebody distrusts is two of them.

What Phase 11 added is two *dimensions* — cash-flow stability and debt burden —
as optional arguments to that same function, defaulting to "not measured" so the
dashboard's number did not move the day this shipped. See its docstring.

## The methodology, in full

Six dimensions. Each is a 0–100 sub-score from a measured quantity, and the
overall score is their weighted mean. Weights are renormalised over whatever was
actually measurable, so a household with no budgets is not punished for a
feature it has not used.

| Dimension | Weight | Measured from | Full marks at |
| --- | --- | --- | --- |
| Savings rate | 35 | `(income − spending) / income` over the window | ≥ 20% kept |
| Cash runway | 25 | cash ÷ typical monthly outgo | ≥ 6 months |
| Budget adherence | 25 | budgets whose monthly pace is within limit | all of them |
| Spending trend | 15 | this window's outgo vs the previous one | ≥ 20% down |
| Cash-flow stability | 10 | std ÷ mean of monthly net flow | identical months |
| Debt burden | 10 | revolving balance ÷ monthly income | no balance |

Bands: **80+ strong**, **62+ steady**, **40+ needs attention**, below that
**strained**. The thresholds and the full-marks targets are the module-level
constants at the top of `dashboard_intel.py`, named so they can be argued with
rather than reverse-engineered.

### What is deliberately not scored

**Investment consistency**, which Phase 11's brief lists as a candidate. There is
no measurable input for it: `PortfolioSnapshotRow` records what a portfolio was
*worth* each day, not what was *paid into* it, and a rising balance in a rising
market is indistinguishable from a contribution. Scoring it would mean inferring
deposits from value changes, which is a guess, and a guess inside a number
labelled "health" is exactly the fabrication this phase exists to prevent. It is
omitted rather than estimated, and this paragraph is the reason.

### A caveat on debt that the caller should know

`FinancialAccount` carries `account_type == 'credit'`, so a revolving balance is
measurable — but `SyncRepository.compute_totals()`, and therefore net worth
across this application, **does not subtract it**. The debt factor here is
consequently the only place in Dough where a card balance affects a headline
number. That is a real inconsistency in the product rather than in this module,
and it is written down here rather than quietly patched, because changing what
net worth means is not a change that belongs in an analytics feature.

Allowed:   models, `dashboard_intel`, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from sqlalchemy import func

import dashboard_intel
from dough.services import analytics
from dough.services.analytics import lookback_window
from dough.services.networth import compute_net_worth, monthly_outgo
from models import Budget, FinancialAccount, db

#: How much history the score reads. Six months is long enough for a stability
#: measure to mean something and short enough to describe the present.
DEFAULT_MONTHS = 6

#: Below this many months of net-flow observations, stability is not measured at
#: all rather than measured badly. Two months have a variance and it means
#: nothing.
MIN_MONTHS_FOR_STABILITY = 3


def score(months=DEFAULT_MONTHS, *, anchor=None):
    """The overall score, its factors, and what would move it.

    Every figure the returned dictionary contains is either measured from the
    ledger or explicitly `None`. There is no default that stands in for missing
    data — see the module docstring on why unknown debt is not scored as zero.
    """
    window = lookback_window(months, anchor)
    summary = analytics.period_summary(window)

    previous = analytics.preceding_window(window)
    prior = analytics.period_summary(previous)

    net_worth = compute_net_worth()
    outgo_per_month = monthly_outgo(months)
    runway = (round(net_worth['cash'] / outgo_per_month, 1)
              if outgo_per_month else None)

    stability = cash_flow_stability(months, anchor=anchor)
    burden = debt_burden(summary['income'], window.months)

    result = dashboard_intel.health_score(
        income=summary['income'],
        outgo=summary['spending'],
        runway_months=runway,
        budget_map={b.category: float(b.monthly_limit) for b in Budget.query.all()},
        category_stats=_category_stats(summary),
        period_months=window.months,
        prev_outgo=prior['spending'],
        cash_flow_stability=stability['coefficient_of_variation'],
        debt=burden,
    )

    result['window'] = window.as_dict()
    result['inputs'] = {
        'income': summary['income'],
        'spending': summary['spending'],
        'net': summary['net'],
        'savings_rate': summary['savings_rate'],
        'cash': net_worth['cash'],
        'investments': net_worth['investments'],
        'typical_monthly_outgo': outgo_per_month,
        'runway_months': runway,
        'previous_spending': prior['spending'],
        'cash_flow_stability': stability,
        'debt': burden,
    }
    result['improvements'] = improvements(result)
    result['not_measured'] = _not_measured(result)
    return result


def cash_flow_stability(months=DEFAULT_MONTHS, *, anchor=None):
    """How much monthly net flow swings, as a coefficient of variation.

    Standard deviation over the *mean of the absolute* monthly flows, not over
    the signed mean. A household that alternates +$2,000 and −$2,000 has a
    signed mean near zero, and dividing by it produces an enormous coefficient
    from a stable-if-lumpy pattern — or a division by zero.

    Returns `coefficient_of_variation: None` below `MIN_MONTHS_FOR_STABILITY`
    months, which `health_score` reads as "not measured" and drops the factor
    for entirely.
    """
    window = lookback_window(months, anchor)
    series = analytics.monthly_series(window.start, window.end)
    flows = [month['net'] for month in series.values()]

    if len(flows) < MIN_MONTHS_FOR_STABILITY:
        return {'coefficient_of_variation': None, 'months': len(flows),
                'mean_net': None, 'swing': None,
                'note': 'not enough months to measure'}

    scale = sum(abs(f) for f in flows) / len(flows)
    mean = sum(flows) / len(flows)
    variance = sum((f - mean) ** 2 for f in flows) / len(flows)
    deviation = variance ** 0.5

    return {
        'coefficient_of_variation': round(deviation / scale, 2) if scale else None,
        'months': len(flows),
        'mean_net': round(mean, 2),
        'swing': round(deviation, 2),
        'best_month': round(max(flows), 2),
        'worst_month': round(min(flows), 2),
    }


def debt_burden(income_for_window, window_months):
    """Revolving balances against monthly income, or None when nothing is connected.

    None rather than a zero balance: a household with no credit account linked
    has *unknown* debt, and scoring the unknown as debt-free would award the
    best rating to the least-visible situation.
    """
    balance = _revolving_balance()
    if balance is None:
        return None
    monthly_income = (income_for_window / window_months) if window_months else 0.0
    return {
        'balance': round(balance, 2),
        'monthly_income': round(monthly_income, 2),
        'months_of_income': (round(balance / monthly_income, 1)
                             if monthly_income > 0 else None),
        'accounts': _revolving_account_count(),
    }


def _revolving_balance():
    """Total owed across active credit accounts, or None if there are none.

    Balances are stored positive on a credit account — it is what is owed, not a
    negative asset — so this is a plain sum. `abs` is applied anyway because a
    provider that reports the other convention would otherwise subtract a card
    balance from the household's debt, which is the wrong direction and silent.
    """
    if not _revolving_account_count():
        return None
    total = (db.session.query(func.sum(func.abs(FinancialAccount.balance)))
             .filter(FinancialAccount.account_type == 'credit',
                     FinancialAccount.is_active.is_(True))
             .scalar())
    return float(total or 0.0)


def _revolving_account_count():
    return int(db.session.query(func.count(FinancialAccount.id))
               .filter(FinancialAccount.account_type == 'credit',
                       FinancialAccount.is_active.is_(True))
               .scalar() or 0)


def _category_stats(summary):
    """The `{category: {inbound, outbound}}` shape `health_score` expects.

    `period_summary` already nets inbound against outbound per category, so
    `inbound` is zero here by construction. The shape is preserved because the
    dashboard passes the un-netted version and the scorer subtracts one from the
    other; passing the netted figure as `outbound` gives the same result through
    the same code path.
    """
    return {category: {'outbound': amount, 'inbound': 0.0}
            for category, amount in summary['by_category'].items()}


def improvements(result):
    """What would move the number most, worst factor first.

    Derived from the factors rather than written as advice: each entry names the
    dimension, what it currently is, and the headroom in weighted points. That
    keeps it a statement about the score's own arithmetic — "budgets are the
    biggest single gap" is checkable — rather than a financial recommendation,
    which this module is not qualified to issue and does not.
    """
    gaps = []
    for factor in result['factors']:
        if not factor['weight'] or factor['status'] in ('good', 'unset'):
            continue
        headroom = (100 - factor['score']) * factor['weight'] / 100.0
        if headroom <= 0:
            continue
        gaps.append({
            'key': factor['key'],
            'label': factor['label'],
            'status': factor['status'],
            'current': factor['detail'],
            'points_available': round(headroom, 1),
        })
    gaps.sort(key=lambda g: -g['points_available'])
    return gaps


def _not_measured(result):
    """Dimensions that produced no factor, named so a reader knows why.

    A score built from four of six dimensions and a score built from all six are
    different claims, and the difference has to be visible. Without this a
    formatter reports "your financial health is 78" with no way to say what that
    number did and did not look at.
    """
    measured = {factor['key'] for factor in result['factors']}
    missing = []
    if 'stability' not in measured:
        missing.append({'key': 'stability', 'label': 'Cash flow stability',
                        'reason': 'not enough months of history'})
    if 'debt' not in measured:
        missing.append({'key': 'debt', 'label': 'Debt burden',
                        'reason': 'no credit accounts connected'})
    missing.append({'key': 'investing', 'label': 'Investment consistency',
                    'reason': 'contributions are not recorded, only balances'})
    return missing


__all__ = ['score', 'cash_flow_stability', 'debt_burden', 'improvements',
           'DEFAULT_MONTHS', 'MIN_MONTHS_FOR_STABILITY']
