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

## One score, one code path  [UAT round 2]

Every surface that shows "financial health" calls `score()` here. The dashboard
used to call `dashboard_intel.health_score` directly with inputs it had summed
itself, and the result was two numbers under one name on two pages — 62 on the
dashboard against 82 on Insights for the same household, differing on the
savings rate, the spending trend, the cash runway *and* how many dimensions were
counted at all. Nothing was broken; the two callers were simply asking different
questions and labelling both answers "your financial health".

So the input-gathering lives here and nowhere else. A caller chooses the
`window` and the `account`; everything downstream of that — which categories
count as spending, which budgets apply, how the prior period is picked, which
dimensions are measured — is decided once, in this module. Two surfaces on the
same window now return the same number by construction, and
`tests/test_health.py::test_the_dashboard_and_insights_agree_on_one_window`
holds it there.

## What the window does and does not change

The score is *of a period*, and savings rate, spending trend, budget adherence
and cash-flow stability all read the window they are given. Two of the six do
not, deliberately:

**Cash runway** is a present-tense fact — how long the cash on hand would last
— not a property of the period being read. Scoring it from a filtered window
would say your buffer changed because you clicked a date chip, and would let one
page report 3.9 months while another reported 8.6 for the same household on the
same day. It is always cash ÷ typical monthly outgo over `RUNWAY_MONTHS`.

**Debt burden** is the same: what is owed today, against typical monthly income.

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

#: The lookback the two window-independent dimensions read — see the module
#: docstring. Separate from `DEFAULT_MONTHS` because that one is the *default
#: window*, which a caller overrides freely, and this one is a property of the
#: measurement: it must not move when somebody picks a date range.
RUNWAY_MONTHS = 6


def score(months=DEFAULT_MONTHS, *, anchor=None, window=None, account=None):
    """The overall score, its factors, and what would move it.

    `window` is the period scored, and it is the parameter that matters: pass
    the one the surface is showing and the number will match every other figure
    on that page. Omitted, it is the last `months` whole months ending at
    `anchor`, which is what a surface with no date filter wants.

    `account` narrows every windowed dimension to one account name, matching the
    dashboard's account chip. It does *not* narrow the runway or the debt
    burden: cash and revolving balances are household-wide facts, and reporting
    "3 weeks of runway" because a reader filtered to their spending card would
    be alarming and false.

    Every figure the returned dictionary contains is either measured from the
    ledger or explicitly `None`. There is no default that stands in for missing
    data — see the module docstring on why unknown debt is not scored as zero.
    """
    window = window or lookback_window(months, anchor)
    summary = analytics.period_summary(window, account=account)

    previous = analytics.preceding_window(window)
    prior = analytics.period_summary(previous, account=account)

    net_worth = compute_net_worth()
    outgo_per_month = monthly_outgo(RUNWAY_MONTHS)
    runway = (round(net_worth['cash'] / outgo_per_month, 1)
              if outgo_per_month else None)

    stability = cash_flow_stability(window=window, account=account)
    burden = debt_burden(summary['income'], _monthly_divisor(window))

    result = dashboard_intel.health_score(
        income=summary['income'],
        outgo=summary['spending'],
        runway_months=runway,
        budget_map=_budget_map(account),
        category_stats=_category_stats(summary),
        period_months=_monthly_divisor(window),
        prev_outgo=prior['spending'],
        cash_flow_stability=stability['coefficient_of_variation'],
        debt=burden,
    )

    result['window'] = window.as_dict()
    result['previous_window'] = previous.as_dict()
    result['account'] = account
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


def cash_flow_stability(months=DEFAULT_MONTHS, *, anchor=None, window=None,
                        account=None):
    """How much monthly net flow swings, as a coefficient of variation.

    Standard deviation over the *mean of the absolute* monthly flows, not over
    the signed mean. A household that alternates +$2,000 and −$2,000 has a
    signed mean near zero, and dividing by it produces an enormous coefficient
    from a stable-if-lumpy pattern — or a division by zero.

    Returns `coefficient_of_variation: None` below `MIN_MONTHS_FOR_STABILITY`
    months, which `health_score` reads as "not measured" and drops the factor
    for entirely. A window shorter than three months therefore drops it too,
    which is the honest answer: one month of history has no month-to-month
    variation to measure, and inventing one to keep a bar on screen would be a
    fabricated factor inside a number called health.
    """
    window = window or lookback_window(months, anchor)
    series = analytics.monthly_series(window.start, window.end, account=account)
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


def _monthly_divisor(window):
    """The window's length in months, never below one.

    Turning a window's total into a per-month rate means dividing by its length,
    and for a window shorter than a month that division is an extrapolation: the
    first four days of a month become 0.13, and a fortnight's income multiplies
    to seven times what anybody earns. The floor makes a short window read as
    "what happened so far", which is what a reader looking at four days means,
    rather than "the rate this implies", which nobody asked for.

    The dashboard has applied this rule to budgets since Phase 2 —
    `period_months = max(1.0, period_days / 30.44)` in its route. It is here now
    because the rule belongs to the measurement, not to one caller, and because
    a date filter on Insights made short windows reachable from a second page.
    """
    return max(1.0, window.months)


def _budget_map(account=None):
    """The monthly limits that apply to `account`, as `{category: limit}`.

    A budget is stored against one account name or against `'both'`. Filtering
    to an account means the budgets scoped to it plus the household-wide ones —
    dropping the `'both'` budgets would score a filtered view against a plan the
    household does not have.
    """
    return {b.category: float(b.monthly_limit) for b in Budget.query.all()
            if account is None or b.account_name in ('both', account)}


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
           'DEFAULT_MONTHS', 'MIN_MONTHS_FOR_STABILITY', 'RUNWAY_MONTHS']
