"""`/` — the marketing page for strangers, the dashboard for everybody else.

## One route, two pages

`/` is the product's front door in both senses, and Phase 10.5 gave it a second
audience: somebody who has never signed in and needs to be told what this is.

The obvious alternative was to move the dashboard to `/dashboard` and give the
landing page `/` outright. It was rejected because of what it costs elsewhere:
`redirect('/')` appears at the end of sign-in, setup and invitation redemption,
`url_for('core.dashboard')` resolves to `/` in every template and test, and
`tests/test_url_map_snapshot.py` freezes the URL surface precisely so a change
like that has to be deliberate. Every one of those is a place the move could be
got subtly wrong, and the benefit is a tidier routing table.

So the route branches instead. The *whole* branch is the two lines at the top of
`dashboard()`, and it happens before any query runs — which matters more than it
looks: this view is now `@public`, so an anonymous request reaches it with **no
household bound** (`_household_for_request` returns None for a caller with no
session), and every `Transaction.query` below would raise `TenantContextMissing`.
The landing page touches no tenant data at all, and the early return is what
guarantees it never gets the chance to.

## Why `@public` is safe on a view that renders the dashboard

`@public` means "may run without a session", not "shows data without a session".
The marker suppresses `_require_login`'s redirect; it does not suppress tenancy.
An anonymous caller gets the landing page and returns before touching the ORM,
and a caller *with* a session has already been through `_enforce_session_lifetime`
— so a session that is expired, or whose credential generation has been
superseded, is cleared and redirected before this function runs at all.
"""

from datetime import date, datetime, timedelta

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func

from dough.auth import public
from dough.services.accounts import ledger_account_names
from dough.services.networth import compute_net_worth, portfolio_snapshot
from dough.services.recurring_service import detect_recurring_full

import dashboard_intel
from models import AppUser, Budget, InstitutionConnection, Transaction, db

bp = Blueprint('core', __name__)


def _is_anonymous():
    """Should this request see the marketing page rather than the dashboard?

    Two conditions, and the second is what keeps ~890 pre-existing tests green.

    With `AUTH_ENABLED` off — TestingConfig, and any installation that has
    deliberately turned authentication off — there is no such thing as an
    anonymous visitor: `_household_for_request` hands every request the default
    household, and the dashboard is what `/` has always meant. Answering
    "anonymous" there would replace the dashboard with a marketing page for every
    test in the suite that fetches `/`, and for every user of an auth-off
    install.

    `session['user_id']` rather than `current_user()` deliberately. This runs on
    every dashboard load, and `current_user()` performs a bearer lookup and a
    user query; the question here is only "is there a session", and the requests
    that have one have already been validated by `_enforce_session_lifetime`
    before reaching this view.
    """
    if not current_app.config.get('AUTH_ENABLED', True):
        return False
    return not session.get('user_id')


@bp.route('/')
@public
def dashboard():
    if _is_anonymous():
        # An installation with no accounts at all is not a stranger's visit --
        # it is an unfinished install, and the only useful thing anybody can do
        # is create the first owner. `/setup` is that, and it refuses to run
        # once an account exists, so this cannot become a way in later.
        #
        # Without this, a fresh install answers `/` with a marketing page whose
        # "Sign in" leads to `/login`, which redirects to `/setup` anyway -- the
        # same destination with a page in front of it, shown to somebody who is
        # not an audience for marketing because they are the person installing
        # the software.
        #
        # `.first()` rather than `.count()`: the question is existence, and the
        # query runs on every anonymous view of `/`. `auth.login` asks it the
        # same way on every sign-in page load.
        if AppUser.query.first() is None:
            return redirect(url_for('auth.setup'))
        return render_template(
            'landing.html',
            # Whether the "Create account" button leads anywhere. A closed
            # instance still renders the page and still shows the button --
            # `/register` explains itself there. Passing the flag lets the page
            # lead with "Sign in" instead, which is the action that works.
            registration_open=current_app.config.get('ALLOW_REGISTRATION', False),
            # Social proof, and the only copy on the page that cannot be
            # checked against this repository. All three are empty by default
            # and their section renders nothing -- see the MARKETING_* block in
            # config.py.
            testimonials=current_app.config.get('MARKETING_TESTIMONIALS', []),
            stats=current_app.config.get('MARKETING_STATS', []),
            press=current_app.config.get('MARKETING_PRESS', []))

    start_date_str = request.args.get('start_date') or session.get('start_date')
    end_date_str = request.args.get('end_date') or session.get('end_date')
    # `or 'both'`, not `session.get('account', 'both')`, and it is the whole
    # bug behind "I filtered the transactions list and the dashboard went
    # blank".  [UAT round 1]
    #
    # Both pages share one session key for the account filter and spell
    # "everything" differently: this page says 'both', the transactions filter
    # form says nothing at all — its "All Accounts" option has value="", which
    # `sticky_filter` turns into None and stores. So every submission of that
    # form — including the date presets, which submit the whole form — left
    # `session['account'] = None`, the key was *present*, and the default
    # never applied. `account_filter != 'both'` was then true, and the page
    # queried `account_name == None`, which matches nothing anywhere.
    #
    # It read as a date bug because the dates are what the reader had just
    # touched, and the account select gave nothing away: with the value None
    # no <option> is selected, so the browser shows the first one and the
    # panel says "Both accounts" while the query says otherwise.
    account_filter = request.args.get('account') or session.get('account') or 'both'

    # `date`, never `datetime`, and it is a fix rather than tidying — the same
    # one `services/transactions.build_transaction_query` carries, which is why
    # the transactions list showed four August rows while this page counted one.
    #
    # `Transaction.date` is a `Date` column. Compared against a `datetime`,
    # SQLAlchemy types the bind by the *value* rather than the column and sends
    # '2026-08-01 00:00:00.000000', which SQLite compares as a string against
    # the stored '2026-08-01'. The stored value is shorter and sorts first, so
    # `date >= start` is False for every transaction falling on the start date
    # itself: the window silently drops its first day. The end boundary
    # survives only by accident of the same rule.
    #
    # Every window on this page is derived from these two — the comparison
    # period, the balance history, both monthly trends, the category trend —
    # so the whole dashboard was reading a window one day short, and
    # disagreeing with the transactions page about the same dates.

    # One reading of the clock for the whole view. Three things below are
    # relative to today — the default window, whether the window on screen
    # *is* that default, and where the date chip's ✕ leads — and two calls a
    # few microseconds apart can straddle midnight into three different
    # answers. That is a bug that only ever happens in production.
    today_date = date.today()

    # Whether the window on screen is one somebody chose. The filter bar shows a
    # removable chip per *applied* filter and hides "Clear all" when there are
    # none, and month-to-date is the absence of a date filter rather than a date
    # filter that happens to say August — a chip for it would offer to remove
    # the state it is already in. Captured here because two lines below the two
    # cases are indistinguishable: both end up holding a pair of ISO strings.
    date_is_default = not (start_date_str and end_date_str)

    if not start_date_str or not end_date_str:
        end_date = today_date
        start_date = end_date.replace(day=1)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    else:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    # A default window that lands on nothing snaps to the newest month that
    # has data.  [UAT round 1]
    #
    # Reported as "none of the data visualizations are appearing", and the
    # dashboard was working exactly as written: the default window is
    # month-to-date, the newest transaction was six days older than the 1st,
    # so every series came back empty and every panel drew an empty box. It is
    # the ordinary state of this application on the early days of a month, and
    # for any household whose last sync is more than a few days old — which is
    # every household that connected an institution and then waited.
    #
    # Month-to-date stays the default because it is the right answer whenever
    # the current month has anything in it; this only rescues the case where it
    # does not.
    #
    # **Only when the dates were not asked for.** A window someone typed, or
    # followed a link to, must answer for itself — "no transactions between
    # these dates" is true and useful, and silently relocating them to a
    # different month would make the date inputs lie about what is on screen.
    # `request.args` rather than the resolved value is what draws that line:
    # session-restored dates are the app's memory, not a choice being made now,
    # so a household stuck on an empty month gets rescued on its next visit
    # instead of having to find "Clear filters".
    if not (request.args.get('start_date') or request.args.get('end_date')):
        window = Transaction.query.filter(
            Transaction.date.between(start_date, end_date))
        if window.first() is None:
            newest = db.session.query(func.max(Transaction.date)).scalar()
            # `func.max` over a Date column comes back as a date from SQLite,
            # but a string from a raw text-typed row; normalise rather than
            # trusting the driver, because the failure would be a TypeError on
            # the dashboard rather than anything visible in a test.
            if isinstance(newest, str):
                newest = datetime.strptime(newest[:10], '%Y-%m-%d').date()
            if newest is not None and newest < start_date:
                end_date = newest
                start_date = newest.replace(day=1)
                start_date_str = start_date.strftime('%Y-%m-%d')
                end_date_str = end_date.strftime('%Y-%m-%d')

    # A window that *is* month-to-date is the default however it arrived. The
    # "This Month" preset submits the dates explicitly, like every other
    # preset, and a chip appearing for it would say the page is filtered to
    # the state it opens in. Judged on the value rather than on where it came
    # from, which is also what keeps a link somebody shared from reading
    # differently to the page they shared it from.
    if start_date == today_date.replace(day=1) and end_date == today_date:
        date_is_default = True

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
                     .filter(Transaction.date >= (today - timedelta(days=90)).date())
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

    # Every category the account has ever used, ranked so that the ones which
    # actually reach a chart hold the eight distinct hues and the long tail
    # shares the neutral. The client walks this list to assign palette slots.
    #
    # Ranked on the WHOLE history, never the filtered window: that is what
    # lets the mapping stay fixed while a filter changes which categories are
    # on screen. Alphabetical ordering was worse — it handed the eight hues to
    # whichever categories started with early letters.
    #
    # ## Ranked by SPENDING, not by gross volume  [UAT round 1]
    #
    # Ranking on `abs(amount)` over every row was the next version of the same
    # bug, and it was reported from the running app: "Spending by category over
    # time" drew three of its six series in the identical overflow gray.
    #
    # The charts that use this palette are spending charts. They filter to
    # `amount < 0` and, on the combined view, drop transfers. But the ranking
    # counted every row, so `Income` and `Transfer` — the two largest movers in
    # almost any ledger, and the two that a spending chart can never draw —
    # took the first two slots. A quarter of a palette that is only eight wide
    # went to categories guaranteed not to appear, pushing three real series
    # past the end of it and into the shared neutral.
    #
    # So the ranking now uses the same measure the charts select on: outbound
    # money, transfers excluded. Non-spending categories keep a slot, ordered
    # after every spender, because `Income` still needs a stable identity in
    # the breakdown grid — it just has no claim on a hue a spending chart needs.
    _transfer_names = ['transfer', 'transfers']
    spend_rank = [row[0] for row in db.session.query(
        Transaction.category, func.sum(func.abs(Transaction.amount)).label('vol')
    ).filter(Transaction.category.isnot(None), Transaction.amount < 0,
             ~func.lower(Transaction.category).in_(_transfer_names))
     .group_by(Transaction.category)
     .order_by(func.sum(func.abs(Transaction.amount)).desc()).all()]

    rest_rank = [row[0] for row in db.session.query(
        Transaction.category, func.sum(func.abs(Transaction.amount)).label('vol')
    ).filter(Transaction.category.isnot(None))
     .group_by(Transaction.category)
     .order_by(func.sum(func.abs(Transaction.amount)).desc()).all()]

    _seen = set(spend_rank)
    all_categories = spend_rank + [c for c in rest_rank if c not in _seen]

    # A dashboard with nothing on it has two different causes, and they want
    # opposite advice. An empty *window* is a filter question — widen the dates.
    # An empty *ledger* is a first run, and the answer there is to link an
    # institution: a connection backfills history and then keeps refilling it
    # every 12 hours, where a statement upload is one snapshot the person has
    # to remember to repeat. Telling a brand-new account to go find a CSV was
    # pointing the one moment they are most willing to set this up at the
    # slower of the two paths.
    #
    # `.first()`, not `.count()`: the question is existence. It only runs when
    # the selected window came back empty, so a dashboard with data pays
    # nothing for it.
    ledger_empty = not transactions and Transaction.query.first() is None
    # A linked institution whose first sync has not landed yet is still an
    # empty ledger, but it must not be asked to connect again — that reads as
    # the connection having failed.
    awaiting_first_sync = bool(ledger_empty and InstitutionConnection.query
                               .filter(InstitutionConnection.status != 'disconnected')
                               .first())

    # The accounts the filter bar offers. Read from the ledger rather than
    # written into the template: the account control listed a hardcoded
    # "Checking" and "Savings" for as long as it existed, which is two names
    # this household may not have and no name for the ones it does — a Visa
    # sitting in every chart with no way to filter to it.
    #
    # `account_name` is what this page filters on, so the ledger's own distinct
    # names are exactly the set of values that can match anything. The active
    # filter is unioned in so a window whose account has since gone quiet still
    # shows the truth rather than falling back to the first option.
    account_options = ledger_account_names()
    if account_filter != 'both' and account_filter not in account_options:
        account_options = sorted(account_options + [account_filter])

    return render_template('dashboard.html',
                           all_categories=all_categories,
                           account_options=account_options,
                           date_is_default=date_is_default,
                           # Where "remove the date filter" goes. The default
                           # window spelled out rather than left off the link:
                           # the dates are sticky in the session, so a link
                           # that simply omits them restores the very window
                           # it is removing.
                           default_start=today_date.replace(day=1).strftime('%Y-%m-%d'),
                           default_end=today_date.strftime('%Y-%m-%d'),
                           period_label=period_label,
                           compare_label=compare_label,
                           has_data=bool(transactions),
                           ledger_empty=ledger_empty,
                           awaiting_first_sync=awaiting_first_sync,
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
