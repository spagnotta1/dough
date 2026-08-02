"""Feature 12 — the financial context Dough reasons over, assembled once.

`finance_context.build_finance_context(detail=True)` is the existing chat
context and it is large: 150 line items, 24 months of month-by-category history,
the same split again by account, 30 merchants, and every holding. It works, and
it costs several thousand tokens on every turn of every conversation.

This module is the summarised alternative. The distinction it draws is the whole
idea:

> Send the model **conclusions and the figures behind them**, not the rows those
> conclusions came from.

A trend is nine numbers and a direction, not twenty-four months of a category's
history. An anomaly is a sentence and its evidence, not the transactions that
triggered it. The model does not need to find the pattern — the analytics layer
already found it, correctly, in SQL — it needs to explain it.

## What this buys, and what it costs

Roughly a fifth of the tokens (`tests/test_ai_context.py` asserts the ratio, so
it cannot quietly regress). What it gives up is the ability to answer a question
about one specific old transaction, which the summary does not carry. That is
why this does **not replace** `build_finance_context`: `chat` keeps the detailed
context, and the copilot surfaces — which ask focused questions and need to
answer in about a second — use this one.

## The provenance rule

Every figure in the returned dictionary was computed by a function in
`dough/services/`, from this household's rows, inside this request. Nothing is
estimated, defaulted, or carried over. Where a figure could not be computed the
key is present with `None` and a reason, rather than absent — an absent key
invites the model to fill the gap, and a `None` with a stated reason invites it
to say "I cannot see that", which is the behaviour the whole phase is for.

`sections` lets a caller take only what a surface needs. The budget coach does
not need holdings, and a context that carries them anyway is tokens spent to
make an answer slower.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from dough.services import (analytics, anomalies, budgets, health, periods,
                            proactive, trends)
from dough.services.analytics import resolve_window
from dough.services.networth import compute_net_worth, portfolio_snapshot
from dough.services.recurring_service import detect_recurring_summary

#: Every section this builder can produce. The default set is everything except
#: `search`, which is question-specific and assembled by a caller that has one.
SECTIONS = ('coverage', 'period', 'comparison', 'categories', 'merchants',
            'trends', 'budgets', 'recurring', 'networth', 'holdings',
            'anomalies', 'insights', 'health')

DEFAULT_SECTIONS = SECTIONS

#: Trend lookback for the context. Six months, matching `proactive`, so the
#: copilot and the insight card cannot describe different baselines.
TREND_MONTHS = 6

#: Ceilings. Each exists because the section behind it is unbounded in principle
#: and a context that grows with the household's history is a context that
#: eventually stops fitting.
MAX_CATEGORIES = 12
MAX_MERCHANTS = 10
MAX_TRENDS = 6
MAX_ANOMALIES = 6
MAX_INSIGHTS = 5
MAX_HOLDINGS = 15


def build(sections=None, *, anchor=None, window=None, months=TREND_MONTHS,
          findings=None, comparison=None):
    """The summarised financial context, as a plain JSON-serialisable dict.

    `window` overrides the period under discussion — the dashboard passes the
    range the user has filtered to, so the copilot narrates the same months the
    page beneath it is showing. Without it the period is the current month.

    `findings` and `comparison` let a caller that has already computed them —
    `dough/ai/copilot.py` does, for every surface — hand them in instead of
    paying for them twice. Both are the expensive ones: `anomalies.detect()` is
    the single most costly call in the layer, and `periods.compare()` is four
    aggregate queries. Left as None they are computed here, so a direct caller
    needs to know nothing about this.
    """
    chosen = tuple(sections) if sections else DEFAULT_SECTIONS
    unknown = [name for name in chosen if name not in SECTIONS]
    if unknown:
        raise ValueError(f'unknown context sections: {unknown}')

    window = window or resolve_window('month', anchor)
    context = {'generated_for': window.as_dict(),
               'currency': 'USD',
               'note': _PROVENANCE_NOTE}

    if 'coverage' in chosen:
        context['coverage'] = analytics.coverage()

    summary = None
    if {'period', 'categories', 'comparison'} & set(chosen):
        summary = analytics.period_summary(window)

    if 'period' in chosen:
        context['period'] = {
            'label': window.label,
            'income': summary['income'],
            'spending': summary['spending'],
            'net_cash_flow': summary['net'],
            'savings_rate_pct': summary['savings_rate'],
            'transactions': summary['transaction_count'],
        }

    if 'categories' in chosen:
        context['spending_by_category'] = dict(
            list(summary['by_category'].items())[:MAX_CATEGORIES])
        context['largest_purchases'] = analytics.largest_purchases(
            window, limit=5)

    if 'comparison' in chosen:
        context['vs_previous_period'] = _comparison(window, comparison)

    if 'merchants' in chosen:
        context['top_merchants'] = analytics.merchant_totals(
            window, limit=MAX_MERCHANTS)

    if 'trends' in chosen:
        context['trends'] = _trends(months, anchor)

    if 'budgets' in chosen:
        context['budgets'] = _budgets(anchor)

    if 'recurring' in chosen:
        context['recurring'] = _recurring()

    if 'networth' in chosen:
        context['net_worth'] = compute_net_worth()

    if 'holdings' in chosen:
        context['portfolio'] = _portfolio()

    if 'anomalies' in chosen:
        context['unusual_activity'] = _anomalies(anchor, findings)

    if 'insights' in chosen:
        context['insights'] = _insights(anchor, findings, comparison)

    if 'health' in chosen:
        context['financial_health'] = _health(months, anchor)

    return context


_PROVENANCE_NOTE = (
    'Every figure here was computed from this household\'s own records. '
    'A value of null means it could not be computed, not zero — say you '
    'cannot see it rather than estimating it. Amounts are positive dollars '
    'unless stated; transfers between the household\'s own accounts are '
    'excluded from spending.')


# ── Sections ────────────────────────────────────────────────────────────────

def _comparison(window, result=None):
    """The what-changed findings, trimmed to what a sentence can use."""
    result = result if result is not None else periods.compare(window)
    totals = result['totals']
    return {
        'previous_label': result['previous']['window']['label'],
        'spending': _movement(totals['spending']),
        'income': _movement(totals['income']),
        'net': _movement(totals['net']),
        'savings_rate_points': totals['savings_rate']['delta_points'],
        'biggest_movers': [
            {'category': f['category'], 'direction': f['direction'],
             'from': f['previous'], 'to': f['current'],
             'change': f['delta'], 'pct': f['pct']}
            for f in result['categories'][:5]],
        'main_driver': result['headline'],
    }


def _movement(total):
    return {'now': total['current'], 'before': total['previous'],
            'change': total['delta'], 'pct': total['pct']}


def _trends(months, anchor=None):
    """Direction of travel per category, with the confidence attached.

    `confidence` rides along because the model is expected to hedge on a weak
    one, and it cannot hedge on a number it was not given. A bare slope invites
    a confident sentence about three data points.
    """
    found = []
    for trend in trends.category_trends(months, limit=MAX_TRENDS, anchor=anchor):
        found.append({
            'category': trend['category'],
            'direction': trend['direction'],
            'per_month_change': trend['slope_per_month'],
            'monthly_average': trend['average'],
            'latest_month': trend['last'],
            'months_observed': trend['months'],
            'confidence': trend['confidence'],
        })
    return found


def _budgets(anchor=None):
    """Budget status, with the month's progress alongside it.

    Reuses `budgets.status()` — the same derivation the Budgets page renders —
    so the copilot cannot call a budget healthy on a page that shows it red.

    `month_progress` ships with every row and is the single most important
    number here: 60% of a budget spent means opposite things on the 5th and the
    25th, and a model given only the percentage will confidently pick the wrong
    one. It is what makes a projection possible rather than a guess.
    """
    from datetime import datetime

    status = budgets.status(datetime(anchor.year, anchor.month, anchor.day)
                            if anchor else None)
    if not status['budgets']:
        return {'available': False, 'reason': 'no budgets set'}
    return {
        'available': True,
        'month_label': status['month_label'],
        'month_progress_pct': status['month_progress'],
        'total_budgeted': status['total_budgeted'],
        'total_spent': status['total_spent'],
        'budgets': [{'category': b['category'], 'account': b['account_name'],
                     'limit': b['monthly_limit'], 'spent': b['spent'],
                     'remaining': b['remaining'], 'used_pct': b['pct'],
                     'state': b['state'], 'last_month': b['prior'],
                     'change_pct': b['change_pct']}
                    for b in status['budgets']],
    }


def _recurring():
    summary = detect_recurring_summary()
    return {
        'bills': summary.get('bills', [])[:10],
        'subscriptions': summary.get('subscriptions', [])[:10],
    }


def _portfolio():
    snapshot = portfolio_snapshot()
    if not snapshot['positions']:
        return {'available': False, 'reason': 'no holdings recorded'}

    from models import Holding

    holdings = (Holding.query
                .order_by(Holding.current_value.desc())
                .limit(MAX_HOLDINGS).all())
    total = snapshot['value'] or 0.0
    return {
        'available': True,
        'value': snapshot['value'],
        'cost_basis': snapshot['cost_basis'],
        'unrealized_gain': snapshot['gain'],
        'unrealized_gain_pct': snapshot['gain_pct'],
        'positions': snapshot['positions'],
        'largest_holdings': [
            {'ticker': h.ticker, 'name': h.name,
             'value': round(float(h.current_value), 2),
             'asset_class': h.asset_class,
             'pct_of_portfolio': (round(float(h.current_value) / total * 100, 1)
                                  if total else None)}
            for h in holdings],
        'basis_note': ('unrealized_gain covers only holdings that have a cost '
                       'basis recorded' if snapshot['cost_basis'] is not None
                       else 'no cost basis recorded, so gain cannot be computed'),
    }


def _anomalies(anchor, findings=None):
    found = (anomalies.detect(anchor=anchor, limit=MAX_ANOMALIES)
             if findings is None else findings[:MAX_ANOMALIES])
    return [{'kind': a['kind'], 'severity': a['severity'],
             'what': a['summary'], 'amount': a['amount'], 'date': a['date'],
             'evidence': a['detail']} for a in found]


def _insights(anchor, findings=None, comparison=None):
    return [{'kind': i['kind'], 'severity': i['severity'],
             'observation': i['summary'], 'amount': i['amount'],
             'evidence': i['evidence']}
            for i in proactive.insights(MAX_INSIGHTS, anchor=anchor,
                                        findings=findings,
                                        comparison=comparison)]


def _health(months, anchor):
    result = health.score(months, anchor=anchor)
    return {
        'score': result['score'],
        'band': result['band'],
        'label': result['label'],
        'factors': [{'what': f['label'], 'score': f['score'],
                     'status': f['status'], 'why': f['detail']}
                    for f in result['factors'] if f['weight']],
        'biggest_gaps': [g['label'] for g in result['improvements'][:3]],
        'not_measured': [m['label'] for m in result['not_measured']],
        'methodology': ('weighted mean of the factors above, each 0-100 from a '
                        'measured quantity; see dough/services/health.py'),
    }


# ── Sizing ──────────────────────────────────────────────────────────────────

def estimated_tokens(context):
    """A rough token count for the serialised context.

    Four characters per token is the usual English approximation and is close
    enough for the only two questions anybody asks of it: "did this get bigger?"
    and "is it smaller than the detailed context?". It is not billing.
    """
    import json

    return len(json.dumps(context, default=str)) // 4


__all__ = ['build', 'estimated_tokens', 'SECTIONS', 'DEFAULT_SECTIONS',
           'TREND_MONTHS']
