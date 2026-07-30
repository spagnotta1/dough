"""The dashboard: the one page every session starts on."""

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, session
from sqlalchemy import func

from dough.services.networth import compute_net_worth, portfolio_snapshot
from dough.services.recurring_service import detect_recurring_full

import dashboard_intel
from models import AppUser, Budget, Transaction, db

bp = Blueprint('core', __name__)

@bp.route('/')
def dashboard():
    start_date_str = request.args.get('start_date') or session.get('start_date')
    end_date_str = request.args.get('end_date') or session.get('end_date')
    account_filter = request.args.get('account') or session.get('account', 'both')

    if not start_date_str or not end_date_str:
        end_date = datetime.now()
        start_date = end_date.replace(day=1)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    else:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

    session['start_date'] = start_date_str
    session['end_date'] = end_date_str
    session['account'] = account_filter

    # Cascading category filter (multiselect via repeated ?category=
    # params): URL-only (not sticky) so leaving the dashboard resets it.
    category_filter = [c for c in request.args.getlist('category') if c]

    query = Transaction.query.filter(Transaction.date.between(start_date, end_date))
    if account_filter != 'both':
        query = query.filter(Transaction.account_name == account_filter)
    all_transactions = query.all()
    transactions = ([t for t in all_transactions if t.category in category_filter]
                    if category_filter else all_transactions)

    def is_transfer(t):
        return t.category.lower() in ['transfer', 'transfers']

    total_income = sum(
        float(t.amount) for t in transactions
        if t.amount > 0 and (account_filter != 'both' or not is_transfer(t))
    )
    total_outgo = abs(sum(
        float(t.amount) for t in transactions
        if t.amount < 0 and (account_filter != 'both' or not is_transfer(t))
    ))
    net_cashflow = total_income - total_outgo

    def _category_breakdown(txn_list):
        stats = {}
        for t in txn_list:
            if account_filter == 'both' and is_transfer(t):
                continue
            if t.category not in stats:
                stats[t.category] = {'inbound': 0, 'outbound': 0}
            if t.amount > 0:
                stats[t.category]['inbound'] += float(t.amount)
            else:
                stats[t.category]['outbound'] += abs(float(t.amount))
        return stats

    category_stats = _category_breakdown(transactions)
    # The breakdown grid always lists every category so the user can
    # click between them even while one is selected.
    all_category_stats = (category_stats if not category_filter
                          else _category_breakdown(all_transactions))

    # Monthly outgo trend
    monthly_outgo_q = db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(func.abs(Transaction.amount)).label('total')
    ).filter(Transaction.date.between(start_date, end_date), Transaction.amount < 0)
    if account_filter != 'both':
        monthly_outgo_q = monthly_outgo_q.filter(Transaction.account_name == account_filter)
    else:
        monthly_outgo_q = monthly_outgo_q.filter(~func.lower(Transaction.category).in_(['transfer', 'transfers']))
    if category_filter:
        monthly_outgo_q = monthly_outgo_q.filter(Transaction.category.in_(category_filter))
    monthly_outgo_data = [{'month': r.month, 'total': float(r.total)}
                           for r in monthly_outgo_q.group_by('month').order_by('month').all()]

    # Monthly income trend
    monthly_income_q = db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(Transaction.amount).label('total')
    ).filter(Transaction.date.between(start_date, end_date), Transaction.amount > 0)
    if account_filter != 'both':
        monthly_income_q = monthly_income_q.filter(Transaction.account_name == account_filter)
    else:
        monthly_income_q = monthly_income_q.filter(~func.lower(Transaction.category).in_(['transfer', 'transfers']))
    if category_filter:
        monthly_income_q = monthly_income_q.filter(Transaction.category.in_(category_filter))
    monthly_income_data = [{'month': r.month, 'total': float(r.total)}
                            for r in monthly_income_q.group_by('month').order_by('month').all()]

    # --- Balance history (running sum ordered by date) ---
    bal_q = Transaction.query.filter(Transaction.date.between(start_date, end_date))
    if account_filter != 'both':
        bal_q = bal_q.filter(Transaction.account_name == account_filter)
    if category_filter:
        bal_q = bal_q.filter(Transaction.category.in_(category_filter))
    bal_txns = bal_q.order_by(Transaction.date.asc()).all()
    running = 0.0
    balance_history = []
    for t in bal_txns:
        running += float(t.amount)
        balance_history.append({'date': t.date.strftime('%Y-%m-%d'), 'balance': round(running, 2)})

    # --- Month-over-Month comparison ---
    period_days = (end_date - start_date).days + 1
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)
    prev_q = Transaction.query.filter(Transaction.date.between(prev_start, prev_end))
    if account_filter != 'both':
        prev_q = prev_q.filter(Transaction.account_name == account_filter)
    if category_filter:
        prev_q = prev_q.filter(Transaction.category.in_(category_filter))
    prev_txns = prev_q.all()
    prev_income = sum(float(t.amount) for t in prev_txns if t.amount > 0 and (account_filter != 'both' or not is_transfer(t)))
    prev_outgo = abs(sum(float(t.amount) for t in prev_txns if t.amount < 0 and (account_filter != 'both' or not is_transfer(t))))
    prev_net = prev_income - prev_outgo
    prev_category_stats = {}
    for t in prev_txns:
        if account_filter == 'both' and is_transfer(t):
            continue
        if t.category not in prev_category_stats:
            prev_category_stats[t.category] = {'inbound': 0, 'outbound': 0}
        if t.amount > 0:
            prev_category_stats[t.category]['inbound'] += float(t.amount)
        else:
            prev_category_stats[t.category]['outbound'] += abs(float(t.amount))

    # --- Budget data ---
    budgets = Budget.query.all()
    budget_map = {}
    for b in budgets:
        if b.account_name == 'both' or b.account_name == account_filter:
            budget_map[b.category] = float(b.monthly_limit)
    # When a category is selected, the budget chart/insights cascade to it;
    # the breakdown grid keeps the full map so every row shows its budget.
    chart_budget_map = ({c: l for c, l in budget_map.items() if c in category_filter}
                        if category_filter else budget_map)

    # Normalize spend to a monthly average so budget limits are always monthly comparisons.
    # For periods < 1 month we compare raw spend vs limit (no extrapolation).
    period_months = max(1.0, period_days / 30.44)

    # --- Budget alerts (B2) ---
    budget_alerts = []
    for cat, limit in budget_map.items():
        cat_stats = category_stats.get(cat, {})
        spent = max(0, cat_stats.get('outbound', 0) - cat_stats.get('inbound', 0))
        monthly_avg = spent / period_months
        if limit > 0 and spent > 0:
            pct = (monthly_avg / limit) * 100
            if pct >= 100:
                budget_alerts.append({'category': cat, 'pct': round(pct), 'level': 'over',
                                      'spent': spent, 'monthly_avg': round(monthly_avg, 2), 'limit': limit})
            elif pct >= 80:
                budget_alerts.append({'category': cat, 'pct': round(pct), 'level': 'warning',
                                      'spent': spent, 'monthly_avg': round(monthly_avg, 2), 'limit': limit})
    budget_alerts.sort(key=lambda x: x['pct'], reverse=True)

    # --- Spending insights (D1) ---
    insights = []
    for cat, stats in sorted(category_stats.items(), key=lambda x: x[1]['outbound'], reverse=True):
        if stats['outbound'] > 0:
            prev = prev_category_stats.get(cat, {}).get('outbound', 0)
            if prev > 0:
                delta_pct = ((stats['outbound'] - prev) / prev) * 100
                if abs(delta_pct) >= 20:
                    direction = 'up' if delta_pct > 0 else 'down'
                    positive = delta_pct < 0
                    insights.append({
                        'text': f"{cat} spending {direction} {abs(delta_pct):.0f}% vs. prior period "
                                f"(${stats['outbound']:.0f} vs. ${prev:.0f})",
                        'positive': positive,
                    })
    if insights:
        insights = insights[:5]
    over_budget = [a for a in budget_alerts if a['level'] == 'over']
    under_budget = [c for c, lim in chart_budget_map.items()
                    if max(0, category_stats.get(c, {}).get('outbound', 0) - category_stats.get(c, {}).get('inbound', 0)) / period_months < lim * 0.5]
    if over_budget:
        count = len(over_budget)
        insights.insert(0, {'text': f"{count} budget{'s' if count != 1 else ''} "
                                    f"exceeded this period.", 'positive': False})
    if under_budget:
        insights.append({'text': f"Well within budget in: {', '.join(under_budget[:3])}.", 'positive': True})

    # --- Category monthly trend (D2) — same date range as the active filter ---
    trend_start = start_date
    trend_end = end_date
    top_spend_cats = sorted(
        [(c, s['outbound']) for c, s in category_stats.items() if s['outbound'] > 0],
        key=lambda x: x[1], reverse=True
    )[:6]
    top_cat_names = [c for c, _ in top_spend_cats]
    trend_q = db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        Transaction.category,
        func.sum(func.abs(Transaction.amount)).label('total')
    ).filter(
        Transaction.date.between(trend_start, trend_end),
        Transaction.amount < 0,
    )
    if account_filter != 'both':
        trend_q = trend_q.filter(Transaction.account_name == account_filter)
    if top_cat_names:
        trend_q = trend_q.filter(Transaction.category.in_(top_cat_names))
    trend_rows = trend_q.group_by('month', Transaction.category).order_by('month').all()
    trend_months = sorted(set(r.month for r in trend_rows))
    category_trend = {'months': trend_months, 'series': {}}
    for cat in top_cat_names:
        category_trend['series'][cat] = [0.0] * len(trend_months)
    for r in trend_rows:
        if r.month in trend_months and r.category in category_trend['series']:
            idx = trend_months.index(r.month)
            category_trend['series'][r.category][idx] = round(float(r.total), 2)

    filter_label = f"{start_date_str} – {end_date_str}"

    def _window_label(a, b):
        """A window as a person would say it, not as two ISO dates."""
        if a.year == b.year and a.month == b.month:
            if a.day == 1 and b.day >= 28:
                return f'{a:%B %Y}'
            return f'{a:%b} {a.day}–{b.day}, {b.year}'
        if a.year == b.year:
            return f'{a:%b} {a.day} – {b:%b} {b.day}, {b.year}'
        return f'{a:%b} {a.day}, {a.year} – {b:%b} {b.day}, {b.year}'

    period_label = _window_label(start_date, end_date)
    compare_label = _window_label(prev_start, prev_end)

    nw = compute_net_worth()

    # ── Layer 2 & 3 intelligence ────────────────────────────────────
    # What the numbers mean, rather than what they are. Kept in
    # dashboard_intel so the reasoning is testable apart from the route.
    today = datetime.now()
    recurring_full = detect_recurring_full()
    portfolio = portfolio_snapshot()

    upcoming = dashboard_intel.upcoming_bills(recurring_full, today)

    # The forecast reads recent reality, not the selected filter window:
    # projecting next month's cash from a "last year" filter would be
    # nonsense. 90 days is enough to see a monthly cycle repeat.
    forecast_txns = [{'date': t.date, 'amount': float(t.amount),
                      'description': t.description}
                     for t in Transaction.query
                     .filter(Transaction.date >= today - timedelta(days=90))
                     .order_by(Transaction.date.asc()).all()]
    forecast = dashboard_intel.cash_flow_forecast(
        transactions=forecast_txns, cash=nw['cash'], bills=upcoming, today=today)

    hikes = dashboard_intel.subscription_hikes(recurring_full, forecast_txns)

    anomaly_count = (Transaction.query
                     .filter(Transaction.anomaly_score == -1.0,
                             Transaction.anomaly_reviewed.is_(False)).count())

    health = dashboard_intel.health_score(
        income=total_income, outgo=total_outgo,
        runway_months=forecast['runway_months'],
        budget_map=budget_map, category_stats=category_stats,
        period_months=period_months, prev_outgo=prev_outgo)

    attention = dashboard_intel.attention_items(
        budget_alerts=budget_alerts,
        category_stats=category_stats, prev_category_stats=prev_category_stats,
        income=total_income, outgo=total_outgo,
        prev_income=prev_income, prev_outgo=prev_outgo,
        cash=nw['cash'], period_months=period_months,
        runway_months=forecast['runway_months'],
        bills=upcoming, hikes=hikes, anomaly_count=anomaly_count,
        portfolio=portfolio)

    _uid = session.get('user_id')
    _user = db.session.get(AppUser, _uid) if _uid else None
    greeting = dashboard_intel.personalize(
        now=today, name=_user.username if _user else None,
        transactions=forecast_txns,
        income=total_income, outgo=total_outgo, prev_outgo=prev_outgo,
        net_worth=nw['net_worth'], health=health)

    # Every category the account has ever used, ranked by lifetime volume.
    # The client walks this list to assign palette slots, so the categories
    # that actually reach a chart are the ones holding distinct hues and
    # the long tail shares the neutral.
    #
    # Ranked on the WHOLE history, never the filtered window: that is what
    # lets the mapping stay fixed while a filter changes which categories
    # are on screen. Alphabetical ordering was worse — it handed the eight
    # hues to whichever categories started with early letters, which put
    # three of the six charted series on the same gray.
    all_categories = [row[0] for row in db.session.query(
        Transaction.category, func.sum(func.abs(Transaction.amount)).label('vol')
    ).filter(Transaction.category.isnot(None))
     .group_by(Transaction.category).order_by(func.sum(func.abs(Transaction.amount)).desc()).all()]

    return render_template('dashboard.html',
                           all_categories=all_categories,
                           period_label=period_label,
                           compare_label=compare_label,
                           has_data=bool(transactions),
                           health=health,
                           attention=attention,
                           forecast=forecast,
                           upcoming_bills=upcoming,
                           greeting=greeting,
                           portfolio=portfolio,
                           anomaly_count=anomaly_count,
                           savings_rate=round((net_cashflow / total_income * 100), 1) if total_income > 0 else 0.0,
                           total_income=total_income,
                           total_outgo=total_outgo,
                           net_cashflow=net_cashflow,
                           category_stats=category_stats,
                           all_category_stats=all_category_stats,
                           category_filter=category_filter,
                           monthly_outgo=monthly_outgo_data,
                           monthly_income=monthly_income_data,
                           balance_history=balance_history,
                           prev_income=prev_income,
                           prev_outgo=prev_outgo,
                           prev_net=prev_net,
                           prev_category_stats=prev_category_stats,
                           prev_start=prev_start.strftime('%Y-%m-%d'),
                           prev_end=prev_end.strftime('%Y-%m-%d'),
                           budget_map=budget_map,
                           chart_budget_map=chart_budget_map,
                           budget_alerts=budget_alerts,
                           period_months=period_months,
                           insights=insights,
                           category_trend=category_trend,
                           filter_label=filter_label,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           account_filter=account_filter,
                           nw=nw)
