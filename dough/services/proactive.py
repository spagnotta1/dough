"""Feature 11 — observations worth interrupting somebody with.

Every other module in this package answers a question. This one decides what is
worth saying when nobody asked, which is a different and harder problem: the
cost of a boring insight is not zero. A card that says something obvious teaches
the reader to skip the card, and once they do, the useful one three weeks later
is skipped too. So the brief's instruction — *prioritise usefulness over
quantity* — is implemented literally here, as a scoring function and a hard cap,
not as an intention.

## How an insight earns its place

Each candidate carries a `priority` from three things:

1. **Severity** — is this money being lost, a trend to correct, or a fact?
2. **Magnitude** — how many dollars does it concern, on a log scale, so that
   $4,000 outranks $400 without drowning it by a factor of ten.
3. **Actionability** — can the reader do something? A subscription that
   repriced can be cancelled. A category that drifted up cannot be "fixed", and
   scores lower even when the dollars are larger.

Below `MIN_PRIORITY` an observation is true and not worth saying, and is
dropped. An empty list is a valid and common result — `test_a_quiet_month_
produces_no_insights` exists to keep it that way.

## Everything here is derived, nothing is generated

An insight is a structured record with the figures attached, exactly like an
anomaly finding. `dough/ai/` turns it into a sentence. No text in this module
is written for the user to read verbatim; `summary` is a label for a card and a
seed for the model, and every number it mentions is in the same record for the
formatter to check against.

Allowed:   models, `dashboard_intel`, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

import math

from dough.services import analytics, anomalies, health, periods, trends
from dough.services.analytics import resolve_window

#: The most insights any surface receives. Past about five, a list of
#: observations becomes a report nobody reads.
DEFAULT_LIMIT = 5

#: Priority below which an observation is true and not worth saying.
MIN_PRIORITY = 30.0

#: Severity weights, on the same vocabulary `dashboard_intel` established.
_SEVERITY_WEIGHT = {'critical': 100.0, 'warning': 65.0, 'positive': 45.0,
                    'info': 30.0}

#: How much being able to act on something is worth, in priority points.
_ACTIONABLE_BONUS = 12.0

#: Months of history the trend and baseline comparisons read.
LOOKBACK_MONTHS = 6


def insights(limit=DEFAULT_LIMIT, *, anchor=None, months=LOOKBACK_MONTHS,
             findings=None, comparison=None):
    """The observations worth surfacing right now, most useful first.

    Assembled from the other engines rather than re-deriving anything: an
    insight that disagreed with the page it sits above would be worse than no
    insight, and calling the same functions is what makes that impossible.

    `findings` and `comparison` let a caller that has **already** computed them
    hand them in instead of paying twice. The Insights hub renders the
    detections and these insights on one page, and without this it ran the whole
    detector three times for one request — once for the list, once for the
    counts, once in here. `dough/ai/copilot.py` passes both, which is the whole
    point of that module.

    They are optional arguments rather than a cache because the duplication is
    within a single request, and a cache would be a second thing to reason about
    for a problem that is really just a shared value.

    When `comparison` is supplied no window is resolved at all — the comparison
    already carries the period it was computed for, and re-deriving one here
    could disagree with it.
    """
    if comparison is None:
        # `_from_comparison` and `_from_savings` both need it, and it is four
        # aggregate queries.
        comparison = periods.compare(resolve_window('month', anchor))

    candidates = []
    candidates += _from_anomalies(anchor, findings)
    candidates += _from_comparison(comparison)
    candidates += _from_trends(months, anchor)
    candidates += _from_savings(comparison, months, anchor)

    for candidate in candidates:
        candidate['priority'] = _priority(candidate)

    kept = [c for c in candidates if c['priority'] >= MIN_PRIORITY]
    kept.sort(key=lambda c: -c['priority'])
    return _deduplicate(kept)[:limit]


# ── Sources ─────────────────────────────────────────────────────────────────

def _from_anomalies(anchor, findings=None):
    """Unusual activity, already detected and already explained.

    Re-shaped rather than re-detected. `anomalies.detect` has the statistics;
    this only decides which of its findings rise to the level of an unprompted
    interruption, which is why subscription repricing is here and a duplicate
    coffee is filtered out by priority rather than by kind.
    """
    out = []
    if findings is None:
        findings = anomalies.detect(anchor=anchor, limit=10)
    for finding in findings:
        out.append({
            'kind': finding['kind'],
            'severity': finding['severity'],
            'summary': finding['summary'],
            'amount': finding['amount'],
            'actionable': finding['kind'] in (
                'subscription_hike', 'bill_increase', 'duplicate',
                'missing_paycheck'),
            'evidence': dict(finding['detail'],
                             transaction_id=finding['transaction_id'],
                             date=finding['date']),
            'source': 'anomalies',
        })
    return out


def _from_comparison(comparison):
    """The month-over-month story: what moved, and what drove it."""
    out = []

    spending = comparison['totals']['spending']
    if spending['material'] and spending['pct'] is not None:
        falling = spending['delta'] < 0
        out.append({
            'kind': 'spending_change',
            'severity': 'positive' if falling else 'warning',
            'summary': ('Spending is down on last period'
                        if falling else 'Spending is up on last period'),
            'amount': abs(spending['delta']),
            'actionable': not falling,
            'evidence': {'current': spending['current'],
                         'previous': spending['previous'],
                         'pct': spending['pct'],
                         'driver': comparison['headline']},
            'source': 'periods',
        })

    income = comparison['totals']['income']
    if income['material'] and income['pct'] is not None:
        rose = income['delta'] > 0
        out.append({
            'kind': 'income_change',
            'severity': 'positive' if rose else 'critical',
            'summary': ('Income is higher than last period'
                        if rose else 'Income is lower than last period'),
            'amount': abs(income['delta']),
            'actionable': False,
            'evidence': {'current': income['current'],
                         'previous': income['previous'],
                         'pct': income['pct']},
            'source': 'periods',
        })

    # "Your largest expense category changed" -- one of the brief's examples,
    # and only sayable by comparing the two rankings rather than either alone.
    now_top = _largest(comparison['current']['by_category'])
    before_top = _largest(comparison['previous']['by_category'])
    if now_top and before_top and now_top[0] != before_top[0]:
        out.append({
            'kind': 'top_category_changed',
            'severity': 'info',
            'summary': f'{now_top[0]} is now your largest spending category',
            'amount': now_top[1],
            'actionable': False,
            'evidence': {'now': {'category': now_top[0], 'amount': now_top[1]},
                         'before': {'category': before_top[0],
                                    'amount': before_top[1]}},
            'source': 'periods',
        })
    return out


def _from_trends(months, anchor):
    """Categories moving against a multi-month baseline.

    The brief's "grocery spending is 18% above your six-month average" lives
    here, and it is stated against the average rather than against last month
    precisely because one month is not a baseline.
    """
    out = []
    for trend in trends.category_trends(months, limit=4, anchor=anchor):
        if trend['direction'] not in ('rising', 'falling'):
            continue
        latest, average = trend['last'], trend['average']
        if not average:
            continue
        above = analytics.pct_change(latest, average)
        if above is None or abs(above) < 12:
            continue
        rising = trend['direction'] == 'rising'
        out.append({
            'kind': 'category_trend',
            'severity': 'warning' if rising else 'positive',
            'summary': (f'{trend["category"]} is {abs(above):.0f}% '
                        f'{"above" if above > 0 else "below"} its '
                        f'{trend["months"]}-month average'),
            'amount': abs(latest - average),
            'actionable': rising,
            'evidence': {'category': trend['category'],
                         'latest_month': latest,
                         'average': average,
                         'pct_vs_average': above,
                         'direction': trend['direction'],
                         'confidence': trend['confidence'],
                         'months': trend['months']},
            'source': 'trends',
        })
    return out


def _from_savings(comparison, months, anchor):
    """Savings-rate movement, and cash flow that swings too much to plan around.

    Both are reported on a *change* or a *property of the series*, never on a
    standing balance. "You have over $10,000 saved" is true every month for a
    year once it is true once, and an insight that repeats itself indefinitely
    is the fastest way to teach somebody to stop reading the surface.

    A savings *milestone* — crossing $10,000 for the first time — would be a
    genuine one-shot insight and is not implemented: it needs the previous
    balance to compare against, and `PortfolioSnapshotRow` only starts once
    accounts are synced, so for a CSV-only household there is nothing to cross.
    """
    out = []
    rate = comparison['totals']['savings_rate']

    if rate['delta_points'] is not None and abs(rate['delta_points']) >= 5:
        improved = rate['delta_points'] > 0
        out.append({
            'kind': 'savings_rate_change',
            'severity': 'positive' if improved else 'warning',
            'summary': ('Your savings rate improved this period'
                        if improved else 'Your savings rate slipped this period'),
            'amount': abs(comparison['current']['net']
                          - comparison['previous']['net']),
            'actionable': not improved,
            'evidence': {'current_pct': rate['current'],
                         'previous_pct': rate['previous'],
                         'change_points': rate['delta_points']},
            'source': 'periods',
        })

    stability = health.cash_flow_stability(months, anchor=anchor)
    if (stability['coefficient_of_variation'] is not None
            and stability['coefficient_of_variation'] > 1.0):
        out.append({
            'kind': 'cash_flow_volatile',
            'severity': 'info',
            'summary': 'Your month-to-month cash flow swings a lot',
            'amount': abs(stability['swing'] or 0.0),
            'actionable': False,
            'evidence': stability,
            'source': 'health',
        })
    return out


# ── Ranking ─────────────────────────────────────────────────────────────────

def _priority(candidate):
    """Severity, plus log-scaled dollars, plus a bonus for being actionable.

    The logarithm is the whole reason this is a formula rather than a sort by
    severity: without it a $40,000 house deposit outranks everything forever,
    and with a linear term a $4,000 finding is worth ten $400 ones, which is
    not how a reader values them.
    """
    base = _SEVERITY_WEIGHT.get(candidate['severity'], 30.0)
    amount = abs(float(candidate.get('amount') or 0.0))
    magnitude = math.log10(amount + 1.0) * 10.0
    bonus = _ACTIONABLE_BONUS if candidate.get('actionable') else 0.0
    return round(base + magnitude + bonus, 1)


def _deduplicate(candidates):
    """One insight per subject.

    A category that both spiked and is trending up produces two findings from
    two engines, and both are true. Saying both is repetition, and the reader
    experiences repetition as the surface having nothing to say.
    """
    seen, out = set(), []
    for candidate in candidates:
        evidence = candidate.get('evidence') or {}
        subject = (evidence.get('category')
                   or evidence.get('description')
                   or candidate['kind'])
        key = (candidate['kind'], subject)
        broad = ('subject', subject)
        if key in seen or broad in seen:
            continue
        seen.add(key)
        # Only category-scoped findings claim the broad key: two different
        # observations about Dining collapse, two unrelated income facts do not.
        if evidence.get('category'):
            seen.add(broad)
        out.append(candidate)
    return out


def _largest(by_category):
    if not by_category:
        return None
    category, amount = max(by_category.items(), key=lambda kv: kv[1])
    return category, amount


def digest(anchor=None, limit=DEFAULT_LIMIT):
    """Insights plus the headline figures, for a card that shows both."""
    window = resolve_window('month', anchor)
    summary = analytics.period_summary(window)
    return {
        'window': window.as_dict(),
        'income': summary['income'],
        'spending': summary['spending'],
        'net': summary['net'],
        'savings_rate': summary['savings_rate'],
        'insights': insights(limit, anchor=anchor),
    }


__all__ = ['insights', 'digest', 'DEFAULT_LIMIT', 'MIN_PRIORITY',
           'LOOKBACK_MONTHS']
