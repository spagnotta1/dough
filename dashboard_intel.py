"""Dashboard intelligence — the layer that turns figures into a story.

The dashboard route already knows *what* the numbers are. This module decides
what they *mean*: which of them deserve the user's attention right now, how
healthy the overall picture is, and where the balance is heading over the next
few weeks.

Everything here is a pure function over plain dicts and lists — no Flask, no
SQLAlchemy, no clock reads except the ``today`` you pass in. That keeps the
reasoning testable in isolation and keeps the route thin.

Vocabulary used throughout:

``severity``
    ``critical`` (money is being lost or about to run out), ``warning``
    (a trend worth correcting), ``info`` (a fact worth knowing), ``positive``
    (something went right — worth saying so, or the app only ever nags).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

# ── Tunables ────────────────────────────────────────────────────────────────
# Named rather than inlined because these are judgement calls, not facts, and
# a reader should be able to see and argue with all of them in one place.

GOOD_SAVINGS_RATE = 20.0      # % of income kept — the widely used target
GOOD_RUNWAY_MONTHS = 6.0      # months of cash covering typical outgo
THIN_RUNWAY_MONTHS = 2.0      # below this, runway itself is the headline
MATERIAL_SWING_PCT = 25.0     # category change worth surfacing
MATERIAL_SWING_USD = 75.0     # ...but only if the dollars are real
INCOME_DROP_PCT = 15.0
SUBSCRIPTION_HIKE_PCT = 8.0
STEADY_CASH_FLOW_CV = 0.75    # net-flow variation above which cash flow is erratic
HEAVY_DEBT_MONTHS = 6.0       # revolving balance worth this much income is heavy
BILL_HORIZON_DAYS = 14
FORECAST_DAYS = 45

_AVG_MONTH_DAYS = 30.44


# ── Small helpers ───────────────────────────────────────────────────────────

def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    raise TypeError(f'unsupported date value: {value!r}')


def _safe_div(numerator, denominator, default=0.0):
    return numerator / denominator if denominator else default


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _pct_change(current, previous):
    """Percent change, or None when there is no baseline to compare against."""
    if not previous:
        return None
    return (current - previous) / abs(previous) * 100.0


def _money(value):
    """Format for prose: $1,284 — whole dollars, which is the resolution
    people actually reason about when scanning a dashboard."""
    return f'${value:,.0f}'


# ══════════════════════════════════════════════════════════════════════════
# Financial health score
# ══════════════════════════════════════════════════════════════════════════

# Band → (key, badge label, phrase that completes "Your finances are …").
# The badge and the sentence need different grammar; deriving one from the
# other produced "Your finances are looking needs attention".
_BANDS = (
    (80, 'strong',   'Strong',          'in strong shape'),
    (62, 'steady',   'Steady',          'holding steady'),
    (40, 'watch',    'Needs attention', 'worth a closer look'),
    (0,  'strained', 'Strained',        'under real strain'),
)


def _band(score):
    for threshold, key, label, _phrase in _BANDS:
        if score >= threshold:
            return key, label
    return 'strained', 'Strained'


def _band_phrase(key):
    for _threshold, band_key, _label, phrase in _BANDS:
        if band_key == key:
            return phrase
    return 'hard to read'


def health_score(*, income, outgo, runway_months, budget_map, category_stats,
                 period_months, prev_outgo=0.0, cash_flow_stability=None,
                 debt=None):
    """A single 0–100 read on financial health, plus the factors behind it.

    Four inputs, weighted by how much each one actually moves the needle on
    someone's finances:

    * **Savings rate (35)** — the strongest single predictor of whether the
      picture improves or decays over time.
    * **Cash runway (25)** — how long the cash on hand covers normal outgo.
    * **Budget adherence (25)** — how much of the plan is being kept to.
      Skipped (and its weight redistributed) when no budgets are set, rather
      than punishing someone for not having used the feature.
    * **Spending trend (15)** — direction of travel versus the prior period.

    ``runway_months`` is passed in rather than derived from ``outgo`` so that
    it agrees with the figure the forecast shows. Deriving it here from the
    filtered period would put two different runway numbers on one page.

    The factors are returned alongside the score so the UI can explain the
    number instead of asking the user to trust it.

    ## The two optional dimensions [Phase 11]

    ``cash_flow_stability`` and ``debt`` are additions, and both default to
    ``None`` meaning *not measured* — in which case no factor is emitted, no
    weight is assigned, and the returned score is bit-for-bit what it was
    before. That default is what lets `dough/services/health.py` compute a
    richer score for the copilot without moving the number on the dashboard,
    which still calls this with four arguments.

    They are `None`-by-default rather than zero-by-default for the reason the
    rest of this module is careful about: a household with no credit accounts
    connected has *unknown* debt, not no debt, and scoring the unknown as
    perfect would hand a strong rating to the one household most likely to be
    in trouble.

    ``cash_flow_stability`` is a 0–1 coefficient of variation of monthly net
    flow (lower is steadier). ``debt`` is ``{'balance', 'monthly_income'}``.
    """
    factors = []

    # ── Savings rate ────────────────────────────────────────────────────
    rate = _safe_div(income - outgo, income, 0.0) * 100.0 if income > 0 else None
    if rate is None:
        savings_pts, savings_status, savings_detail = 50.0, 'unknown', 'No income recorded this period'
    else:
        savings_pts = _clamp(rate / GOOD_SAVINGS_RATE * 100.0)
        savings_status = 'good' if rate >= GOOD_SAVINGS_RATE else 'ok' if rate >= 5 else 'poor'
        savings_detail = f'Keeping {rate:.0f}% of income' if rate >= 0 else \
                         f'Spending {abs(rate):.0f}% more than earned'
    factors.append({'key': 'savings', 'label': 'Savings rate', 'weight': 35,
                    'score': round(savings_pts), 'status': savings_status,
                    'detail': savings_detail,
                    'value': None if rate is None else round(rate, 1)})

    # ── Cash runway ─────────────────────────────────────────────────────
    runway = runway_months
    if runway is None:
        runway_pts, runway_status, runway_detail = 60.0, 'unknown', 'Not enough spending history'
    else:
        runway_pts = _clamp(runway / GOOD_RUNWAY_MONTHS * 100.0)
        runway_status = 'good' if runway >= GOOD_RUNWAY_MONTHS else \
                        'ok' if runway >= THIN_RUNWAY_MONTHS else 'poor'
        runway_detail = f'{runway:.1f} months of cash at this burn rate'
    factors.append({'key': 'runway', 'label': 'Cash runway', 'weight': 25,
                    'score': round(runway_pts), 'status': runway_status,
                    'detail': runway_detail,
                    'value': None if runway is None else round(runway, 1)})

    # ── Budget adherence ────────────────────────────────────────────────
    tracked = [(cat, limit) for cat, limit in (budget_map or {}).items() if limit > 0]
    if tracked:
        within = 0
        for cat, limit in tracked:
            stats = category_stats.get(cat, {})
            spent = max(0.0, stats.get('outbound', 0.0) - stats.get('inbound', 0.0))
            if _safe_div(spent, period_months) <= limit:
                within += 1
        budget_pts = within / len(tracked) * 100.0
        budget_status = 'good' if budget_pts >= 85 else 'ok' if budget_pts >= 60 else 'poor'
        budget_detail = f'{within} of {len(tracked)} budgets on track'
        budget_weight = 25
    else:
        budget_pts, budget_status, budget_weight = 0.0, 'unset', 0
        budget_detail = 'No budgets set yet'
    factors.append({'key': 'budgets', 'label': 'Budget adherence', 'weight': budget_weight,
                    'score': round(budget_pts), 'status': budget_status,
                    'detail': budget_detail,
                    'value': round(budget_pts) if tracked else None})

    # ── Spending trend ──────────────────────────────────────────────────
    trend = _pct_change(outgo, prev_outgo)
    if trend is None:
        trend_pts, trend_status, trend_detail = 60.0, 'unknown', 'No prior period to compare'
    else:
        # -20% or better earns full marks; +20% or worse earns none.
        trend_pts = _clamp(50.0 - trend * 2.5)
        trend_status = 'good' if trend <= -5 else 'ok' if trend <= 10 else 'poor'
        direction = 'down' if trend < 0 else 'up'
        trend_detail = f'Spending {direction} {abs(trend):.0f}% vs. prior period'
    factors.append({'key': 'trend', 'label': 'Spending trend', 'weight': 15,
                    'score': round(trend_pts), 'status': trend_status,
                    'detail': trend_detail,
                    'value': None if trend is None else round(trend, 1)})

    # ── Cash-flow stability (optional) ──────────────────────────────────
    # A household earning the same amount every month and one whose income
    # swings by half can share a savings rate and be in very different
    # positions. Measured as the coefficient of variation of monthly net flow.
    if cash_flow_stability is not None:
        # 0.0 (identical every month) earns full marks; STEADY_CASH_FLOW_CV and
        # above earns none.
        stability_pts = _clamp(100.0 - cash_flow_stability / STEADY_CASH_FLOW_CV * 100.0)
        stability_status = ('good' if cash_flow_stability <= STEADY_CASH_FLOW_CV / 2
                            else 'ok' if cash_flow_stability <= STEADY_CASH_FLOW_CV
                            else 'poor')
        factors.append({
            'key': 'stability', 'label': 'Cash flow stability', 'weight': 10,
            'score': round(stability_pts), 'status': stability_status,
            'detail': ('Month-to-month cash flow varies by '
                       f'{cash_flow_stability * 100:.0f}% of its average'),
            'value': round(cash_flow_stability, 2)})

    # ── Debt burden (optional) ──────────────────────────────────────────
    if debt is not None:
        balance = float(debt.get('balance') or 0.0)
        monthly_income = float(debt.get('monthly_income') or 0.0)
        if monthly_income > 0:
            ratio = balance / monthly_income
            debt_pts = _clamp(100.0 - ratio / HEAVY_DEBT_MONTHS * 100.0)
            debt_status = ('good' if ratio <= HEAVY_DEBT_MONTHS / 3
                           else 'ok' if ratio <= HEAVY_DEBT_MONTHS else 'poor')
            debt_detail = (f'{_money(balance)} owed, about {ratio:.1f} months of income'
                           if balance else 'No revolving balance')
        else:
            # A balance with no income to service it cannot be expressed as a
            # ratio. Scored neutrally and labelled, rather than scored as zero.
            ratio = None
            debt_pts, debt_status = 50.0, 'unknown'
            debt_detail = f'{_money(balance)} owed, no income recorded to compare'
        factors.append({
            'key': 'debt', 'label': 'Debt burden', 'weight': 10,
            'score': round(debt_pts), 'status': debt_status,
            'detail': debt_detail,
            'value': None if ratio is None else round(ratio, 1)})

    total_weight = sum(f['weight'] for f in factors) or 1
    score = round(sum(f['score'] * f['weight'] for f in factors) / total_weight)
    band, label = _band(score)
    return {'score': score, 'band': band, 'label': label, 'factors': factors}


# ══════════════════════════════════════════════════════════════════════════
# Upcoming bills
# ══════════════════════════════════════════════════════════════════════════

def upcoming_bills(recurring, today, horizon_days=BILL_HORIZON_DAYS):
    """Bills and subscriptions expected to hit within ``horizon_days``.

    ``detect_recurring`` reports one ``next_expected`` per payee. That date can
    already be in the past for a bill that landed late, and a weekly or
    fortnightly charge can fall due more than once inside the window, so each
    payee is rolled forward on its own median cadence until it lands in range.
    """
    today = _as_date(today)
    horizon = today + timedelta(days=horizon_days)
    out = []

    for kind in ('bills', 'subscriptions'):
        for group in (recurring or {}).get(kind, []):
            gap = max(1, int(round(group.get('median_gap_days') or 30)))
            try:
                due = _as_date(group['next_expected'])
            except (KeyError, TypeError, ValueError):
                continue
            # Roll a stale expectation forward to the next real occurrence.
            guard = 0
            while due < today and guard < 400:
                due += timedelta(days=gap)
                guard += 1
            # Emit every occurrence that lands inside the window.
            while due <= horizon and guard < 400:
                out.append({
                    'description': group.get('description', 'Recurring payment'),
                    'category': group.get('category', 'Uncategorized'),
                    'amount': abs(float(group.get('monthly_amount') or 0.0)),
                    'due_date': due.strftime('%Y-%m-%d'),
                    'days_away': (due - today).days,
                    'kind': 'bill' if kind == 'bills' else 'subscription',
                })
                due += timedelta(days=gap)
                guard += 1

    out.sort(key=lambda b: (b['days_away'], -b['amount']))
    return out


def subscription_hikes(recurring, transactions):
    """Subscriptions whose latest charge is materially above their own norm.

    ``detect_recurring`` reports the current price. Comparing it against the
    median of the same payee's earlier charges is what turns "you pay $15.99
    for X" into "X went up" — the thing worth telling someone about.
    """
    from recurring import normalize_description  # local: avoids a cycle at import

    hikes = []
    for group in (recurring or {}).get('subscriptions', []):
        keys = group.get('desc_keys') or []
        if not keys:
            continue
        charges = sorted(
            (t for t in transactions
             if t['amount'] < 0 and any(k in normalize_description(t['description'])
                                        or normalize_description(t['description']) in k
                                        for k in keys)),
            key=lambda t: t['date'])
        if len(charges) < 3:
            continue
        current = abs(float(charges[-1]['amount']))
        baseline = abs(_median([float(t['amount']) for t in charges[:-1]]))
        if baseline <= 0:
            continue
        delta_pct = (current - baseline) / baseline * 100.0
        if delta_pct >= SUBSCRIPTION_HIKE_PCT and (current - baseline) >= 1.0:
            hikes.append({
                'description': group.get('description', 'Subscription'),
                'was': round(baseline, 2),
                'now': round(current, 2),
                'delta_pct': round(delta_pct),
                'annual_impact': round((current - baseline) * 12),
            })
    hikes.sort(key=lambda h: h['annual_impact'], reverse=True)
    return hikes


# ══════════════════════════════════════════════════════════════════════════
# Attention center
# ══════════════════════════════════════════════════════════════════════════

_SEVERITY_RANK = {'critical': 0, 'warning': 1, 'info': 2, 'positive': 3}


def attention_items(*, budget_alerts, category_stats, prev_category_stats,
                    income, outgo, prev_income, prev_outgo, cash,
                    period_months, runway_months, bills, hikes,
                    anomaly_count=0, portfolio=None, limit=8):
    """Everything competing for the user's attention, ranked.

    One list replaces the alert strips that used to be scattered down the page.
    Each item is self-contained — severity, what happened, and the one action
    that resolves it — so the UI only has to render, never interpret.
    """
    items = []

    def add(severity, key, title, detail, action_label=None, action_url=None, icon='dot'):
        items.append({'severity': severity, 'key': key, 'title': title,
                      'detail': detail, 'action_label': action_label,
                      'action_url': action_url, 'icon': icon})

    monthly_outgo = _safe_div(outgo, period_months)

    # ── Runway ──────────────────────────────────────────────────────────
    if runway_months is not None and runway_months < THIN_RUNWAY_MONTHS:
        add('critical', 'runway', 'Cash runway is thin',
            f'{_money(cash)} on hand covers about {runway_months:.1f} months '
            f'at {_money(monthly_outgo)}/mo of spending.',
            'Review spending', '/transactions', 'alert')

    # ── Cash flow ───────────────────────────────────────────────────────
    net = income - outgo
    if net < 0:
        severity = 'critical' if abs(net) > _safe_div(income, 4) else 'warning'
        add(severity, 'cashflow', 'Spending exceeded income',
            f'{_money(abs(net))} more went out than came in this period '
            f'({_money(outgo)} out vs. {_money(income)} in).',
            'See transactions', '/transactions', 'trend-down')

    # ── Budgets ─────────────────────────────────────────────────────────
    over = [a for a in budget_alerts if a['level'] == 'over']
    near = [a for a in budget_alerts if a['level'] != 'over']
    if over:
        worst = over[0]
        rest = f' and {len(over) - 1} other{"s" if len(over) > 2 else ""}' if len(over) > 1 else ''
        add('critical', 'budget-over',
            f'{len(over)} budget{"s" if len(over) > 1 else ""} exceeded',
            f'{worst["category"]} is at {worst["pct"]}% of its '
            f'{_money(worst["limit"])}/mo limit{rest}.',
            'Manage budgets', '/budgets', 'target')
    if near:
        worst = near[0]
        add('warning', 'budget-near',
            f'{len(near)} budget{"s" if len(near) > 1 else ""} close to the limit',
            f'{worst["category"]} is at {worst["pct"]}% of '
            f'{_money(worst["limit"])}/mo.',
            'Manage budgets', '/budgets', 'target')

    # ── Unusual spending ────────────────────────────────────────────────
    swings = []
    for cat, stats in category_stats.items():
        current = stats.get('outbound', 0.0)
        previous = (prev_category_stats.get(cat) or {}).get('outbound', 0.0)
        change = _pct_change(current, previous)
        if change is None or change < MATERIAL_SWING_PCT:
            continue
        if (current - previous) < MATERIAL_SWING_USD:
            continue
        swings.append((current - previous, cat, change, current, previous))
    swings.sort(reverse=True)
    if swings:
        delta, cat, change, current, previous = swings[0]
        others = f' (+{len(swings) - 1} more categor{"ies" if len(swings) > 2 else "y"})' \
                 if len(swings) > 1 else ''
        add('warning', 'unusual-spend', f'{cat} spending jumped {change:.0f}%',
            f'{_money(current)} this period vs. {_money(previous)} last '
            f'— {_money(delta)} more{others}.',
            f'View {cat}', f'/transactions?category={cat}&type=outgo', 'trend-up')

    # ── Income ──────────────────────────────────────────────────────────
    income_change = _pct_change(income, prev_income)
    if income_change is not None and income_change <= -INCOME_DROP_PCT:
        add('warning', 'income-down', f'Income fell {abs(income_change):.0f}%',
            f'{_money(income)} this period vs. {_money(prev_income)} last.',
            'See income', '/transactions?type=inbound', 'trend-down')

    # ── Upcoming bills ──────────────────────────────────────────────────
    if bills:
        total = sum(b['amount'] for b in bills)
        soonest = bills[0]
        when = ('today' if soonest['days_away'] == 0
                else 'tomorrow' if soonest['days_away'] == 1
                else f'in {soonest["days_away"]} days')
        severity = 'warning' if total > cash else 'info'
        add(severity, 'bills',
            f'{len(bills)} bill{"s" if len(bills) > 1 else ""} due in the next '
            f'{BILL_HORIZON_DAYS} days',
            f'{_money(total)} total — {soonest["description"]} '
            f'({_money(soonest["amount"])}) {when}.',
            'View recurring', '/recurring', 'calendar')

    # ── Subscription price rises ────────────────────────────────────────
    if hikes:
        top = hikes[0]
        rest = f' (+{len(hikes) - 1} more)' if len(hikes) > 1 else ''
        add('warning', 'sub-hike', f'{top["description"]} went up {top["delta_pct"]}%',
            f'${top["was"]:,.2f} → ${top["now"]:,.2f} per charge, about '
            f'{_money(top["annual_impact"])}/yr more{rest}.',
            'Review subscriptions', '/recurring', 'trend-up')

    # ── Portfolio movement ──────────────────────────────────────────────
    # Only holdings with a cost basis can say anything; a manually entered
    # value with no basis is a number, not a movement.
    if portfolio and portfolio.get('cost_basis'):
        gain = portfolio.get('gain', 0.0)
        gain_pct = portfolio.get('gain_pct', 0.0)
        if abs(gain_pct) >= 5 and abs(gain) >= 100:
            up = gain > 0
            add('positive' if up else 'warning', 'portfolio',
                f'Portfolio is {"up" if up else "down"} {abs(gain_pct):.1f}%',
                f'{_money(portfolio["value"])} now, {_money(abs(gain))} '
                f'{"above" if up else "below"} what you paid.',
                'View portfolio', '/investments', 'trend-up' if up else 'trend-down')

    # ── Anomalies ───────────────────────────────────────────────────────
    if anomaly_count:
        add('info', 'anomalies',
            f'{anomaly_count} unusual transaction{"s" if anomaly_count > 1 else ""} flagged',
            'Transactions that do not match your normal pattern are waiting for review.',
            'Review', '/anomalies', 'search')

    # ── Wins worth naming ───────────────────────────────────────────────
    outgo_change = _pct_change(outgo, prev_outgo)
    if outgo_change is not None and outgo_change <= -10:
        add('positive', 'spend-down', f'Spending is down {abs(outgo_change):.0f}%',
            f'{_money(outgo)} this period vs. {_money(prev_outgo)} last — '
            f'{_money(prev_outgo - outgo)} saved.',
            None, None, 'trend-down')

    rate = _safe_div(net, income, 0.0) * 100.0 if income > 0 else 0.0
    prev_rate = _safe_div(prev_income - prev_outgo, prev_income, 0.0) * 100.0 if prev_income > 0 else None
    if rate >= GOOD_SAVINGS_RATE and (prev_rate is None or rate >= prev_rate):
        add('positive', 'savings-strong', f'Savings rate at {rate:.0f}%',
            f'Keeping {_money(net)} of {_money(income)} earned this period.',
            None, None, 'check')

    if not items:
        add('positive', 'all-clear', 'Nothing needs your attention',
            'No budget overruns, no unusual spending, and no bills due soon.',
            None, None, 'check')

    items.sort(key=lambda i: _SEVERITY_RANK.get(i['severity'], 9))
    return items[:limit]


# ══════════════════════════════════════════════════════════════════════════
# Cash-flow forecast
# ══════════════════════════════════════════════════════════════════════════

def cash_flow_forecast(*, transactions, cash, bills, today, days=FORECAST_DAYS):
    """Project the cash balance forward, with a widening confidence band.

    The projection has three parts:

    * **Known bills** land on the dates ``upcoming_bills`` computed for them.
    * **Everyday spending** continues at the trailing daily average, with the
      recurring bills stripped out so they are not counted twice.
    * **Income** is scheduled on the days of the month deposits have actually
      landed on, at the trailing monthly average — spreading a paycheck evenly
      across the month would flatten exactly the dips this chart exists to show.

    The band is a random walk: uncertainty grows with the square root of the
    horizon, not linearly, because daily variance partly cancels out over time.
    Widening it linearly would make day 45 look far less knowable than it is.
    """
    today = _as_date(today)
    horizon = [today + timedelta(days=i) for i in range(days + 1)]

    txns = [{'date': _as_date(t['date']), 'amount': float(t['amount'])} for t in transactions]
    if txns:
        span_start, span_end = min(t['date'] for t in txns), max(t['date'] for t in txns)
        span_days = max(1, (span_end - span_start).days + 1)
    else:
        span_days = 1

    # ── Everyday outflow, excluding what the bill schedule already covers ──
    bill_monthly = sum(b['amount'] for b in bills) * (_AVG_MONTH_DAYS / max(1, BILL_HORIZON_DAYS)) \
        if bills else 0.0
    gross_out = sum(-t['amount'] for t in txns if t['amount'] < 0)
    daily_out = max(0.0, _safe_div(gross_out, span_days) - _safe_div(bill_monthly, _AVG_MONTH_DAYS))

    # ── Income timing ───────────────────────────────────────────────────
    deposits = [t for t in txns if t['amount'] > 0]
    total_income = sum(t['amount'] for t in deposits)
    monthly_income = _safe_div(total_income, span_days) * _AVG_MONTH_DAYS
    # The days of the month money actually arrives on. Anything smaller than a
    # quarter of the largest deposit is noise (refunds, interest), not a paycheck.
    if deposits:
        biggest = max(t['amount'] for t in deposits)
        pay_days = sorted({t['date'].day for t in deposits if t['amount'] >= biggest * 0.25})
    else:
        pay_days = []
    per_pay = _safe_div(monthly_income, len(pay_days)) if pay_days else 0.0

    # ── Daily volatility, for the band ──────────────────────────────────
    by_day = {}
    for t in txns:
        by_day[t['date']] = by_day.get(t['date'], 0.0) + t['amount']
    daily_values = list(by_day.values())
    if len(daily_values) > 1:
        mean = sum(daily_values) / len(daily_values)
        variance = sum((v - mean) ** 2 for v in daily_values) / (len(daily_values) - 1)
        sigma = math.sqrt(variance)
    else:
        sigma = 0.0

    bills_by_date = {}
    for b in bills:
        bills_by_date[_as_date(b['due_date'])] = bills_by_date.get(_as_date(b['due_date']), 0.0) + b['amount']

    points = []
    balance = float(cash)
    for i, day in enumerate(horizon):
        if i > 0:
            balance -= daily_out
            balance -= bills_by_date.get(day, 0.0)
            if day.day in pay_days:
                balance += per_pay
        # 80% band (±1.28σ) — wide enough to be honest, tight enough to read.
        spread = 1.28 * sigma * math.sqrt(i)
        points.append({
            'date': day.strftime('%Y-%m-%d'),
            'balance': round(balance, 2),
            'low': round(balance - spread, 2),
            'high': round(balance + spread, 2),
        })

    projected = points[-1]['balance']
    low_point = min(points, key=lambda p: p['balance'])
    monthly_net = _safe_div(total_income - gross_out, span_days) * _AVG_MONTH_DAYS
    burn = _safe_div(gross_out, span_days) * _AVG_MONTH_DAYS

    return {
        'points': points,
        'days': days,
        'starting_cash': round(float(cash), 2),
        'projected_balance': round(projected, 2),
        'change': round(projected - float(cash), 2),
        'low_point': low_point,
        'goes_negative': low_point['balance'] < 0,
        'expected_monthly_income': round(monthly_income, 2),
        'expected_monthly_spend': round(burn, 2),
        'expected_monthly_savings': round(monthly_net, 2),
        'runway_months': round(_safe_div(float(cash), burn, 0.0), 1) if burn > 0 else None,
        'bills_total': round(sum(b['amount'] for b in bills), 2),
        'confident': sigma > 0 and len(daily_values) >= 14,
    }


# ══════════════════════════════════════════════════════════════════════════
# Personalization
# ══════════════════════════════════════════════════════════════════════════

def _salutation(now):
    hour = now.hour
    if hour < 5:
        return 'Working late'
    if hour < 12:
        return 'Good morning'
    if hour < 17:
        return 'Good afternoon'
    if hour < 22:
        return 'Good evening'
    return 'Good evening'


def personalize(*, now, name, transactions, income, outgo, prev_outgo,
                net_worth, prev_net_worth=None, health=None):
    """Greeting plus the one contextual line that earns its place at the top.

    Only one context line is chosen, from the most specific signal available.
    Showing several at once turns a greeting into another list to read, which
    is the opposite of what the top of the page is for.
    """
    now = now if isinstance(now, datetime) else datetime.combine(_as_date(now), datetime.min.time())
    today = now.date()
    tag = None

    recent = [{'date': _as_date(t['date']), 'amount': float(t['amount']),
               'description': t.get('description', '')}
              for t in transactions]

    # Payday — a large deposit in the last three days.
    deposits = [t for t in recent if t['amount'] > 0]
    if deposits:
        biggest = max(t['amount'] for t in deposits)
        fresh = [t for t in deposits
                 if (today - t['date']).days <= 3 and t['amount'] >= biggest * 0.5]
        if fresh:
            top = max(fresh, key=lambda t: t['amount'])
            tag = {'kind': 'payday', 'icon': 'sparkle',
                   'text': f'{_money(top["amount"])} landed — payday. '
                           f'A good moment to move some of it out of reach.'}

    # Month-end recap.
    if tag is None:
        tomorrow = today + timedelta(days=1)
        if tomorrow.month != today.month:
            tag = {'kind': 'recap', 'icon': 'calendar',
                   'text': f'Last day of {today.strftime("%B")} — '
                           f'{_money(outgo)} spent, {_money(income - outgo)} kept.'}

    # Weekend spending.
    if tag is None and today.weekday() >= 5:
        weekend_start = today - timedelta(days=today.weekday() - 4)
        spent = sum(-t['amount'] for t in recent
                    if t['amount'] < 0 and t['date'] >= weekend_start)
        if spent > 0:
            tag = {'kind': 'weekend', 'icon': 'calendar',
                   'text': f'{_money(spent)} spent so far this weekend.'}

    # Net-worth milestone — the next round number worth naming.
    if tag is None and net_worth > 0:
        step = 10000 if net_worth >= 10000 else 1000
        milestone = math.floor(net_worth / step) * step
        if prev_net_worth is not None and prev_net_worth < milestone <= net_worth:
            tag = {'kind': 'milestone', 'icon': 'sparkle',
                   'text': f'Net worth just passed {_money(milestone)}.'}
        elif milestone > 0 and (net_worth - milestone) < step * 0.12:
            nxt = milestone + step
            tag = {'kind': 'milestone', 'icon': 'target',
                   'text': f'{_money(nxt - net_worth)} away from {_money(nxt)} net worth.'}

    # Spending trend.
    if tag is None:
        change = _pct_change(outgo, prev_outgo)
        if change is not None and abs(change) >= 8:
            direction = 'down' if change < 0 else 'up'
            tag = {'kind': 'trend', 'icon': 'trend-' + direction,
                   'text': f'Spending is {direction} {abs(change):.0f}% versus the prior period.'}

    # Fall back to the health read.
    if tag is None and health:
        tag = {'kind': 'health', 'icon': 'check',
               'text': f'Your finances are {_band_phrase(health["band"])}.'}

    return {
        'salutation': _salutation(now),
        'name': (name or '').split()[0] if name else None,
        # Built by hand rather than with %-d, which is glibc-only and raises
        # on Windows.
        'date_label': f'{now:%A, %B} {now.day}',
        'tag': tag,
    }
