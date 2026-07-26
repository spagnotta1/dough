import io
import json
import os
import re
import time
import uuid
from dotenv import load_dotenv
load_dotenv()
from calendar import monthrange
from datetime import datetime, timedelta, date
from flask import (Flask, render_template, request, jsonify, redirect, url_for, flash,
                   session, Response, stream_with_context, g)
from flask_migrate import Migrate
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import anthropic
import pandas as pd
import numpy as np
from sqlalchemy.exc import IntegrityError
from sklearn.ensemble import IsolationForest
from sqlalchemy import func, and_, or_

from config import Config
from models import (db, AppUser, Transaction, LogEntry, AccountBalance, Budget, Holding,
                    ChatMessage, Conversation, InstitutionConnection,
                    FinancialAccount, SyncRun, RecurringDismissal,
                    PortfolioSnapshotRow)
from rules import CategoryRules
from recurring import detect_recurring, normalize_description
import dashboard_intel
import investments_intel
from finance_sync.repository import SyncRepository
from finance_sync.routes import sync_bp
from finance_sync.scheduler import init_scheduler

_insight_cache = {'text': None, 'expires': 0}
_brief_cache = {'data': None, 'expires': 0, 'key': None}
_wealth_cache = {'data': None, 'expires': 0}

# How much line-item detail rides along in the chat assistant's context window.
CHAT_RECENT_TXN_LIMIT     = 150
CHAT_TOP_MERCHANT_LIMIT   = 30
CHAT_ANOMALY_LIMIT        = 25
CHAT_TREND_MONTHS         = 24     # months of month-by-category history
CHAT_HISTORY_LIMIT        = 24     # prior messages replayed to Claude
CHAT_MAX_TOKENS           = 4096

# Charting contract. The client owns every colour decision — the model only
# supplies a shape and the numbers — so this describes the data, not the design.
CHART_INSTRUCTIONS = """Charts:
You can draw a chart by emitting a fenced block tagged `chart` containing JSON:

```chart
{"type": "bar", "title": "Spending by category, last 30 days",
 "unit": "usd", "labels": ["Groceries", "Dining", "Transport"],
 "series": [{"name": "Spent", "data": [842.10, 511.42, 220.00]}]}
```

- type: "bar" (compare categories), "grouped_bar" (the same measure for several
  groups side by side, e.g. months across the axis with one bar per category),
  "line" (trend over time), "stacked_bar" (parts of a whole over time), "donut"
  (share of a whole, max 6 slices), or "diverging_bar" (amounts above/below a
  baseline, e.g. over/under budget — use signed numbers).
- Grouping: `labels` is the axis, `series` is the legend — one entry per group,
  named by that group. So "spending by category by month" is labels = the
  months, series = one per category; "total by category" with no time dimension
  is a plain bar with labels = the categories and a single series. When a
  question names a breakdown ("by category", "by account") alongside a period,
  the period goes on the axis and the breakdown goes in the legend. Six series
  is the ceiling and is already a lot — take the top few and, if it is worth
  showing, sum the rest into one "Other" series rather than dropping to a
  flatter chart. Use "grouped_bar" to compare the groups against each other and
  "stacked_bar" when their total is also part of the point. Grouped bars need
  room — past about eight axis points they turn to hatching, so use a "line"
  with one series per category for a long run of months.
- On a diverging_bar, add "positive": "bad" when a positive number is the
  unwelcome direction (over budget, overspending — this is the default), or
  "positive": "good" when a positive number is the welcome one (surplus,
  money saved). This decides which side is drawn in the warning colour, so
  set it deliberately, and say in your sentence which way is which.
- unit: "usd", "percent", or "number". labels: max 24. series: max 6, each
  with a data array the same length as labels. Numbers only — no strings,
  no nulls, no formatting, no currency symbols.
- Always write a sentence of plain-language interpretation before or after the
  chart saying what it shows. The chart supports your answer; it is not the answer.

When NOT to chart — this matters as much as the format:
- One number, or a simple comparison of two: say it in a sentence. A one-bar
  chart is worse than the sentence.
- Three or four values the reader will want to read exactly: use a table.
- Anything you cannot fill from the data above. Never estimate a data point to
  round out a chart; a shorter honest chart beats a padded one.
Reach for a chart when shape is the point — a trend across months, a split
across many categories, or a comparison against budget. At most one or two per
reply."""


def _months_ago(n):
    """First day of the month `n` months before today."""
    today = date.today()
    year, month = today.year, today.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _md_to_html(text):
    """Convert Claude's markdown to HTML with no external dependencies."""
    if not text:
        return ''

    def esc(s):
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def fmt(s):
        s = esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        s = re.sub(r'`(.+?)`', r'<code style="background:#f3f4f6;padding:.1em .3em;border-radius:3px;font-size:.85em">\1</code>', s)
        return s

    lines = text.split('\n')
    parts = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if re.match(r'^-{3,}$', s) or re.match(r'^\*{3,}$', s):
            parts.append('<hr style="border:none;border-top:1px solid #e5e7eb;margin:.6em 0">')
            i += 1; continue
        if s.startswith('### '):
            parts.append(f'<h3 style="font-weight:600;font-size:.9rem;margin:.75em 0 .2em;color:#1f2937">{fmt(s[4:])}</h3>')
            i += 1; continue
        if s.startswith('## '):
            parts.append(f'<h2 style="font-weight:700;font-size:1rem;margin:.85em 0 .25em;color:#111827">{fmt(s[3:])}</h2>')
            i += 1; continue
        if s.startswith('# '):
            parts.append(f'<h1 style="font-weight:700;font-size:1.1rem;margin:1em 0 .3em;color:#111827">{fmt(s[2:])}</h1>')
            i += 1; continue
        if s.startswith('|'):
            tbl, thead, tbody_rows, hdr_done = [], '', [], False
            while i < len(lines) and lines[i].strip().startswith('|'):
                tbl.append(lines[i].strip()); i += 1
            for row in tbl:
                if re.match(r'^\|[\s\-\:|]+\|$', row):
                    hdr_done = True; continue
                cells = [c.strip() for c in row.split('|')[1:-1]]
                if not hdr_done:
                    thead = '<thead><tr>' + ''.join(
                        f'<th style="font-weight:600;text-align:left;padding:.35em .6em;border:1px solid #ddd6fe;background:#f3f0ff;color:#5b21b6;font-size:.78rem">{fmt(c)}</th>'
                        for c in cells) + '</tr></thead>'
                else:
                    tbody_rows.append('<tr>' + ''.join(
                        f'<td style="padding:.3em .6em;border:1px solid #e5e7eb;font-size:.8rem;vertical-align:top">{fmt(c)}</td>'
                        for c in cells) + '</tr>')
            parts.append(f'<div style="overflow-x:auto;margin:.5em 0"><table style="width:100%;border-collapse:collapse">'
                         f'{thead}<tbody>{"".join(tbody_rows)}</tbody></table></div>')
            continue
        if re.match(r'^[-*] ', s):
            items = []
            while i < len(lines) and re.match(r'^[-*] ', lines[i].strip()):
                items.append(f'<li style="margin:.2em 0">{fmt(lines[i].strip()[2:])}</li>'); i += 1
            parts.append(f'<ul style="margin:.35em 0;padding-left:1.4em;list-style:disc">{"".join(items)}</ul>')
            continue
        if re.match(r'^\d+\. ', s):
            items = []
            while i < len(lines) and re.match(r'^\d+\. ', lines[i].strip()):
                item_text = re.sub(r'^\d+\. ', '', lines[i].strip())
                items.append(f'<li style="margin:.2em 0">{fmt(item_text)}</li>'); i += 1
            parts.append(f'<ol style="margin:.35em 0;padding-left:1.4em;list-style:decimal">{"".join(items)}</ol>')
            continue
        parts.append(f'<p style="margin:.3em 0;line-height:1.6">{fmt(s)}</p>')
        i += 1
    return ''.join(parts)

def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    db.init_app(app)
    migrate = Migrate(app, db)
    category_rules = CategoryRules()

    @app.template_filter('money')
    def _money_filter(value, decimals=0):
        """Format a number the way people write money: -$341, not $-341.

        Every currency figure in a template should go through this. Doing it
        inline with format() put the minus sign in the wrong place wherever a
        value could go negative, which on a finance dashboard is most of them.
        """
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return value
        sign = '-' if amount < 0 else ''
        return f'{sign}${abs(amount):,.{decimals}f}'

    @app.context_processor
    def _inject_current_user():
        uid = session.get('user_id')
        user = AppUser.query.get(uid) if uid else None
        return {'current_username': user.username if user else None}

    # ---------------------------------------------------------------------------
    # Login — a single owner account with a hashed password, created on first
    # run via /setup. Always on outside the test suite: this app fronts real
    # financial data, from the PC browser and the phone WebView alike.
    # ---------------------------------------------------------------------------
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    app.config['SESSION_COOKIE_SECURE'] = (
        os.environ.get('APP_HTTPS', '').lower() in ('1', 'true', 'yes'))

    auth_enabled = app.config.get('AUTH_ENABLED', not app.config.get('TESTING'))
    if auth_enabled:
        # Failed-attempt throttle: after 5 wrong passwords from one address,
        # login is refused for 15 minutes (in-memory; resets on restart).
        _failed_logins = {}

        def _throttled(ip):
            now = time.time()
            _failed_logins[ip] = [t for t in _failed_logins.get(ip, []) if now - t < 900]
            return len(_failed_logins[ip]) >= 5

        @app.before_request
        def _require_login():
            if session.get('user_id') or request.endpoint in ('login', 'setup', 'static'):
                return None
            if request.path.startswith('/api/'):
                return jsonify({'error': 'authentication required'}), 401
            return redirect(url_for('login', next=request.path))

        def _sign_in(user):
            session.clear()
            session['user_id'] = user.id
            session.permanent = True

        def _safe_next():
            nxt = request.args.get('next', '/')
            if not nxt.startswith('/') or nxt.startswith('//'):
                nxt = '/'
            return nxt

        @app.route('/setup', methods=['GET', 'POST'])
        def setup():
            if AppUser.query.first():
                return redirect(url_for('login'))
            error = None
            if request.method == 'POST':
                username = request.form.get('username', '').strip()
                password = request.form.get('password', '')
                if not username:
                    error = 'Please choose a username.'
                elif len(password) < 8:
                    error = 'Password must be at least 8 characters.'
                elif password != request.form.get('confirm', ''):
                    error = 'Passwords do not match.'
                else:
                    user = AppUser(username=username,
                                   password_hash=generate_password_hash(password))
                    db.session.add(user)
                    db.session.commit()
                    _sign_in(user)
                    return redirect('/')
            return render_template('setup.html', error=error)

        @app.route('/login', methods=['GET', 'POST'])
        def login():
            if not AppUser.query.first():
                return redirect(url_for('setup'))
            error = None
            if request.method == 'POST':
                ip = request.remote_addr or 'unknown'
                if _throttled(ip):
                    error = 'Too many failed attempts — try again in 15 minutes.'
                else:
                    user = AppUser.query.filter_by(
                        username=request.form.get('username', '').strip()).first()
                    if user and check_password_hash(user.password_hash,
                                                    request.form.get('password', '')):
                        _failed_logins.pop(ip, None)
                        _sign_in(user)
                        return redirect(_safe_next())
                    _failed_logins.setdefault(ip, []).append(time.time())
                    error = 'Invalid username or password.'
            return render_template('login.html', error=error)

        @app.route('/logout', methods=['POST'])
        def logout():
            session.clear()
            return redirect(url_for('login'))

    @app.template_filter('dict_update')
    def dict_update_filter(d, updates):
        """Jinja2 filter: return a copy of dict d merged with the updates dict."""
        result = dict(d)
        result.update(updates)
        return result

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    def _compute_anomaly_scores():
        """Recompute Isolation Forest anomaly scores for all transactions and persist."""
        transactions = Transaction.query.all()
        if len(transactions) < 10:
            return
        df = pd.DataFrame([{
            'id': t.id,
            'abs_amount': abs(float(t.amount)),
            'day_of_week': pd.to_datetime(t.date).dayofweek,
            'day_of_month': pd.to_datetime(t.date).day,
        } for t in transactions])
        X = df[['abs_amount', 'day_of_week', 'day_of_month']].replace([np.inf, -np.inf], np.nan).dropna()
        model = IsolationForest(contamination='auto', random_state=42)
        scores = model.fit_predict(X)
        for tid, score in zip(df['id'], scores):
            t = Transaction.query.get(tid)
            if t:
                t.anomaly_score = float(score)
        db.session.commit()

    def _sticky_filter(session_key, *arg_keys, default=None):
        """Resolve a filter value from the query string with session fallback.

        A key that is present but empty means the user explicitly cleared the
        filter (e.g. picked "All" in the form), so it must not fall back to
        the stale session value; only a fully absent key does.
        """
        for key in arg_keys or (session_key,):
            if key in request.args:
                return request.args.get(key) or None
        return session.get(session_key, default)

    def _build_transaction_query(account_filter, category_filter, start_date_str,
                                  end_date_str, direction_filter, search_query):
        filters = []
        if account_filter and account_filter != 'both':
            filters.append(Transaction.account_name == account_filter)
        if category_filter:
            filters.append(Transaction.category == category_filter)
        if start_date_str:
            filters.append(Transaction.date >= datetime.strptime(start_date_str, '%Y-%m-%d'))
        if end_date_str:
            filters.append(Transaction.date <= datetime.strptime(end_date_str, '%Y-%m-%d'))
        if search_query:
            terms = []
            try:
                terms.append(Transaction.id == int(search_query))
            except ValueError:
                pass
            terms.append(Transaction.description.ilike(f'%{search_query}%'))
            filters.append(or_(*terms))
        if direction_filter == 'inbound':
            filters.append(Transaction.amount > 0)
        elif direction_filter == 'outgo':
            filters.append(Transaction.amount < 0)
        return Transaction.query.filter(and_(*filters)) if filters else Transaction.query

    def _compute_net_worth():
        """Net-worth breakdown from synced accounts + holdings.

        Prefers automatically synchronized balances (finance_sync) and falls
        back to the manually entered AccountBalance rows for account types
        that have never been synced. Adds brokerage/crypto detail on top of
        the original keys.
        """
        return SyncRepository.compute_totals()

    def _dismissed_recurring_keys():
        return [d.desc_key for d in RecurringDismissal.query.all()]

    def _detect_recurring_summary():
        """Return detected recurring bills and subscriptions for Claude context."""
        txns = Transaction.query.order_by(Transaction.date.asc()).all()
        detected = detect_recurring([{
            'date': t.date,
            'description': t.description,
            'amount': float(t.amount),
            'category': t.category,
        } for t in txns], dismissed_keys=_dismissed_recurring_keys())
        return {
            kind: [{
                'description': g['description'],
                'category': g['category'],
                'monthly_amount': abs(g['monthly_amount']),
                'occurrences': g['occurrences'],
                'last_seen': g['last_seen'],
            } for g in groups]
            for kind, groups in detected.items()
        }

    def _detect_recurring_full():
        """Full recurring detection, memoized for the life of one request.

        Detection walks every transaction and clusters them, which is fine
        once but wasteful three times in a single dashboard render — the
        attention center, the bill schedule, and the forecast all want the
        same answer.
        """
        cached = getattr(g, '_recurring_full', None)
        if cached is not None:
            return cached
        txns = Transaction.query.order_by(Transaction.date.asc()).all()
        detected = detect_recurring([{
            'date': t.date,
            'description': t.description,
            'amount': float(t.amount),
            'category': t.category,
            'account_name': t.account_name,
        } for t in txns], dismissed_keys=_dismissed_recurring_keys())
        g._recurring_full = detected
        return detected

    def _portfolio_snapshot():
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

    def _build_finance_context(months=6, detail=False):
        """Assemble a full financial snapshot for Claude — spending, net worth, and complete holdings detail.

        When ``detail`` is True the snapshot also carries line-item data the chat
        assistant needs to answer questions about specific purchases: recent
        transactions, top merchants, and unreviewed anomalies.
        """
        cutoff = datetime.now() - timedelta(days=months * 30)

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
        recurring_items = _detect_recurring_summary()

        # --- Net worth ---
        nw = _compute_net_worth()

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
        trend_cutoff = _months_ago(CHAT_TREND_MONTHS)
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

    # ---------------------------------------------------------------------------
    # Dashboard
    # ---------------------------------------------------------------------------

    @app.route('/')
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

        nw = _compute_net_worth()

        # ── Layer 2 & 3 intelligence ────────────────────────────────────
        # What the numbers mean, rather than what they are. Kept in
        # dashboard_intel so the reasoning is testable apart from the route.
        today = datetime.now()
        recurring_full = _detect_recurring_full()
        portfolio = _portfolio_snapshot()

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

    # ---------------------------------------------------------------------------
    # Upload (smart merge — no truncate)
    # ---------------------------------------------------------------------------

    @app.route('/upload', methods=['GET', 'POST'])
    def upload():
        if request.method == 'POST':
            if 'files[]' not in request.files:
                flash('No file selected', 'error')
                return redirect(request.url)

            files = request.files.getlist('files[]')
            account_name = request.form.get('account_name')

            if not account_name:
                flash('Please select an account', 'error')
                return redirect(request.url)

            total_new = 0
            total_skipped = 0
            batch_id = str(uuid.uuid4())

            for file in files:
                if not file.filename:
                    continue
                print(f"Processing file: {file.filename}")
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                df = pd.read_csv(filepath)
                df = df.sort_values(by='Transaction Date')
                prev_balance = None

                for idx, row in df.iterrows():
                    desc = str(row['Transaction Description'])
                    date = pd.to_datetime(row['Transaction Date']).date()
                    amount = float(row['Transaction Amount'])
                    balance = float(row['Balance']) if not pd.isnull(row['Balance']) else None

                    signed_amount = amount
                    desc_lower = desc.lower()

                    if 'deposit from 360 checking' in desc_lower or 'deposit from 360 performance savings' in desc_lower:
                        signed_amount = abs(amount)
                    elif 'withdrawal to 360 checking' in desc_lower or 'withdrawal to 360 performance savings' in desc_lower:
                        signed_amount = -abs(amount)
                    elif 'monthly interest paid' in desc_lower:
                        signed_amount = abs(amount)
                    elif 'credit card' in desc_lower or 'credit crd' in desc_lower:
                        signed_amount = -abs(amount)
                    elif 'purchase' in desc_lower:
                        signed_amount = -abs(amount)
                    elif 'deposit' in desc_lower or 'credit' in desc_lower:
                        signed_amount = abs(amount)
                    elif 'withdraw' in desc_lower or 'payment' in desc_lower:
                        signed_amount = -abs(amount)
                    elif prev_balance is not None and balance is not None:
                        if balance < prev_balance:
                            signed_amount = -abs(amount)
                        elif balance > prev_balance:
                            signed_amount = abs(amount)

                    category = category_rules.get_category(desc)
                    transaction = Transaction(
                        account_name=account_name,
                        date=date,
                        description=desc,
                        amount=signed_amount,
                        category=category,
                        import_batch_id=batch_id
                    )
                    try:
                        db.session.add(transaction)
                        db.session.flush()
                        total_new += 1
                    except IntegrityError:
                        db.session.rollback()
                        total_skipped += 1
                    else:
                        db.session.commit()

                    prev_balance = balance

                os.remove(filepath)
                print(f"Finished processing: {file.filename}")

            _compute_anomaly_scores()
            if total_new > 0:
                session['last_batch_id'] = batch_id
                session['last_batch_count'] = total_new
            flash(f'Import complete — {total_new} new transactions added, {total_skipped} duplicates skipped.', 'success')
            return redirect(url_for('upload'))

        return render_template('upload.html',
                               last_batch_id=session.get('last_batch_id'),
                               last_batch_count=session.get('last_batch_count'))

    # ---------------------------------------------------------------------------
    # Transactions (with bulk update + export)
    # ---------------------------------------------------------------------------

    @app.route('/transactions')
    def transactions():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        if per_page not in (25, 50, 100, 250):
            per_page = 50
        sort_by = request.args.get('sort_by', 'date')
        sort_dir = request.args.get('sort_dir', 'desc')

        start_date_str = _sticky_filter('start_date')
        end_date_str = _sticky_filter('end_date')
        account_filter = _sticky_filter('account', default='both')
        category_filter = _sticky_filter('category')
        direction_filter = _sticky_filter('direction', 'type', 'direction')
        search_query = _sticky_filter('search')

        if request.args.get('type'):
            session.pop('search', None)
            search_query = None

        session['start_date'] = start_date_str
        session['end_date'] = end_date_str
        session['account'] = account_filter
        session['category'] = category_filter
        session['direction'] = direction_filter
        session['search'] = search_query

        query = _build_transaction_query(account_filter, category_filter, start_date_str,
                                          end_date_str, direction_filter, search_query)

        sort_col_map = {
            'id': Transaction.id,
            'date': Transaction.date,
            'description': Transaction.description,
            'account': Transaction.account_name,
            'category': Transaction.category,
            'amount': Transaction.amount,
        }
        sort_col = sort_col_map.get(sort_by, Transaction.date)
        order = sort_col.asc() if sort_dir == 'asc' else sort_col.desc()

        categories = db.session.query(Transaction.category).distinct().order_by(Transaction.category).all()
        accounts = db.session.query(Transaction.account_name).distinct().all()

        txn_page = query.order_by(order).paginate(
            page=page, per_page=per_page, error_out=False, max_per_page=250
        )

        return render_template('transactions.html',
                               transactions=txn_page,
                               categories=[c[0] for c in categories],
                               accounts=[a[0] for a in accounts],
                               start_date=start_date_str,
                               end_date=end_date_str,
                               account_filter=account_filter,
                               category_filter=category_filter,
                               direction_filter=direction_filter,
                               search_query=search_query,
                               sort_by=sort_by,
                               sort_dir=sort_dir,
                               per_page=per_page)

    @app.route('/clear_filters')
    def clear_filters():
        for key in ['start_date', 'end_date', 'account', 'category', 'direction', 'search']:
            session.pop(key, None)
        flash('Filters cleared.', 'info')
        next_url = request.args.get('next')
        return redirect(next_url if next_url else url_for('transactions'))

    @app.route('/update_category', methods=['POST'])
    def update_category():
        transaction_id = request.form.get('transaction_id')
        new_category = request.form.get('category')
        transaction = Transaction.query.get_or_404(transaction_id)
        transaction.category = new_category
        try:
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/update_categories_bulk', methods=['POST'])
    def update_categories_bulk():
        data = request.json
        ids = data.get('ids', [])
        new_category = data.get('category', '')
        if not ids or not new_category:
            return jsonify({'success': False, 'error': 'Missing ids or category'})
        try:
            Transaction.query.filter(Transaction.id.in_(ids)).update(
                {Transaction.category: new_category}, synchronize_session='fetch'
            )
            db.session.commit()
            return jsonify({'success': True, 'updated': len(ids)})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/transactions/<int:transaction_id>', methods=['PUT'])
    def edit_transaction(transaction_id):
        t = Transaction.query.get_or_404(transaction_id)
        data = request.json
        if 'date' in data:
            t.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
        if 'description' in data:
            t.description = data['description']
        if 'amount' in data:
            t.amount = float(data['amount'])
        if 'category' in data:
            t.category = data['category']
        if 'account_name' in data:
            t.account_name = data['account_name']
        if 'notes' in data:
            t.notes = data['notes'] or None
        try:
            db.session.commit()
            return jsonify({'success': True, 'transaction': {
                'id': t.id,
                'date': t.date.strftime('%Y-%m-%d'),
                'description': t.description,
                'amount': float(t.amount),
                'category': t.category,
                'account_name': t.account_name,
                'notes': t.notes or '',
            }})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/transactions/<int:transaction_id>', methods=['DELETE'])
    def delete_transaction(transaction_id):
        transaction = Transaction.query.get_or_404(transaction_id)
        try:
            db.session.delete(transaction)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/import/<batch_id>/undo', methods=['POST'])
    def undo_import(batch_id):
        try:
            deleted = Transaction.query.filter_by(import_batch_id=batch_id).delete(synchronize_session='fetch')
            db.session.commit()
            session.pop('last_batch_id', None)
            session.pop('last_batch_count', None)
            flash(f'Import undone — {deleted} transaction(s) removed.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Undo failed: {str(e)}', 'error')
        return redirect(url_for('upload'))

    @app.route('/transactions/bulk_delete', methods=['POST'])
    def bulk_delete_transactions():
        data = request.json
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'})
        try:
            deleted = Transaction.query.filter(Transaction.id.in_(ids)).delete(synchronize_session='fetch')
            db.session.commit()
            return jsonify({'success': True, 'deleted': deleted})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    # ---------------------------------------------------------------------------
    # Export
    # ---------------------------------------------------------------------------

    @app.route('/export')
    def export():
        account_filter = _sticky_filter('account', default='both')
        category_filter = _sticky_filter('category')
        start_date_str = _sticky_filter('start_date')
        end_date_str = _sticky_filter('end_date')
        direction_filter = _sticky_filter('direction', 'type', 'direction')
        search_query = _sticky_filter('search')

        query = _build_transaction_query(account_filter, category_filter, start_date_str,
                                          end_date_str, direction_filter, search_query)
        txns = query.order_by(Transaction.date.desc()).all()

        rows = [{
            'ID': t.id,
            'Date': t.date.strftime('%Y-%m-%d'),
            'Description': t.description,
            'Account': t.account_name,
            'Category': t.category,
            'Amount': float(t.amount),
        } for t in txns]

        df = pd.DataFrame(rows)
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)

        filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    # ---------------------------------------------------------------------------
    # Anomalies (reads from DB column instead of retraining each load)
    # ---------------------------------------------------------------------------

    @app.route('/anomalies/<int:transaction_id>/dismiss', methods=['POST'])
    def dismiss_anomaly(transaction_id):
        t = Transaction.query.get_or_404(transaction_id)
        t.anomaly_reviewed = True
        try:
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/anomalies/dismiss_all', methods=['POST'])
    def dismiss_all_anomalies():
        search_id = request.args.get('search_id')
        query = Transaction.query.filter(Transaction.anomaly_score == -1.0, Transaction.anomaly_reviewed == False)
        if search_id:
            try:
                query = query.filter(Transaction.id == int(search_id))
            except ValueError:
                pass
        try:
            count = query.update({Transaction.anomaly_reviewed: True}, synchronize_session=False)
            db.session.commit()
            return jsonify({'success': True, 'count': count})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/anomalies')
    def anomalies():
        page = request.args.get('page', 1, type=int)
        search_id = request.args.get('search_id')
        sort_by = request.args.get('sort', 'date_desc')
        show_reviewed = request.args.get('show_reviewed', '0') == '1'

        col_map = {
            'id': Transaction.id, 'date': Transaction.date,
            'description': Transaction.description, 'amount': Transaction.amount,
            'category': Transaction.category,
        }
        col_key = sort_by.replace('_asc', '').replace('_desc', '')
        sort_col = col_map.get(col_key, Transaction.date)
        order = sort_col.asc() if sort_by.endswith('_asc') else sort_col.desc()

        query = Transaction.query.filter(Transaction.anomaly_score == -1.0)
        if not show_reviewed:
            query = query.filter(Transaction.anomaly_reviewed == False)
        if search_id:
            try:
                query = query.filter(Transaction.id == int(search_id))
            except ValueError:
                pass

        if not query.first() and not Transaction.query.first():
            flash('No transactions found. Please upload some data first.', 'info')
            return redirect(url_for('upload'))

        anomaly_page = query.order_by(order).paginate(page=page, per_page=50, error_out=False)

        return render_template('anomalies.html',
                               anomalies=anomaly_page,
                               sort_by=sort_by,
                               show_reviewed=show_reviewed)

    # ---------------------------------------------------------------------------
    # Recurring transactions
    # ---------------------------------------------------------------------------

    @app.route('/recurring')
    def recurring():
        account_filter = request.args.get('account', 'both')
        txns = Transaction.query
        if account_filter != 'both':
            txns = txns.filter(Transaction.account_name == account_filter)
        txns = txns.order_by(Transaction.date.asc()).all()

        dismissals = RecurringDismissal.query.order_by(RecurringDismissal.created_at.desc()).all()
        detected = detect_recurring([{
            'date': t.date,
            'description': t.description,
            'amount': float(t.amount),
            'category': t.category,
            'account_name': t.account_name,
        } for t in txns], dismissed_keys=[d.desc_key for d in dismissals])

        accounts = db.session.query(Transaction.account_name).distinct().all()
        return render_template('recurring.html',
                               bills=detected['bills'],
                               subscriptions=detected['subscriptions'],
                               dismissals=dismissals,
                               account_filter=account_filter,
                               accounts=[a[0] for a in accounts])

    @app.route('/recurring/dismiss', methods=['POST'])
    def recurring_dismiss():
        description = (request.form.get('description') or '').strip()
        kind = request.form.get('kind', 'subscription')
        desc_key = normalize_description(description)
        if desc_key and not RecurringDismissal.query.filter_by(desc_key=desc_key).first():
            db.session.add(RecurringDismissal(desc_key=desc_key, description=description,
                                              kind=kind))
            db.session.commit()
            flash(f'"{description}" hidden from recurring view.', 'success')
        return redirect(url_for('recurring'))

    @app.route('/recurring/restore', methods=['POST'])
    def recurring_restore():
        dismissal_id = request.form.get('id', type=int)
        dismissal = db.session.get(RecurringDismissal, dismissal_id) if dismissal_id else None
        if dismissal:
            db.session.delete(dismissal)
            db.session.commit()
            flash(f'"{dismissal.description}" restored to recurring view.', 'success')
        return redirect(url_for('recurring'))

    # ---------------------------------------------------------------------------
    # Budgets
    # ---------------------------------------------------------------------------

    @app.route('/budgets', methods=['GET', 'POST'])
    def budgets():
        categories = [c[0] for c in db.session.query(Transaction.category).distinct().all()]
        accounts = [a[0] for a in db.session.query(Transaction.account_name).distinct().all()]

        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'add':
                category = request.form.get('category', '').strip()
                account_name = request.form.get('account_name', 'both')
                try:
                    monthly_limit = float(request.form.get('monthly_limit', 0))
                except ValueError:
                    flash('Invalid budget amount.', 'error')
                    return redirect(url_for('budgets'))
                existing = Budget.query.filter_by(category=category, account_name=account_name).first()
                if existing:
                    existing.monthly_limit = monthly_limit
                    flash(f'Updated budget for {category}.', 'success')
                else:
                    db.session.add(Budget(category=category, account_name=account_name, monthly_limit=monthly_limit))
                    flash(f'Budget set for {category}.', 'success')
                db.session.commit()
            elif action == 'delete':
                budget_id = request.form.get('budget_id')
                b = Budget.query.get(budget_id)
                if b:
                    db.session.delete(b)
                    db.session.commit()
                    flash('Budget deleted.', 'success')
            return redirect(url_for('budgets'))

        all_budgets = Budget.query.order_by(Budget.category).all()

        # Actual spend for the current month and the one before it, so each
        # budget can show where it stands rather than only what it is. A page
        # of limits with no spend against them cannot answer the only question
        # anyone opens it with: am I over?
        today = datetime.now()
        month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_end = month_start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)

        def _spend_by_category(start, end):
            rows = (db.session.query(Transaction.category,
                                     Transaction.account_name,
                                     func.sum(Transaction.amount).label('net'))
                    .filter(Transaction.date.between(start, end))
                    .group_by(Transaction.category, Transaction.account_name).all())
            totals = {}
            for category, account, net in rows:
                spent = max(0.0, -float(net or 0.0))
                totals[(category, account)] = totals.get((category, account), 0.0) + spent
                totals[(category, 'both')] = totals.get((category, 'both'), 0.0) + spent
            return totals

        this_month = _spend_by_category(month_start, today)
        last_month = _spend_by_category(prev_start, prev_end)

        budget_status = []
        for b in all_budgets:
            limit = float(b.monthly_limit)
            spent = this_month.get((b.category, b.account_name), 0.0)
            prior = last_month.get((b.category, b.account_name), 0.0)
            pct = (spent / limit * 100) if limit > 0 else 0.0
            budget_status.append({
                'budget': b,
                'spent': round(spent, 2),
                'prior': round(prior, 2),
                'remaining': round(limit - spent, 2),
                'pct': round(pct, 1),
                'state': 'danger' if pct > 100 else 'warn' if pct > 80 else 'ok',
                'change_pct': round((spent - prior) / prior * 100) if prior > 0 else None,
            })

        # How far through the month we are — the pace marker on each bar. Being
        # at 60% of a budget means nothing without knowing it is the 5th.
        days_in_month = monthrange(today.year, today.month)[1]
        month_progress = round(today.day / days_in_month * 100)

        return render_template('budgets.html', budgets=all_budgets,
                               budget_status=budget_status,
                               month_label=today.strftime('%B %Y'),
                               month_progress=month_progress,
                               total_budgeted=round(sum(float(b.monthly_limit) for b in all_budgets), 2),
                               total_spent=round(sum(s['spent'] for s in budget_status), 2),
                               categories=categories, accounts=accounts)

    # ---------------------------------------------------------------------------
    # Rules
    # ---------------------------------------------------------------------------

    @app.route('/rules', methods=['GET', 'POST'])
    def rules():
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'add':
                category = request.form.get('category')
                keyword = request.form.get('keyword')
                category_rules.add_rule(category, keyword)
                for transaction in Transaction.query.filter(Transaction.description.ilike(f'%{keyword}%')).all():
                    transaction.category = category
                try:
                    db.session.commit()
                    flash('Rule added and existing transactions updated successfully', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash(f'Error updating transactions: {str(e)}', 'error')
            elif action == 'remove':
                category = request.form.get('category')
                keyword = request.form.get('keyword')
                category_rules.remove_rule(category, keyword)
                for transaction in Transaction.query.filter(
                    Transaction.description.ilike(f'%{keyword}%'),
                    Transaction.category == category
                ).all():
                    transaction.category = 'Uncategorized'
                try:
                    db.session.commit()
                    flash('Rule removed and affected transactions updated successfully', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash(f'Error updating transactions: {str(e)}', 'error')
            return redirect(url_for('rules'))
        rule_stats = {cat: Transaction.query.filter(Transaction.category == cat).count()
                      for cat in category_rules.get_all_rules()}
        uncategorized_count = Transaction.query.filter_by(category='Uncategorized').count()
        return render_template('rules.html', rules=category_rules.get_all_rules(),
                               rule_stats=rule_stats,
                               uncategorized_count=uncategorized_count)

    @app.route('/rules/test', methods=['POST'])
    def rules_test():
        import re as _re
        keyword = (request.json or {}).get('keyword', '').strip()
        if not keyword:
            return jsonify({'matches': []})
        if keyword.startswith('/') and keyword.endswith('/') and len(keyword) > 2:
            pattern = keyword[1:-1]
            try:
                all_txns = Transaction.query.order_by(Transaction.date.desc()).limit(2000).all()
                matches = [t for t in all_txns if _re.search(pattern, t.description, _re.IGNORECASE)][:10]
            except Exception:
                matches = []
        else:
            matches = Transaction.query.filter(
                Transaction.description.ilike(f'%{keyword}%')
            ).order_by(Transaction.date.desc()).limit(10).all()
        return jsonify({'matches': [
            {'date': str(t.date), 'description': t.description,
             'amount': float(t.amount), 'category': t.category}
            for t in matches
        ]})

    @app.route('/rules/reorder', methods=['POST'])
    def rules_reorder():
        new_order = (request.json or {}).get('order', [])
        all_rules = category_rules.get_all_rules()
        reordered = {cat: all_rules[cat] for cat in new_order if cat in all_rules}
        for cat in all_rules:
            if cat not in reordered:
                reordered[cat] = all_rules[cat]
        category_rules.rules = reordered
        category_rules._save_rules(reordered)
        return jsonify({'success': True})

    @app.route('/rules/ai-suggest', methods=['POST'])
    def rules_ai_suggest():
        """Send uncategorized descriptions to Claude and get rule suggestions."""
        body    = request.get_json(force=True) or {}
        model   = (body.get('model') or 'claude-sonnet-4-6').strip()
        if model not in {'claude-haiku-4-5-20251001', 'claude-sonnet-4-6', 'claude-opus-4-8'}:
            model = 'claude-sonnet-4-6'

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503

        # Unique uncategorized descriptions (cap at 200 to stay within token limits)
        rows = (Transaction.query
                .filter_by(category='Uncategorized')
                .with_entities(Transaction.description)
                .distinct()
                .limit(200)
                .all())
        if not rows:
            return jsonify({'suggestions': [], 'message': 'No uncategorized transactions found.'})

        descriptions     = [r[0] for r in rows]
        existing_cats    = list(category_rules.get_all_rules().keys())

        prompt = f"""You are a personal finance assistant analyzing bank/credit-card transaction descriptions.

Existing categories (reuse these when they fit):
{json.dumps(existing_cats)}

Here are {len(descriptions)} unique transaction descriptions that are currently "Uncategorized":
{json.dumps(descriptions, indent=2)}

Suggest keyword rules to categorize them. Guidelines:
- Group related merchants into ONE rule using a regex pattern /merchant1|merchant2/
- Use concise, standard personal-finance categories (Groceries, Dining, Gas, Utilities, Streaming, Healthcare, Shopping, Travel, Entertainment, Rent, Insurance, etc.)
- Reuse existing categories where they fit; only create new ones when clearly needed
- Patterns are case-insensitive substring matches — keep them specific enough to avoid false positives
- Skip descriptions that are too ambiguous or clearly one-off transfers
- Aim for 5–15 high-quality suggestions, not exhaustive coverage

Respond with ONLY valid JSON (no markdown fences, no commentary):
{{
  "suggestions": [
    {{
      "category": "Category Name",
      "keyword": "keyword or /regex/",
      "reason": "one-sentence explanation"
    }}
  ]
}}"""

        try:
            client   = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=model, max_tokens=2000,
                messages=[{'role': 'user', 'content': prompt}]
            )
            text = response.content[0].text.strip()
            # Strip markdown code fences Claude sometimes adds
            text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
            text = re.sub(r'\s*```$',          '', text, flags=re.MULTILINE)
            raw_suggestions = json.loads(text.strip()).get('suggestions', [])
        except json.JSONDecodeError:
            return jsonify({'error': 'Claude returned invalid JSON — try again or use a different model.'}), 500
        except anthropic.APIError as e:
            return jsonify({'error': str(e)}), 500
        except Exception as e:
            return jsonify({'error': f'Unexpected error: {e}'}), 500

        # Enrich each suggestion with real match counts and example descriptions
        all_transactions = Transaction.query.all()
        enriched = []
        for s in raw_suggestions[:20]:
            cat    = (s.get('category') or '').strip()
            kw     = (s.get('keyword')  or '').strip()
            reason = (s.get('reason')   or '').strip()
            if not cat or not kw:
                continue

            is_regex = kw.startswith('/') and kw.endswith('/') and len(kw) > 2
            total_count = 0
            uncat_count = 0
            examples    = []
            for t in all_transactions:
                try:
                    hit = (re.search(kw[1:-1], t.description, re.IGNORECASE)
                           if is_regex else kw.upper() in t.description.upper())
                    if hit:
                        total_count += 1
                        if t.category == 'Uncategorized':
                            uncat_count += 1
                        if len(examples) < 3 and t.description not in examples:
                            examples.append(t.description)
                except re.error:
                    pass

            enriched.append({
                'category':    cat,
                'keyword':     kw,
                'reason':      reason,
                'total_count': total_count,
                'uncat_count': uncat_count,
                'examples':    examples,
            })

        return jsonify({'suggestions': enriched})

    @app.route('/rules/ai-apply', methods=['POST'])
    def rules_ai_apply():
        """Accept one AI suggestion: add the rule and recategorize matching transactions."""
        body     = request.get_json(force=True) or {}
        category = (body.get('category') or '').strip()
        keyword  = (body.get('keyword')  or '').strip()
        if not category or not keyword:
            return jsonify({'error': 'Missing category or keyword'}), 400

        # Add rule at the TOP of the priority list so it beats all existing rules.
        category_rules.add_rule_first(category, keyword)

        is_regex = keyword.startswith('/') and keyword.endswith('/') and len(keyword) > 2
        count    = 0
        try:
            if is_regex:
                pattern = keyword[1:-1]
                # Recategorize ALL matching transactions regardless of current category —
                # the AI rule takes priority over whatever was assigned before.
                for t in Transaction.query.all():
                    try:
                        if re.search(pattern, t.description, re.IGNORECASE):
                            t.category = category
                            count += 1
                    except re.error:
                        pass
            else:
                txns = Transaction.query.filter(
                    Transaction.description.ilike(f'%{keyword}%')
                ).all()
                for t in txns:
                    t.category = category
                count = len(txns)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500

        return jsonify({'ok': True, 'applied_count': count})

    # ---------------------------------------------------------------------------
    # Log
    # ---------------------------------------------------------------------------

    # The /log page is retired; its /api/log/* endpoints remain because the
    # Investments page reads and writes account balances through them.

    @app.route('/api/log/entries', methods=['GET'])
    def get_log_entries():
        entries = LogEntry.query.order_by(LogEntry.date.asc()).all()
        return jsonify([entry.to_dict() for entry in entries])

    def _recompute_log_balances(account_type):
        """Recompute and persist snapshot balance fields for all entries of an account."""
        acc_bal = AccountBalance.query.filter_by(account_type=account_type).first()
        sb = float(acc_bal.starting_balance) if acc_bal else 0.0
        all_entries = LogEntry.query.filter_by(account_type=account_type).all()
        cleared_sum = sum(e.amount for e in all_entries if e.cleared)
        pending_sum = sum(e.amount for e in all_entries if not e.cleared)
        for e in all_entries:
            e.starting_balance = sb
            e.cleared_balance = sb + cleared_sum
            e.pending_total = pending_sum
            e.available_balance = sb + cleared_sum + pending_sum

    @app.route('/api/log/entries', methods=['POST'])
    def add_log_entry():
        data = request.json
        account_type = data['account_type']
        amount = float(data['amount'])
        cleared = bool(data.get('cleared', False))

        acc_bal = AccountBalance.query.filter_by(account_type=account_type).first()
        sb = float(acc_bal.starting_balance) if acc_bal else 0.0
        existing = LogEntry.query.filter_by(account_type=account_type).all()

        cleared_sum = sum(e.amount for e in existing if e.cleared) + (amount if cleared else 0)
        pending_sum = sum(e.amount for e in existing if not e.cleared) + (0 if cleared else amount)

        entry = LogEntry(
            account_type=account_type,
            date=datetime.strptime(data['date'], '%Y-%m-%d').date(),
            description=data['description'],
            amount=amount,
            cleared=cleared,
            starting_balance=sb,
            pending_total=pending_sum,
            cleared_balance=sb + cleared_sum,
            available_balance=sb + cleared_sum + pending_sum
        )
        db.session.add(entry)
        try:
            db.session.commit()
            return jsonify(entry.to_dict())
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    @app.route('/api/log/entries/<int:entry_id>', methods=['PUT'])
    def update_log_entry(entry_id):
        entry = LogEntry.query.get_or_404(entry_id)
        data = request.json
        if 'date' in data:
            entry.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
        if 'description' in data:
            entry.description = data['description']
        if 'amount' in data:
            entry.amount = float(data['amount'])
        if 'cleared' in data:
            entry.cleared = bool(data['cleared'])
        _recompute_log_balances(entry.account_type)
        try:
            db.session.commit()
            return jsonify(entry.to_dict())
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    @app.route('/api/log/entries/<int:entry_id>', methods=['DELETE'])
    def delete_log_entry(entry_id):
        entry = LogEntry.query.get_or_404(entry_id)
        db.session.delete(entry)
        try:
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    @app.route('/api/log/clear', methods=['POST'])
    def clear_log():
        try:
            LogEntry.query.delete()
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    @app.route('/api/log/balances', methods=['GET'])
    def get_account_balances():
        return jsonify([b.to_dict() for b in AccountBalance.query.all()])

    @app.route('/api/log/balances/<account_type>', methods=['PUT'])
    def update_account_balance(account_type):
        balance = AccountBalance.query.filter_by(account_type=account_type).first()
        if not balance:
            balance = AccountBalance(account_type=account_type)
            db.session.add(balance)
        balance.starting_balance = float(request.json['starting_balance'])
        try:
            db.session.commit()
            return jsonify(balance.to_dict())
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    # ---------------------------------------------------------------------------
    # AI Chat
    # ---------------------------------------------------------------------------

    @app.route('/chat')
    def chat():
        return render_template('chat.html')

    @app.route('/api/chat', methods=['POST'])
    def api_chat():
        req = request.get_json(force=True)
        user_message = (req.get('message') or '').strip()
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'ANTHROPIC_API_KEY not configured.'}), 503

        context = _build_finance_context()
        system_prompt = (
            "You are a personal finance and investment advisor. The user's full financial data is provided. "
            "Respond ONLY with valid JSON in this exact shape (no markdown code fences, no extra text): "
            '{"analysis": "markdown string", "insights": ["string"], "recommended_actions": ["string"]}. '
            "analysis supports markdown (headers, bold, tables, lists). "
            "insights and recommended_actions are short plain-text strings. Be specific with dollar amounts."
        )
        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=1500,
                system=system_prompt,
                messages=[{
                    'role': 'user',
                    'content': f"Financial data:\n{json.dumps(_build_finance_context(), indent=2)}\n\nQuestion: {user_message}"
                }]
            )
        except anthropic.APIError as e:
            return jsonify({'error': str(e)}), 502

        raw = response.content[0].text.strip()

        # Strip code fences
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw).strip()

        # Extract outermost JSON object
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            raw = m.group()

        # Parse JSON — completely isolated from HTML conversion
        result = {}
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            result = {'analysis': raw, 'insights': [], 'recommended_actions': []}

        # Unwrap double-encoded: Claude occasionally nests the full JSON inside analysis
        analysis_text = result.get('analysis', '')
        if isinstance(analysis_text, str) and analysis_text.strip().startswith('{'):
            try:
                inner = json.loads(analysis_text)
                if isinstance(inner.get('analysis'), str):
                    result = inner
                    analysis_text = inner['analysis']
            except Exception:
                pass

        # Convert markdown to HTML — fully isolated, never causes a JSON fallback
        try:
            html = _md_to_html(analysis_text) if analysis_text else ''
        except Exception:
            html = '<pre style="white-space:pre-wrap;font-size:.85rem">' + \
                   analysis_text.replace('&', '&amp;').replace('<', '&lt;') + '</pre>'

        return jsonify({
            'html': html,
            'insights': result.get('insights', []),
            'actions': result.get('recommended_actions', []),
        })

    # ── Conversation management ──────────────────────────────────────────────

    @app.route('/api/conversations', methods=['GET'])
    def api_conversations():
        convs = (Conversation.query
                 .order_by(Conversation.updated_at.desc())
                 .all())
        return jsonify({'conversations': [
            {'id': c.id, 'title': c.title, 'updated_at': c.updated_at.isoformat()}
            for c in convs
        ]})

    @app.route('/api/conversations', methods=['POST'])
    def api_new_conversation():
        conv = Conversation(id=str(uuid.uuid4()), title='New Chat')
        db.session.add(conv)
        db.session.commit()
        return jsonify({'id': conv.id, 'title': conv.title})

    @app.route('/api/conversations/<conv_id>', methods=['PATCH'])
    def api_rename_conversation(conv_id):
        """Rename a conversation. Empty titles fall back to 'New Chat'."""
        req   = request.get_json(force=True) or {}
        title = (req.get('title') or '').strip()[:120] or 'New Chat'
        conv  = db.session.get(Conversation, conv_id)
        if not conv:
            return jsonify({'error': 'Conversation not found'}), 404
        try:
            conv.title = title
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({'error': 'Could not rename conversation'}), 500
        return jsonify({'id': conv.id, 'title': conv.title})

    @app.route('/api/conversations/<conv_id>', methods=['DELETE'])
    def api_delete_conversation(conv_id):
        try:
            ChatMessage.query.filter_by(session_id=conv_id).delete()
            Conversation.query.filter_by(id=conv_id).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({'ok': True})

    # ── Chat messages ────────────────────────────────────────────────────────

    @app.route('/api/chat_history')
    def api_chat_history():
        conv_id = request.args.get('conv', '').strip()
        if not conv_id:
            return jsonify({'messages': []})
        msgs = (ChatMessage.query
                .filter_by(session_id=conv_id)
                .order_by(ChatMessage.id.asc())   # id is insertion-ordered; created_at ties when saved in one commit
                .all())
        return jsonify({'messages': [
            {'role': m.role, 'content': m.content,
             'created_at': m.created_at.isoformat() + 'Z'}   # mark as UTC so JS Date parses correctly
            for m in msgs
        ]})

    @app.route('/api/chat_truncate', methods=['POST'])
    def api_chat_truncate():
        """Drop every message from ``keep`` onwards.

        Backs the two rewind actions in the UI: regenerating a reply (keep all
        messages up to and including the last user turn) and editing an earlier
        prompt (keep everything before it). Returns the surviving count so the
        client can reconcile its local cache.
        """
        req     = request.get_json(force=True) or {}
        conv_id = (req.get('conv_id') or '').strip()
        keep    = req.get('keep')
        if not conv_id:
            return jsonify({'error': 'No conversation ID'}), 400
        try:
            keep = max(0, int(keep))
        except (TypeError, ValueError):
            return jsonify({'error': 'keep must be an integer'}), 400

        msgs = (ChatMessage.query
                .filter_by(session_id=conv_id)
                .order_by(ChatMessage.id.asc()).all())
        try:
            for m in msgs[keep:]:
                db.session.delete(m)
            db.session.commit()
        except Exception as e:
            app.logger.error('chat_truncate failed: %s', e)
            db.session.rollback()
            return jsonify({'error': 'Could not update conversation'}), 500
        return jsonify({'ok': True, 'remaining': min(keep, len(msgs))})

    _ALLOWED_MODELS = {
        'claude-haiku-4-5-20251001',
        'claude-sonnet-4-6',
        'claude-opus-4-8',
    }

    @app.route('/api/chat_stream', methods=['POST'])
    def api_chat_stream():
        req = request.get_json(force=True)
        user_message = (req.get('message') or '').strip()
        conv_id       = (req.get('conv_id') or '').strip()
        model         = (req.get('model')   or 'claude-sonnet-4-6').strip()
        # `resend` replays the conversation as it already stands — used by
        # "regenerate", where the trailing assistant reply has been truncated
        # away and the last stored message is the user's prompt.
        resend        = bool(req.get('resend'))
        if model not in _ALLOWED_MODELS:
            model = 'claude-sonnet-4-6'
        if not user_message and not resend:
            return jsonify({'error': 'No message provided'}), 400
        if not conv_id:
            return jsonify({'error': 'No conversation ID'}), 400

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'The AI assistant is not set up yet — no API key is configured.'}), 503

        conv = db.session.get(Conversation, conv_id)
        if not conv:
            return jsonify({'error': 'Conversation not found'}), 404

        # ── Build Claude context from history BEFORE saving the current message ──
        recent = (ChatMessage.query
                  .filter_by(session_id=conv_id)
                  .order_by(ChatMessage.id.desc())
                  .limit(CHAT_HISTORY_LIMIT).all())
        history = [{'role': m.role, 'content': m.content} for m in reversed(recent)]

        if resend:
            # Nothing new to persist; the stored history already ends with the
            # user turn we want answered.
            if not history or history[-1]['role'] != 'user':
                return jsonify({'error': 'Nothing to regenerate'}), 400
            messages = history
        else:
            # ── Persist user message immediately so it survives navigation / disconnect ──
            user_ts = datetime.utcnow()
            try:
                db.session.add(ChatMessage(session_id=conv_id, role='user',
                                           content=user_message, created_at=user_ts))
                if conv.title == 'New Chat':
                    conv.title = user_message[:55] + ('…' if len(user_message) > 55 else '')
                conv.updated_at = user_ts
                db.session.commit()
            except Exception as e:
                app.logger.error('chat_stream pre-save user msg failed: %s', e)
                db.session.rollback()
            messages = history + [{'role': 'user', 'content': user_message}]

        fin_ctx = _build_finance_context(detail=True)
        system_prompt = (
            "You are the personal finance assistant built into this user's own checkbook app. "
            "You are talking to the account holder about their own money, and every question "
            "should be answered from the snapshot of their linked accounts below — transactions, "
            "spending, income, budgets, recurring bills, net worth, investment holdings, and "
            "flagged anomalies.\n\n"
            f"{json.dumps(fin_ctx, indent=2, default=str)}\n\n"
            "How to answer:\n"
            "- Lead with the answer, then the supporting numbers. Cite real figures from the data.\n"
            "- Write for a smart person who is not a finance professional: plain language, no jargon "
            "unless you define it in the same breath.\n"
            "- Keep it short by default. Use a table or bullet list only when it genuinely reads "
            "better than a sentence; skip headers on short answers.\n"
            "- Format money the way people read it ($1,284.50).\n"
            "- Check 'transaction_coverage' before saying anything about what you can and "
            "cannot see. It states the real date range.\n"
            "- For any 'per month', 'trend', 'this year', or 'compare months' question, use "
            "'monthly_spending_by_category' and 'monthly_income' — they cover the whole "
            "range in 'transaction_coverage', broken down by category. Do not rebuild a "
            "monthly total by adding up 'recent_transactions'.\n"
            "- For one account ('checking only', 'what does savings pay for'), use "
            "'monthly_spending_by_account_category' and 'monthly_income_by_account', which "
            "carry the same full range split by account. The account names are listed in "
            "'transaction_coverage'. Never say you cannot separate the accounts, and never "
            "fall back to 'recent_transactions' for a question these series answer.\n"
            "- Transfers between the user's own accounts are movement, not spending. When "
            "totalling ACROSS accounts, leave the Transfer category out and say so, or it "
            "double-counts money that only moved. When reporting on a SINGLE account, keep "
            "transfers in — money really did leave that account — and label them as such. "
            "If transfers are more than half of a chart, say so in your sentence: the shape "
            "of everything else is invisible next to them.\n"
            "- Every chart must reconcile. If you fold the smaller categories into an "
            "'Other' series, get it by subtracting the categories you named from that "
            "month's figure in 'monthly_spending_totals' ('total' when transfers are in the "
            "chart, 'excluding_transfers' when they are not). Never add the leftover "
            "categories up yourself — that is the step that goes wrong, and a wrong 'Other' "
            "hides real spending. If your series do not sum to the total, the chart is wrong.\n"
            "- 'recent_transactions' is only the newest slice, for questions about specific "
            "purchases. Its start date is NOT the start of your data. If someone asks to "
            "itemise a month older than that slice, say you can give the monthly totals but "
            "not the individual purchases for that month.\n"
            "- Use the category names as they appear in the data. If a question uses a "
            "different word, map it to the real categories and say which ones you combined.\n"
            "- Never invent a transaction, holding, or balance that is not in the data.\n\n"
            + CHART_INSTRUCTIONS
        )

        def _generate():
            full_response = ''
            stream_done   = False
            try:
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model=model,
                    max_tokens=CHAT_MAX_TOKENS,
                    # The snapshot runs ~8-9k tokens and is byte-identical for
                    # every turn in a session, so cache it: later messages read
                    # the prefix at a tenth of the cost and start streaming
                    # noticeably sooner.
                    system=[{
                        'type': 'text',
                        'text': system_prompt,
                        'cache_control': {'type': 'ephemeral'},
                    }],
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        full_response += text
                        yield f'data: {json.dumps({"delta": text})}\n\n'
                stream_done = True
            except anthropic.RateLimitError:
                yield f'data: {json.dumps({"error": "Too many requests right now. Give it a moment and try again."})}\n\n'
                return
            except anthropic.AuthenticationError:
                yield f'data: {json.dumps({"error": "The configured API key was rejected. Check ANTHROPIC_API_KEY."})}\n\n'
                return
            except anthropic.APIConnectionError:
                yield f'data: {json.dumps({"error": "Could not reach the AI service. Check your connection and try again."})}\n\n'
                return
            except anthropic.APIError as e:
                app.logger.error('chat_stream API error: %s', e)
                yield f'data: {json.dumps({"error": "The AI service returned an error. Try again in a moment."})}\n\n'
                return
            except Exception as e:
                app.logger.error('chat_stream unexpected error: %s', e)
                yield f'data: {json.dumps({"error": "Something went wrong generating that reply. Try again."})}\n\n'
                return
            finally:
                # Runs on normal completion, errors, AND client disconnect (GeneratorExit).
                # Saves whatever response accumulated so nothing is silently lost.
                if full_response:
                    try:
                        asst_ts = datetime.utcnow()
                        db.session.add(ChatMessage(session_id=conv_id, role='assistant',
                                                   content=full_response, created_at=asst_ts))
                        c = db.session.get(Conversation, conv_id)
                        if c:
                            c.updated_at = asst_ts
                        db.session.commit()
                    except Exception as e:
                        app.logger.error('chat_stream save assistant failed: %s', e)
                        db.session.rollback()

            if stream_done:
                yield 'data: [DONE]\n\n'

        return Response(
            stream_with_context(_generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
        )

    @app.route('/api/chat_clear', methods=['POST'])
    def api_chat_clear():
        """Clear messages for a conversation without deleting it."""
        req     = request.get_json(force=True) or {}
        conv_id = (req.get('conv_id') or '').strip()
        if conv_id:
            try:
                ChatMessage.query.filter_by(session_id=conv_id).delete()
                conv = db.session.get(Conversation, conv_id)
                if conv:
                    conv.title      = 'New Chat'
                    conv.updated_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
        return jsonify({'ok': True})

    @app.route('/api/dashboard-insight')
    def dashboard_insight():
        now = time.time()
        if _insight_cache['text'] and now < _insight_cache['expires']:
            return jsonify({'insight': _insight_cache['text']})
        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'insight': ''})
        context = _build_finance_context(months=1)
        client = anthropic.Anthropic(api_key=api_key)
        try:
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=200,
                system=(
                    "You are a personal finance advisor. In 2-3 sentences, give the single most "
                    "important financial insight from the data. Be specific with dollar amounts."
                ),
                messages=[{'role': 'user', 'content': json.dumps(context)}]
            )
            text = resp.content[0].text.strip()
            _insight_cache['text'] = text
            _insight_cache['expires'] = now + app.config.get('AI_INSIGHT_CACHE_TTL', 3600)
            return jsonify({'insight': text})
        except Exception:
            return jsonify({'insight': ''})

    # ---------------------------------------------------------------------------
    # Financial Copilot — the dashboard's conversational layer
    # ---------------------------------------------------------------------------

    def _copilot_context(start=None, end=None):
        """The compact snapshot the copilot reasons over.

        Deliberately smaller than the chat assistant's context: the briefing
        needs the shape of the period, not every line item, and a tight
        context is what keeps the card appearing in about a second.

        The window defaults to the current month but follows the dashboard's
        own filter when one is given. Without that the copilot would narrate
        July while the page beneath it showed March through June.
        """
        end = end or datetime.now()
        start = start or end.replace(day=1)
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

        nw = _compute_net_worth()
        recurring_full = _detect_recurring_full()
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
            'portfolio': _portfolio_snapshot(),
            'monthly_budgets': budget_map,
            'upcoming_bills': upcoming[:10],
            'subscriptions': [{'description': s['description'],
                               'monthly_amount': abs(s['monthly_amount'])}
                              for s in recurring_full.get('subscriptions', [])[:15]],
        }

    _COPILOT_STYLE = (
        "You are the financial copilot built into the account holder's own checkbook app. "
        "You are talking to them about their own money, using the snapshot below.\n\n"
        "Voice: calm, specific, and short. Lead with the answer. Cite real dollar figures "
        "from the data and format them the way people read them ($1,284). Plain language, "
        "no jargon. Never invent a transaction, balance, or holding that is not in the data. "
        "Transfers between the user's own accounts are movement, not spending — leave them "
        "out of spending totals."
    )

    @app.route('/api/copilot/brief')
    def copilot_brief():
        """A short written read on the month, plus concrete opportunities.

        The dashboard already renders the hard numbers server-side, so this
        endpoint exists only for the parts that need judgement: how the month
        is actually going, and what is worth doing about it.
        """
        def _parse(name):
            raw = request.args.get(name)
            try:
                return datetime.strptime(raw, '%Y-%m-%d') if raw else None
            except ValueError:
                return None

        start, end = _parse('start'), _parse('end')
        # Cache per window: a briefing about March–June must not be served to
        # a reader who has since switched the dashboard to this month.
        cache_key = f"{start:%Y-%m-%d}|{end:%Y-%m-%d}" if start and end else 'default'

        now = time.time()
        if (_brief_cache['data'] and now < _brief_cache['expires']
                and _brief_cache.get('key') == cache_key):
            return jsonify(_brief_cache['data'])

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'available': False})

        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=700,
                system=(
                    _COPILOT_STYLE + "\n\n"
                    "Reply with ONLY a JSON object, no prose around it, shaped like:\n"
                    '{"narrative": "2-3 sentences on how this month is going versus last, '
                    'naming the single biggest driver of the difference.",\n'
                    ' "opportunities": [{"title": "Short imperative, max 6 words",\n'
                    '                    "detail": "One sentence with the dollar figure and why.",\n'
                    '                    "impact": "estimated annual or monthly dollar saving, e.g. $480/yr"}],\n'
                    ' "questions": ["3 or 4 short follow-up questions the user might ask next, '
                    'each answerable from this data, phrased in their voice"]}\n\n'
                    "Give 2-3 opportunities, most valuable first. An opportunity must be "
                    "something they can act on, grounded in a figure you can point to. If the "
                    "period genuinely offers none, return an empty list rather than padding it.\n\n"
                    "Write about 'selected_period' versus 'previous_period' and refer to them by "
                    "their labels. This briefing sits directly beneath a dashboard showing exactly "
                    "that window, so narrating a different one contradicts what the user is "
                    "looking at."
                ),
                messages=[{'role': 'user',
                           'content': json.dumps(_copilot_context(start, end), default=str)}]
            )
            raw = resp.content[0].text.strip()
            # Haiku occasionally wraps JSON in a fenced block despite the instruction.
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
            data = json.loads(raw)
            data['available'] = True
            data.setdefault('opportunities', [])
            data.setdefault('questions', [])
            _brief_cache['data'] = data
            _brief_cache['key'] = cache_key
            _brief_cache['expires'] = now + app.config.get('AI_INSIGHT_CACHE_TTL', 3600)
            return jsonify(data)
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            app.logger.warning('copilot_brief could not parse model output: %s', e)
            return jsonify({'available': False})
        except Exception as e:
            app.logger.error('copilot_brief failed: %s', e)
            return jsonify({'available': False})

    @app.route('/api/copilot/ask', methods=['POST'])
    def copilot_ask():
        """Answer one dashboard question, streamed, without touching chat history.

        Deliberately stateless: the copilot card is for a quick question in
        passing. Anything that wants to become a conversation has a "continue
        in chat" path into /chat, which is where history belongs.
        """
        req = request.get_json(force=True) or {}
        question = (req.get('question') or '').strip()
        if not question:
            return jsonify({'error': 'No question provided'}), 400
        if len(question) > 500:
            question = question[:500]

        def _parse(name):
            try:
                raw = req.get(name)
                return datetime.strptime(raw, '%Y-%m-%d') if raw else None
            except (ValueError, TypeError):
                return None

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'The AI assistant is not set up yet — '
                                     'no API key is configured.'}), 503

        system_prompt = (
            _COPILOT_STYLE + "\n\n"
            f"{json.dumps(_copilot_context(_parse('start'), _parse('end')), indent=2, default=str)}\n\n"
            "'selected_period' is the window the user currently has the dashboard "
            "filtered to. Unless they name a different one, answer about that.\n\n"
            "This answer appears in a small card on their dashboard, so keep it to "
            "2-4 sentences. No headings. Use a short bullet list only if the answer is "
            "genuinely a list. If the question needs data you do not have here, say so "
            "in one sentence and suggest opening the full chat."
        )

        def _generate():
            try:
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model='claude-sonnet-4-6',
                    max_tokens=600,
                    system=[{'type': 'text', 'text': system_prompt,
                             'cache_control': {'type': 'ephemeral'}}],
                    messages=[{'role': 'user', 'content': question}],
                ) as stream:
                    for text in stream.text_stream:
                        yield f'data: {json.dumps({"delta": text})}\n\n'
                yield 'data: [DONE]\n\n'
            except anthropic.RateLimitError:
                yield f'data: {json.dumps({"error": "Too many requests right now. Give it a moment."})}\n\n'
            except anthropic.AuthenticationError:
                yield f'data: {json.dumps({"error": "The configured API key was rejected."})}\n\n'
            except anthropic.APIConnectionError:
                yield f'data: {json.dumps({"error": "Could not reach the AI service."})}\n\n'
            except Exception as e:
                app.logger.error('copilot_ask failed: %s', e)
                yield f'data: {json.dumps({"error": "Something went wrong. Try again."})}\n\n'

        return Response(
            stream_with_context(_generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                     'Connection': 'keep-alive'},
        )

    # ---------------------------------------------------------------------------
    # Investments / Holdings
    # ---------------------------------------------------------------------------

    def _monthly_outgo(months=6):
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

    def _snapshot_history(days=730):
        """Daily net-worth snapshots, oldest first — the only real price history."""
        cutoff = date.today() - timedelta(days=days)
        rows = (PortfolioSnapshotRow.query
                .filter(PortfolioSnapshotRow.snapshot_date >= cutoff)
                .order_by(PortfolioSnapshotRow.snapshot_date).all())
        return [r.to_dict() for r in rows]

    def _wealth_snapshot(benchmark='sp500', horizon=10, contribution=0.0):
        """Everything the Investments page reasons over, in one place.

        The route renders it, the copilot endpoints send it to the model, and
        the tests can call it directly — one derivation, three consumers, no
        chance of the AI narrating figures the page does not show.
        """
        holding_rows = [h.to_dict() for h in
                        Holding.query.order_by(Holding.asset_class, Holding.ticker).all()]
        nw = _compute_net_worth()
        history = _snapshot_history()
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
            diversification=div, conc=conc, monthly_expenses=_monthly_outgo())
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

    @app.route('/investments')
    def investments():
        benchmark = request.args.get('benchmark', 'sp500')
        if benchmark not in investments_intel.BENCHMARKS:
            benchmark = 'sp500'
        try:
            horizon = max(1, min(40, int(request.args.get('horizon', 10))))
        except (TypeError, ValueError):
            horizon = 10
        try:
            contribution = max(0.0, float(request.args.get('contribution', 0)))
        except (TypeError, ValueError):
            contribution = 0.0

        snap = _wealth_snapshot(benchmark, horizon, contribution)
        nw = snap['nw']
        holdings = Holding.query.order_by(Holding.asset_class, Holding.ticker).all()

        # The original donut mixed cash accounts in with the holdings; keep
        # that view intact, it answers a different question from the pure
        # portfolio allocation below it.
        portfolio_by_class = ({'Checking': nw['checking'], 'Savings': nw['savings']}
                              if (nw['checking'] or nw['savings']) else {})
        for h in holdings:
            portfolio_by_class[h.asset_class] = round(
                portfolio_by_class.get(h.asset_class, 0) + float(h.current_value), 2)
        portfolio_by_class = {k: v for k, v in portfolio_by_class.items() if v > 0}
        asset_classes = ['Stock', 'ETF', 'Mutual Fund', 'Bond', 'Crypto', 'Cash', 'Other']

        # --- synchronization context (finance_sync) ---
        connections = [c.to_dict() for c in InstitutionConnection.query
                       .order_by(InstitutionConnection.display_name).all()]
        synced_accounts = [a.to_dict() for a in
                           FinancialAccount.query.filter_by(is_active=True)
                           .order_by(FinancialAccount.account_type,
                                     FinancialAccount.name).all()]
        cash_synced = any(a['account_type'] in ('checking', 'savings')
                          for a in synced_accounts)
        last_sync = (db.session.query(func.max(InstitutionConnection.last_sync_at))
                     .scalar())
        investment_accounts = [a for a in synced_accounts
                               if a['account_type'] in ('brokerage', 'crypto')]

        return render_template(
            'investments.html',
            holdings=holdings, nw=nw,
            portfolio_by_class=portfolio_by_class,
            asset_classes=asset_classes,
            connections=connections,
            synced_accounts=synced_accounts,
            cash_synced=cash_synced,
            last_sync=last_sync.strftime('%Y-%m-%d %H:%M') if last_sync else None,
            wealth=snap,
            accounts=investments_intel.account_rollup(
                snap['positions'], investment_accounts, connections),
            benchmarks=investments_intel.BENCHMARKS,
            benchmark_key=benchmark,
            horizon=horizon,
            contribution=contribution,
        )

    # ── Wealth copilot ──────────────────────────────────────────────────────
    # Same split as the dashboard copilot: the page renders every hard number
    # server-side, and the model is asked only for the parts that need
    # judgement. Its context is the identical snapshot the page drew from, so
    # it can never narrate figures the reader cannot see.

    def _wealth_context():
        snap = _wealth_snapshot()
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

    _WEALTH_STYLE = (
        "You are the wealth copilot built into the account holder's own portfolio app. "
        "You are talking to them about their own investments, using the snapshot below.\n\n"
        "Voice: calm, specific, and short. Lead with the answer. Cite real figures from "
        "the data and format them the way people read them ($12,480, 4.2%). Plain "
        "language, no jargon.\n\n"
        "Honesty rules, which matter more here than anywhere else in the app:\n"
        "- Never invent a holding, price, balance, or return that is not in the data.\n"
        "- Sector, region, market-cap and dividend-yield labels come from a built-in "
        "reference table, not a market feed. 'allocation_coverage' says how much of the "
        "portfolio could be classified. Say 'estimated' when you lean on them.\n"
        "- 'benchmark_reference' is a modelled line compounding at a long-run average "
        "rate, not live index data. Never present it as the S&P's actual return for "
        "this period.\n"
        "- 'projection' is a model built on the stated assumptions. Never call it a "
        "prediction or imply a guaranteed outcome.\n"
        "- When 'performance.sparse' is true there are only a handful of daily "
        "snapshots, mostly taken while accounts were still being connected. Say so "
        "if you quote a return from that window — a swing there is usually setup, "
        "not the market.\n"
        "- You are not a licensed advisor. Explain trade-offs and name what a decision "
        "depends on rather than issuing buy or sell instructions."
    )

    @app.route('/api/investments/brief')
    def wealth_brief():
        """A written read on the portfolio, plus concrete moves worth considering."""
        now = time.time()
        if _wealth_cache['data'] and now < _wealth_cache['expires']:
            return jsonify(_wealth_cache['data'])

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'available': False})

        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=900,
                system=(
                    _WEALTH_STYLE + "\n\n"
                    "Reply with ONLY a JSON object, no prose around it, shaped like:\n"
                    '{"narrative": "2-3 sentences on the shape and health of this '
                    'portfolio right now, naming the single thing that most defines it.",\n'
                    ' "opportunities": [{"title": "Short imperative, max 6 words",\n'
                    '                    "detail": "One sentence with the figure and why it matters.",\n'
                    '                    "impact": "the size of the move, e.g. \'$18k over-weight\' or \'+9 health\'"}],\n'
                    ' "questions": ["3 or 4 short follow-up questions the user might ask '
                    'next, each answerable from this data, phrased in their voice"]}\n\n'
                    "Give 2-3 opportunities, most valuable first, each grounded in a "
                    "figure you can point to. If the portfolio genuinely offers none, "
                    "return an empty list rather than padding it."
                ),
                messages=[{'role': 'user',
                           'content': json.dumps(_wealth_context(), default=str)}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
            data = json.loads(raw)
            data['available'] = True
            data.setdefault('opportunities', [])
            data.setdefault('questions', [])
            _wealth_cache['data'] = data
            _wealth_cache['expires'] = now + app.config.get('AI_INSIGHT_CACHE_TTL', 3600)
            return jsonify(data)
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            app.logger.warning('wealth_brief could not parse model output: %s', e)
            return jsonify({'available': False})
        except Exception as e:
            app.logger.error('wealth_brief failed: %s', e)
            return jsonify({'available': False})

    @app.route('/api/investments/ask', methods=['POST'])
    def wealth_ask():
        """Answer one portfolio question, streamed, with follow-up context.

        Unlike the dashboard copilot this one accepts a short prior turn list,
        because "should I rebalance?" is rarely the last thing someone wants
        to ask. History stays client-side and capped — anything that wants to
        become a real conversation has a path into /chat.
        """
        req = request.get_json(force=True) or {}
        question = (req.get('question') or '').strip()
        if not question:
            return jsonify({'error': 'No question provided'}), 400
        question = question[:500]

        history = []
        for turn in (req.get('history') or [])[-6:]:
            role = turn.get('role')
            content = (turn.get('content') or '').strip()[:2000]
            if role in ('user', 'assistant') and content:
                history.append({'role': role, 'content': content})
        # A trailing assistant turn would leave two assistant messages in a row
        # once the new question is appended below.
        while history and history[-1]['role'] == 'assistant' and len(history) % 2 == 0:
            history.pop()

        api_key = app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'The AI assistant is not set up yet — '
                                     'no API key is configured.'}), 503

        system_prompt = (
            _WEALTH_STYLE + "\n\n"
            f"{json.dumps(_wealth_context(), indent=2, default=str)}\n\n"
            "This answer appears in a card on the user's Investments page, beside the "
            "charts these figures came from. Keep it to 2-4 sentences. No headings. "
            "Use a short bullet list only when the answer is genuinely a list. If the "
            "question needs data that is not here — a live quote, a tax position, a "
            "specific fund's holdings — say so in one sentence and suggest the full chat."
        )

        messages = history + [{'role': 'user', 'content': question}]

        def _generate():
            try:
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model='claude-sonnet-4-6',
                    max_tokens=800,
                    system=[{'type': 'text', 'text': system_prompt,
                             'cache_control': {'type': 'ephemeral'}}],
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        yield f'data: {json.dumps({"delta": text})}\n\n'
                yield 'data: [DONE]\n\n'
            except anthropic.RateLimitError:
                yield f'data: {json.dumps({"error": "Too many requests right now. Give it a moment."})}\n\n'
            except anthropic.AuthenticationError:
                yield f'data: {json.dumps({"error": "The configured API key was rejected."})}\n\n'
            except anthropic.APIConnectionError:
                yield f'data: {json.dumps({"error": "Could not reach the AI service."})}\n\n'
            except Exception as e:
                app.logger.error('wealth_ask failed: %s', e)
                yield f'data: {json.dumps({"error": "Something went wrong. Try again."})}\n\n'

        return Response(
            stream_with_context(_generate()),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                     'Connection': 'keep-alive'},
        )

    # Tests reach the snapshot directly; the AI routes above need a key and a
    # network, and the page itself is a poor place to assert on arithmetic.
    app.wealth_snapshot = _wealth_snapshot

    @app.route('/api/holdings', methods=['POST'])
    def add_holding():
        d = request.get_json(force=True)
        h = Holding(
            ticker=d.get('ticker', '').upper(),
            name=d.get('name', ''),
            shares=d.get('shares', 0),
            current_value=d.get('current_value', 0),
            asset_class=d.get('asset_class', 'Stock'),
            account_name=d.get('account_name', 'Brokerage'),
        )
        db.session.add(h)
        try:
            db.session.commit()
            return jsonify(h.to_dict()), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    @app.route('/api/holdings/<int:hid>', methods=['PUT', 'DELETE'])
    def holding(hid):
        h = Holding.query.get_or_404(hid)
        if h.source == 'sync':
            return jsonify({'error': 'This holding is synchronized automatically from '
                                     f'{h.account_name} and cannot be edited manually. '
                                     'Manage it from the Connections page.'}), 409
        if request.method == 'DELETE':
            db.session.delete(h)
            try:
                db.session.commit()
                return jsonify({'ok': True})
            except Exception as e:
                db.session.rollback()
                return jsonify({'error': str(e)}), 400
        d = request.get_json(force=True)
        if 'ticker' in d:
            h.ticker = d['ticker'].upper()
        for field in ['name', 'shares', 'current_value', 'asset_class', 'account_name']:
            if field in d:
                setattr(h, field, d[field])
        try:
            db.session.commit()
            return jsonify(h.to_dict())
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

    # Auto-migrate: add new columns / tables to existing SQLite DB if they don't exist yet
    from sqlalchemy import text as _text
    with app.app_context():
        with db.engine.connect() as _conn:
            # Create holdings table if it doesn't exist (fallback for installs
            # without flask db upgrade) BEFORE the column migrations below so a
            # fresh database also receives the finance_sync columns.
            _conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker VARCHAR(20) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    shares NUMERIC(14,6) NOT NULL DEFAULT 0,
                    current_value NUMERIC(12,2) NOT NULL,
                    asset_class VARCHAR(20) NOT NULL DEFAULT 'Stock',
                    account_name VARCHAR(50) NOT NULL DEFAULT 'Brokerage',
                    updated_at DATETIME
                )
            """))
            _conn.commit()
            for _col_sql in [
                "ALTER TABLE transactions ADD COLUMN notes TEXT",
                "ALTER TABLE transactions ADD COLUMN import_batch_id VARCHAR(36)",
                "ALTER TABLE transactions ADD COLUMN anomaly_reviewed BOOLEAN NOT NULL DEFAULT 0",
                # finance_sync columns
                "ALTER TABLE transactions ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'csv'",
                "ALTER TABLE transactions ADD COLUMN account_id INTEGER",
                "ALTER TABLE transactions ADD COLUMN external_id VARCHAR(120)",
                "ALTER TABLE holdings ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'manual'",
                "ALTER TABLE holdings ADD COLUMN account_id INTEGER",
                "ALTER TABLE holdings ADD COLUMN external_id VARCHAR(120)",
                "ALTER TABLE holdings ADD COLUMN avg_cost NUMERIC(14,4)",
                "ALTER TABLE holdings ADD COLUMN current_price NUMERIC(14,4)",
                "ALTER TABLE holdings ADD COLUMN last_synced_at DATETIME",
                "ALTER TABLE connected_accounts ADD COLUMN item_id VARCHAR(80)",
            ]:
                try:
                    _conn.execute(_text(_col_sql))
                    _conn.commit()
                except Exception:
                    pass  # column already exists

            # connected_accounts originally had UNIQUE(institution) alone, which
            # blocks aggregator adapters (Plaid) from linking more than one
            # institution. Rebuild the table under the new UNIQUE(institution,
            # item_id) constraint if the old single-column constraint is still
            # in place. SQLite can't ALTER a constraint directly.
            try:
                _unique_single_institution = False
                for _idx in _conn.execute(_text(
                        "PRAGMA index_list('connected_accounts')")).fetchall():
                    if not _idx[2]:  # not unique
                        continue
                    _cols = [r[2] for r in _conn.execute(
                        _text(f"PRAGMA index_info('{_idx[1]}')")).fetchall()]
                    if _cols == ['institution']:
                        _unique_single_institution = True
                        break
                if _unique_single_institution:
                    _conn.execute(_text(
                        "ALTER TABLE connected_accounts RENAME TO connected_accounts_old"))
                    _conn.commit()
                    db.create_all()  # recreates connected_accounts with the new schema
                    _conn.execute(_text(
                        "INSERT INTO connected_accounts (id, institution, item_id, "
                        "display_name, status, auth_blob, token_expires_at, last_sync_at, "
                        "last_sync_status, last_error, created_at, updated_at) "
                        "SELECT id, institution, NULL, display_name, status, auth_blob, "
                        "token_expires_at, last_sync_at, last_sync_status, last_error, "
                        "created_at, updated_at FROM connected_accounts_old"))
                    _conn.execute(_text("DROP TABLE connected_accounts_old"))
                    _conn.commit()
            except Exception:
                pass  # table doesn't exist yet (fresh install) — db.create_all() below handles it

    # Create any new tables (e.g. chat_messages, finance_sync tables) without
    # touching existing data, plus dedupe indexes on pre-existing tables.
    with app.app_context():
        db.create_all()
        with db.engine.connect() as _conn:
            for _idx_sql in [
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_external_unique "
                "ON transactions (account_id, external_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_holding_sync_unique "
                "ON holdings (account_id, ticker)",
            ]:
                try:
                    _conn.execute(_text(_idx_sql))
                    _conn.commit()
                except Exception:
                    pass  # index already exists

    # ---------------------------------------------------------------------------
    # Financial institution synchronization (finance_sync)
    # ---------------------------------------------------------------------------
    app.register_blueprint(sync_bp)

    # The assistant's whole answer quality rides on this snapshot, so give the
    # tests a way in — the routes that use it need an API key and a network.
    app.build_finance_context = _build_finance_context

    if app.config.get('SYNC_AUTO_ENABLED', True) and not app.config.get('TESTING'):
        # Start the background scheduler lazily on the first request so it only
        # runs in the serving process (never in the werkzeug reloader parent).
        @app.before_request
        def _ensure_sync_scheduler():
            init_scheduler(app, interval_hours=app.config.get('SYNC_INTERVAL_HOURS', 12))
    else:
        # Tests still need a scheduler object for the manual-refresh API,
        # but without the periodic background thread.
        init_scheduler(app, interval_hours=app.config.get('SYNC_INTERVAL_HOURS', 12),
                       autostart=False)

    return app

def _ensure_dev_cert(base_dir):
    """Create (once) and return a self-signed localhost certificate pair.

    Plaid requires https for OAuth redirect URIs even in sandbox, so local
    testing needs TLS. The cert covers localhost and this machine's LAN IP;
    browsers will show a one-time "not trusted" warning — that's expected
    for a self-signed cert and fine for development.
    """
    import ipaddress
    import socket
    from datetime import timezone

    cert_path = os.path.join(base_dir, '.dev-cert.pem')
    key_path = os.path.join(base_dir, '.dev-key.pem')
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Best-effort LAN IP so other devices on the network can use https too.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    sans = [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]
    if lan_ip:
        sans.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(key_path, 'wb') as fh:
        fh.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    with open(cert_path, 'wb') as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _truthy(name, default=''):
    return os.environ.get(name, default).lower() in ('1', 'true', 'yes')


if __name__ == '__main__':
    app = create_app()
    ssl_context = None
    if _truthy('APP_HTTPS'):
        ssl_context = _ensure_dev_cert(os.path.dirname(os.path.abspath(__file__)))

    # Login is always required (owner account created at /setup on first run),
    # so LAN exposure via APP_HOST=0.0.0.0 is acceptable; default stays
    # loopback-only regardless.
    host = os.environ.get('APP_HOST', '127.0.0.1')
    app.run(host=host, port=int(os.environ.get('APP_PORT', '5000')),
            debug=_truthy('APP_DEBUG', '1'), ssl_context=ssl_context)
