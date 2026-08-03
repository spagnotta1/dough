"""The snapshots Dough reasons over.

Three contexts, three sizes, one rule: **the model only ever sees numbers the
page already derived.** That is what makes it structurally impossible for Dough
to narrate a figure the reader cannot check.

- `build_finance_context` — the chat assistant's context. The widest of the
  three; with `detail=True` it carries line items, month-by-category history,
  top merchants and open anomalies.
- `copilot_context` — the dashboard briefing's context. Deliberately smaller;
  the briefing needs the shape of the period, not every row, and a tight context
  is what keeps the card appearing in about a second.
- `wealth_context` — the Investments briefing's context, derived entirely from
  `networth.wealth_snapshot()`, i.e. the exact dictionary the page rendered.

The `CHAT_*` constants below travel with this module rather than staying in
`app.py` because they size *this* context; the request-shaped ones
(`CHAT_HISTORY_LIMIT`, `CHAT_MAX_TOKENS`) stay behind and belong to `dough/ai/`
in Phase 4.

Allowed:   models, `dashboard_intel`, sibling services (`networth`,
           `recurring_service`), SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
Exception: `copilot_context` reaches `recurring_service.detect_recurring_full`,
           which memoizes on `flask.g`. That is the only Flask context this
           module touches, and only transitively.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import func

import dashboard_intel
from dough.services.analytics import as_date
from dough.services.networth import (compute_net_worth, portfolio_snapshot,
                                     wealth_snapshot)
from dough.services.recurring_service import (detect_recurring_full,
                                              detect_recurring_summary)
from models import Budget, Holding, Transaction, db

# How much line-item detail rides along in the chat assistant's context window.
CHAT_RECENT_TXN_LIMIT     = 150
CHAT_TOP_MERCHANT_LIMIT   = 30
CHAT_ANOMALY_LIMIT        = 25
CHAT_TREND_MONTHS         = 24     # months of month-by-category history


def months_ago(n):
    """First day of the month `n` months before today."""
    today = date.today()
    year, month = today.year, today.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def build_finance_context(months=6, detail=False):
    """Assemble a full financial snapshot for Claude — spending, net worth, and complete holdings detail.

    When ``detail`` is True the snapshot also carries line-item data the chat
    assistant needs to answer questions about specific purchases: recent
    transactions, top merchants, and unreviewed anomalies.
    """
    # `.date()`: `Transaction.date` is a `Date` column, and a `datetime` bound
    # against it is compared as the string '2026-08-01 00:00:00.000000' by
    # SQLite, which excludes rows stored as '2026-08-01' — the cutoff day
    # itself. See `services/transactions.build_transaction_query`.
    cutoff = (datetime.now() - timedelta(days=months * 30)).date()

    # --- Spending by category (last N months) ---
    rows = (db.session.query(Transaction.category, func.sum(Transaction.amount))
            .filter(Transaction.date >= cutoff, Transaction.amount < 0)
            .group_by(Transaction.category)
            .order_by(func.sum(Transaction.amount))
            .all())
    spending = {cat: round(abs(float(amt)), 2) for cat, amt in rows}

    # --- Income (last N months) ---
    income = float(db.session.query(func.sum(Transaction.amount))
                   .filter(Transaction.date >= cutoff, Transaction.amount > 0)
                   .scalar() or 0)

    # --- Monthly income/expense trend (6 months) ---
    monthly_rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(Transaction.amount).label('net'),
        func.sum(func.abs(Transaction.amount)).label('gross'),
    ).filter(Transaction.date >= cutoff)
     .group_by('month').order_by('month').all())
    monthly_trend = [{'month': r.month, 'net': round(float(r.net), 2)} for r in monthly_rows]

    # --- Budgets ---
    budget_status = [{'category': b.category, 'monthly_limit': float(b.monthly_limit)}
                     for b in Budget.query.all()]

    # --- Recurring bills & subscriptions ---
    recurring_items = detect_recurring_summary()

    # --- Net worth ---
    nw = compute_net_worth()

    # --- Full holdings list with individual positions ---
    all_holdings = Holding.query.order_by(Holding.asset_class, Holding.ticker).all()
    total_invested = float(db.session.query(func.sum(Holding.current_value)).scalar() or 0)
    holdings_list = []
    for h in all_holdings:
        val = float(h.current_value)
        holdings_list.append({
            'ticker': h.ticker,
            'name': h.name,
            'shares': float(h.shares),
            'current_value': round(val, 2),
            'asset_class': h.asset_class,
            'account': h.account_name,
            'pct_of_portfolio': round(val / total_invested * 100, 1) if total_invested > 0 else 0,
        })

    # --- Asset class allocation (investments only, with % breakdown) ---
    asset_alloc = {}
    for h in all_holdings:
        ac = h.asset_class
        val = float(h.current_value)
        asset_alloc[ac] = asset_alloc.get(ac, 0) + val
    asset_alloc_detail = {
        ac: {
            'value': round(v, 2),
            'pct_of_investments': round(v / total_invested * 100, 1) if total_invested > 0 else 0,
        }
        for ac, v in sorted(asset_alloc.items(), key=lambda x: -x[1])
    }

    # --- Cash vs investments ratio ---
    total_nw = nw['net_worth']
    allocation_summary = {
        'cash_checking': nw['checking'],
        'cash_savings': nw['savings'],
        'total_cash': nw['cash'],
        'total_investments': round(total_invested, 2),
        'cash_pct': round(nw['cash'] / total_nw * 100, 1) if total_nw > 0 else 0,
        'investments_pct': round(total_invested / total_nw * 100, 1) if total_nw > 0 else 0,
    }

    ctx = {
        'data_period_months': months,
        'net_worth': nw,
        'allocation_summary': allocation_summary,
        'holdings': holdings_list,
        'asset_class_allocation': asset_alloc_detail,
        'total_income_period': round(income, 2),
        'spending_by_category': spending,
        'monthly_cashflow_trend': monthly_trend,
        'budgets': budget_status,
        'recurring_bills': recurring_items['bills'],
        'recurring_subscriptions': recurring_items['subscriptions'],
    }

    if not detail:
        return ctx

    # --- Line-item detail: lets the assistant answer "what did I spend at X?" ---
    ctx['today'] = date.today().isoformat()

    # What actually exists, stated plainly. Without this the assistant infers
    # its coverage from the newest slice below and wrongly reports that older
    # months are missing.
    first_txn, last_txn, txn_count = db.session.query(
        func.min(Transaction.date), func.max(Transaction.date),
        func.count(Transaction.id)).one()
    account_names = sorted(
        n for (n,) in db.session.query(Transaction.account_name).distinct().all() if n)
    ctx['transaction_coverage'] = {
        'first_transaction': first_txn.isoformat() if first_txn else None,
        'last_transaction': last_txn.isoformat() if last_txn else None,
        'total_transactions': int(txn_count or 0),
        'accounts': account_names,
        'note': ('monthly_spending_by_category, monthly_spending_by_account_category, '
                 'monthly_income and monthly_income_by_account all cover this whole '
                 'range. Anything that can be asked about the total can also be asked '
                 'about one account. recent_transactions is only the newest slice, for '
                 'questions about individual purchases.'),
    }

    # Month-by-category spend over the full history — the series behind any
    # "how has X trended" question. Aggregated in SQL so the window can be
    # long without the token cost of the underlying rows.
    trend_cutoff = months_ago(CHAT_TREND_MONTHS)
    by_month = {}
    cat_rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        Transaction.category,
        func.sum(Transaction.amount).label('total'))
        .filter(Transaction.date >= trend_cutoff, Transaction.amount < 0)
        .group_by('month', Transaction.category).all())
    for r in cat_rows:
        amount = round(abs(float(r.total)), 2)
        if amount:
            by_month.setdefault(r.month, {})[r.category] = amount
    ctx['monthly_spending_by_category'] = dict(sorted(by_month.items()))

    # The same spend split by account. Both series ship: the aggregate above
    # is what a "total" question should read directly rather than re-adding,
    # and this one answers "checking only" / "which account paid for it",
    # which the aggregate silently cannot. Categories barely overlap between
    # accounts, so the split costs a handful of extra numbers, not double.
    acct_cat_rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        Transaction.account_name,
        Transaction.category,
        func.sum(Transaction.amount).label('total'))
        .filter(Transaction.date >= trend_cutoff, Transaction.amount < 0)
        .group_by('month', Transaction.account_name, Transaction.category).all())
    by_month_acct = {}
    for r in acct_cat_rows:
        amount = round(abs(float(r.total)), 2)
        if amount:
            by_month_acct.setdefault(r.month, {}).setdefault(
                r.account_name, {})[r.category] = amount
    ctx['monthly_spending_by_account_category'] = {
        month: {acct: dict(sorted(cats.items(), key=lambda kv: -kv[1]))
                for acct, cats in sorted(accts.items())}
        for month, accts in sorted(by_month_acct.items())
    }

    # Ground truth for any chart that folds the small categories into an
    # "Other" series. Adding a dozen leftovers by hand is exactly the step
    # the assistant gets wrong, so the residual is a subtraction from a
    # number that is already here. Both conventions are given because
    # transfers count as an outflow for one account but cancel across two.
    totals = {}
    for r in acct_cat_rows:
        amount = abs(float(r.total))
        if not amount:
            continue
        is_transfer = (r.category or '').strip().lower() in ('transfer', 'transfers')
        for group in (r.account_name, 'all_accounts'):
            bucket = totals.setdefault(r.month, {}).setdefault(
                group, {'total': 0.0, 'excluding_transfers': 0.0})
            bucket['total'] += amount
            if not is_transfer:
                bucket['excluding_transfers'] += amount
    ctx['monthly_spending_totals'] = {
        month: {group: {k: round(v, 2) for k, v in sums.items()}
                for group, sums in sorted(groups.items())}
        for month, groups in sorted(totals.items())
    }

    income_rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(Transaction.amount).label('total'))
        .filter(Transaction.date >= trend_cutoff, Transaction.amount > 0)
        .group_by('month').all())
    ctx['monthly_income'] = {r.month: round(float(r.total), 2)
                             for r in sorted(income_rows, key=lambda r: r.month)}

    income_acct_rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        Transaction.account_name,
        func.sum(Transaction.amount).label('total'))
        .filter(Transaction.date >= trend_cutoff, Transaction.amount > 0)
        .group_by('month', Transaction.account_name).all())
    by_month_income = {}
    for r in income_acct_rows:
        by_month_income.setdefault(r.month, {})[r.account_name] = round(float(r.total), 2)
    ctx['monthly_income_by_account'] = {
        month: dict(sorted(accts.items())) for month, accts in sorted(by_month_income.items())
    }

    recent = (Transaction.query
              .order_by(Transaction.date.desc(), Transaction.id.desc())
              .limit(CHAT_RECENT_TXN_LIMIT).all())
    ctx['recent_transactions'] = [{
        'date': t.date.isoformat(),
        'description': t.description,
        'amount': round(float(t.amount), 2),
        'category': t.category,
        'account': t.account_name,
    } for t in recent]

    merchant_rows = (db.session.query(
        Transaction.description,
        func.sum(Transaction.amount).label('total'),
        func.count(Transaction.id).label('n'))
        .filter(Transaction.date >= cutoff, Transaction.amount < 0)
        .group_by(Transaction.description)
        .order_by(func.sum(Transaction.amount))
        .limit(CHAT_TOP_MERCHANT_LIMIT).all())
    ctx['top_merchants_in_period'] = [{
        'description': r.description,
        'total_spent': round(abs(float(r.total)), 2),
        'transactions': int(r.n),
    } for r in merchant_rows]

    flagged = (Transaction.query
               .filter(Transaction.anomaly_score == -1.0,
                       Transaction.anomaly_reviewed == False)  # noqa: E712
               .order_by(Transaction.date.desc())
               .limit(CHAT_ANOMALY_LIMIT).all())
    ctx['open_anomalies'] = [{
        'date': t.date.isoformat(),
        'description': t.description,
        'amount': round(float(t.amount), 2),
        'category': t.category,
    } for t in flagged]

    return ctx


def copilot_context(start=None, end=None):
    """The compact snapshot the copilot reasons over.

    Deliberately smaller than the chat assistant's context: the briefing
    needs the shape of the period, not every line item, and a tight
    context is what keeps the card appearing in about a second.

    The window defaults to the current month but follows the dashboard's
    own filter when one is given. Without that the copilot would narrate
    July while the page beneath it showed March through June.
    """
    # Normalised to `date` because the callers hand this function `datetime`
    # (the API converts to one explicitly), and a `datetime` compared against
    # the `Date` column loses the first day of the window under SQLite — see
    # `services/transactions.build_transaction_query`. The briefing sits on the
    # dashboard, so a window one day short here contradicts the page around it.
    end = as_date(end or datetime.now())
    start = as_date(start) if start else end.replace(day=1)
    txns = Transaction.query.filter(Transaction.date.between(start, end)).all()

    def is_transfer(t):
        return t.category.lower() in ('transfer', 'transfers')

    spend = {}
    income = 0.0
    for t in txns:
        if is_transfer(t):
            continue
        if t.amount > 0:
            income += float(t.amount)
        else:
            spend[t.category] = round(spend.get(t.category, 0.0) + abs(float(t.amount)), 2)

    # The comparison window is the same length as the selected one, so a
    # four-month filter compares against the four months before it.
    span = max(1, (end - start).days + 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev_txns = Transaction.query.filter(Transaction.date.between(prev_start, prev_end)).all()
    prev_spend = {}
    prev_income = 0.0
    for t in prev_txns:
        if is_transfer(t):
            continue
        if t.amount > 0:
            prev_income += float(t.amount)
        else:
            prev_spend[t.category] = round(prev_spend.get(t.category, 0.0) + abs(float(t.amount)), 2)

    nw = compute_net_worth()
    recurring_full = detect_recurring_full()
    upcoming = dashboard_intel.upcoming_bills(recurring_full, end)
    budget_map = {b.category: float(b.monthly_limit) for b in Budget.query.all()}

    def window_label(a, b):
        """"March 2026" for a single month, "Mar 1 – Jun 30, 2026" otherwise."""
        if a.year == b.year and a.month == b.month:
            return f'{a:%B %Y}'
        return f'{a:%b} {a.day} – {b:%b} {b.day}, {b.year}'

    return {
        'today': datetime.now().strftime('%Y-%m-%d'),
        'selected_period': {'label': window_label(start, end),
                            'start': start.strftime('%Y-%m-%d'),
                            'end': end.strftime('%Y-%m-%d'),
                            'income': round(income, 2),
                            'spending_by_category': spend,
                            'total_spending': round(sum(spend.values()), 2)},
        'previous_period': {'label': window_label(prev_start, prev_end),
                            'start': prev_start.strftime('%Y-%m-%d'),
                            'end': prev_end.strftime('%Y-%m-%d'),
                            'income': round(prev_income, 2),
                            'spending_by_category': prev_spend,
                            'total_spending': round(sum(prev_spend.values()), 2)},
        'net_worth': nw,
        'portfolio': portfolio_snapshot(),
        'monthly_budgets': budget_map,
        'upcoming_bills': upcoming[:10],
        'subscriptions': [{'description': s['description'],
                           'monthly_amount': abs(s['monthly_amount'])}
                          for s in recurring_full.get('subscriptions', [])[:15]],
    }


def wealth_context():
    snap = wealth_snapshot()
    positions = snap['positions']

    def bucket(alloc, limit=8):
        return [{'label': b['label'], 'value': b['value'], 'pct': b['pct']}
                for b in alloc['buckets'][:limit]]

    return {
        'today': date.today().strftime('%Y-%m-%d'),
        'net_worth': snap['nw'],
        'portfolio_value': snap['nw']['investments'],
        'performance': snap['performance'],
        'annualized_return_pct': snap['annualized_return'],
        'volatility': snap['volatility'],
        'max_drawdown': snap['drawdown'],
        'holdings': [
            {'ticker': p['ticker'], 'name': p['name'], 'value': p['value'],
             'weight_pct': p['weight'], 'asset_class': p['asset_class'],
             'sector': p['sector'], 'region': p['region'],
             'account': p['account'], 'gain': p['gain'], 'gain_pct': p['gain_pct'],
             'cost_basis': p['cost_basis'],
             'estimated_yield_pct': p['yield_pct']}
            for p in positions[:60]
        ],
        'allocation': {
            'asset_class': bucket(snap['allocation']['asset_class']),
            'sector': bucket(snap['allocation']['sector']),
            'region': bucket(snap['allocation']['region']),
            'market_cap': bucket(snap['allocation']['market_cap']),
            'account': bucket(snap['allocation']['account']),
        },
        'allocation_coverage': {
            'sector_pct': snap['allocation']['sector']['coverage'],
            'region_pct': snap['allocation']['region']['coverage'],
        },
        'concentration': snap['concentration'],
        'risk': snap['risk'],
        'diversification': snap['diversification'],
        'portfolio_health': snap['health'],
        'estimated_dividends': snap['dividends'],
        'benchmark_reference': {k: v for k, v in snap['benchmark'].items()
                                if k not in ('portfolio', 'reference')},
        'projection': {'final': snap['projection']['final'],
                       'assumptions': snap['projection']['assumptions']},
        'deterministic_insights': [
            {'severity': i['severity'], 'title': i['title'], 'detail': i['detail']}
            for i in snap['insights'][:10]
        ],
    }
