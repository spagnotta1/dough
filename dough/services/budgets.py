"""Monthly budget targets, and where each one stands.  [Phase 10]

Allowed:   models, dough.tenancy, sibling services, `recurring`, SQLAlchemy,
           stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`
Exception: `committed_by_category` and `bills_due_remaining` reach
           `dough.services.recurring_service`, whose detection memoizes on
           `flask.g`. Both import it *inside the function*, and every caller
           that wants their answer on a budget row passes it in — so `status()`
           itself stays as context-free and as cheap as it was.

Extracted from `dough/blueprints/budgets.py`, where the whole of it — the
upsert, the two-month spend rollup, the over/warn/ok banding and the month-pace
marker — lived inside one 90-line view function. It moved for the reason
everything in this directory moved: the API needs the same answers, and a second
implementation of "am I over budget" is a second thing that can disagree with
the first about a household's money.

## What `status()` computes, and the one judgement in it

Spend is `max(0, -net)` per category, not the sum of negative amounts. That
matters because a refund posts as a positive row in the same category: a $200
purchase followed by a $200 return should read as zero spent, and summing only
the negatives would report $200. Netting is what makes a returned item stop
counting against a budget.

The `'both'` pseudo-account is accumulated alongside the real ones rather than
derived afterwards, because a budget may be set against a single account *or*
against everything, and the two are different rows with different limits.

## Why `spend_by_category` takes explicit dates

It is called twice with different windows and once from a caller that has its
own idea of "this month". Reading the clock inside would make the two calls
disagree at a month boundary — a request that starts at 23:59:59 on the last day
would compute "this month" and "last month" on opposite sides of midnight.

## What this module gained when Budgets stopped being a scoreboard

The original module answered one question — "did I overspend in the categories
I remembered to track?" — and a budget is supposed to answer a different one:
*what is my plan for the money coming in?* Three things were missing, and each
is now a function here rather than a number invented in a template.

**`project()` — the page had no tense.** Spend-to-date against a limit is a
report on the past; "at this pace you finish the month at $612 of $500" is the
sentence somebody can still act on. The arithmetic is four lines and it was
already written, in `dough/ai/copilot.py::_project_budgets`, where its own
docstring called it *"the single most useful number on the page"* — and where
nothing reachable from a route ever called it. It lives here now, the copilot
reads it from here, and a projection is no longer a thing only a language model
could have told you.

**`plan()` — nothing on the page knew what the household earns.** A budget
starts at take-home pay; without it, limits summing to $9,000 against $4,200 of
income render as four calm green cards. `plan()` is the anchor: income, what is
planned against it, what is left unallocated, and what is genuinely free to
spend once the bills still to come and the goal contributions are set aside.

**`unplanned()` — spending outside a budget was invisible.** The page looped
over budgets, so a category with real money in it and no limit did not exist
here at all, while the header read "$X of $Y budgeted" in the position where a
reader takes it for total spending. Surfacing the gap is what makes this a plan
for every dollar without asking anyone to learn envelope budgeting.

## Suggestions come from the household's own history

`suggested_limits()` reads the median of the last `SUGGEST_MONTHS` **complete**
months — the current one is excluded, because a limit suggested on the 3rd from
three days of data is a number with no evidence behind it.

The median, not the mean, for the reason `affordability.py` gives: one annual
premium moves a mean enough to suggest a limit nobody needs. And it rounds
**up** (`_round_limit`), because a suggested budget below what the household
actually spends every month is a budget they fail on day one, and the first
failure is where people quit.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func

from dough.services import analytics
from dough.tenancy import get_owned
from recurring import is_excluded_category

__all__ = [
    'ACCOUNT_ANY',
    'alerts',
    'balanced_frame',
    'bills_due_remaining',
    'committed_by_category',
    'delete_budget',
    'income_for',
    'list_budgets',
    'month_window',
    'plan',
    'project',
    'serialize',
    'spend_by_category',
    'status',
    'suggested_limits',
    'unplanned',
    'upsert_budget',
    'upsert_many',
]

#: The pseudo-account meaning "count this category across every account".
ACCOUNT_ANY = 'both'

#: Where the bands sit. Named rather than inline so the page, the API and any
#: future alerting agree on what "warning" means without three copies of `80`.
WARN_AT_PCT = 80
OVER_AT_PCT = 100

#: How many complete months a suggested limit is drawn from.
SUGGEST_MONTHS = 6

#: A category needs spending in this many of those months before a limit is
#: suggested for it. One month is an anecdote, and budgeting a household's
#: one-off furniture purchase as a monthly ceiling teaches them the suggestions
#: are not worth reading.
MIN_MONTHS_FOR_SUGGESTION = 2

#: Below this, an unbudgeted category is noise. A page that asks somebody to
#: set a limit on the $4 they spent at a parking meter is a page they close.
UNPLANNED_MIN_USD = 25.0

#: The application's own placeholder for "not sorted yet" — `Transaction.
#: category`'s default, not a name any household chose. It is never offered a
#: limit and never priced into a suggestion: a ceiling on Uncategorized would
#: be a budget for a filing gap, and it moves the moment those rows are sorted,
#: taking the limit's meaning with it. The money is still real, and the place
#: it is dealt with is the Transactions page.
UNBUDGETABLE_CATEGORY = 'uncategorized'

#: The 50/30/20 frame, as a *starting shape* rather than a verdict — see
#: `balanced_frame`. Needs / wants / savings, as percentages of take-home.
BALANCED_SPLIT = (('needs', 50), ('wants', 30), ('savings', 20))

#: Words that put a category on the "needs" side of that frame, on top of the
#: ones `recurring.is_bill_category` already recognises as obligations. These
#: are the daily necessities rather than the monthly bills — the household
#: still moves anything it disagrees with, which is the whole point of showing
#: the frame as an editable proposal instead of applying it.
ESSENTIAL_CATEGORY_WORDS = {
    'grocery', 'groceries', 'gas', 'fuel', 'transit', 'commute',
    'medical', 'health', 'healthcare', 'pharmacy', 'prescription',
    'transportation',
}


def serialize(budget):
    return {
        'id': budget.id,
        'category': budget.category,
        'account_name': budget.account_name,
        'monthly_limit': float(budget.monthly_limit),
    }


def list_budgets():
    from models import Budget

    return Budget.query.order_by(Budget.category).all()


def upsert_budget(category, account_name, monthly_limit):
    """Set a budget, returning `(budget, created)`.

    Upsert rather than create-or-fail because that is what the form did and what
    a person means: setting a budget for a category they have already budgeted
    is changing the number, not an error. `created` is returned so the API can
    answer 201 or 200 honestly instead of guessing.
    """
    from models import Budget, db

    category = (category or '').strip()
    account_name = (account_name or ACCOUNT_ANY).strip() or ACCOUNT_ANY

    existing = Budget.query.filter_by(category=category,
                                      account_name=account_name).first()
    if existing is not None:
        existing.monthly_limit = monthly_limit
        db.session.commit()
        return existing, False

    budget = Budget(category=category, account_name=account_name,
                    monthly_limit=monthly_limit)
    db.session.add(budget)
    db.session.commit()
    return budget, True


def upsert_many(entries):
    """Set several budgets in **one** commit. Returns `(created, updated)`.

    The builder is the caller: somebody accepting a proposed plan is making one
    decision about twelve categories, not twelve decisions. Committing per row
    would leave a half-applied plan behind if the eighth row failed, and would
    make "undo that" mean something different depending on where it stopped.

    Rows with a limit of zero or less are skipped rather than rejected. The
    builder renders every suggestion with a checkbox and an editable amount;
    clearing the box is how somebody says "not this one", and that is not an
    error worth a flash message.
    """
    from models import Budget, db

    created = updated = 0
    for category, account_name, monthly_limit in entries:
        category = (category or '').strip()
        account_name = (account_name or ACCOUNT_ANY).strip() or ACCOUNT_ANY
        if not category or monthly_limit is None or monthly_limit <= 0:
            continue
        existing = Budget.query.filter_by(category=category,
                                          account_name=account_name).first()
        if existing is not None:
            existing.monthly_limit = monthly_limit
            updated += 1
        else:
            db.session.add(Budget(category=category,
                                  account_name=account_name,
                                  monthly_limit=monthly_limit))
            created += 1
    if created or updated:
        db.session.commit()
    return created, updated


def delete_budget(budget_id):
    """Remove a budget. Returns the row that was deleted."""
    from models import Budget, db

    budget = get_owned(Budget, budget_id)
    db.session.delete(budget)
    db.session.commit()
    return budget


def month_window(today=None):
    """`(month_start, today, prev_start, prev_end)` for the current month.

    One function so the page and the API agree about what "this month" and "the
    month before" mean, including at the awkward edges — the previous month's
    end is the day before this month starts, which is how February gets 28 days
    without anybody writing down that it does.
    """
    today = today or datetime.now()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return month_start, today, prev_start, prev_end


def _as_date(value):
    """Narrow a `datetime` to a `date` before it reaches a `Date` column.

    A fix, not a tidy-up, and the same one `dough/services/transactions.py`
    carries.  [Phase 10] `Transaction.date` is a `Date`, so binding a `datetime`
    makes SQLAlchemy send '2026-07-01 00:00:00.000000', which SQLite compares as
    a *string* against the stored '2026-07-01'. The stored value is shorter and
    sorts first, so `BETWEEN` excluded the window's first day.

    On this function that meant **the 1st of every month was missing from every
    budget's spend**, on the page as well as the API. A budget at 90% of its
    limit was reported as 'warn' when the true figure was 135% and 'danger'.
    Nothing failed; the number was simply wrong, and wrong in the direction that
    reassures.

    Carried verbatim from `dough/blueprints/budgets.py` during the extraction —
    per this directory's rule that an extraction is a pure move — and fixed
    immediately afterwards, once the same bug had been found and understood in
    the transaction filter.
    """
    return value.date() if isinstance(value, datetime) else value


def spend_by_category(start, end):
    """Net spend per `(category, account)` in a window, plus a `'both'` rollup.

    Both bounds inclusive. Refunds net off — see the module docstring. Returns a
    dict keyed by tuple, which is what `status` indexes into; a caller wanting
    something friendlier should shape it rather than this function guessing.
    """
    from models import Transaction, db

    rows = (db.session.query(Transaction.category,
                             Transaction.account_name,
                             func.sum(Transaction.amount).label('net'))
            .filter(Transaction.date.between(_as_date(start), _as_date(end)))
            .group_by(Transaction.category, Transaction.account_name).all())

    totals = {}
    for category, account, net in rows:
        spent = max(0.0, -float(net or 0.0))
        totals[(category, account)] = totals.get((category, account), 0.0) + spent
        totals[(category, ACCOUNT_ANY)] = (
            totals.get((category, ACCOUNT_ANY), 0.0) + spent)
    return totals


def status(today=None, *, committed=None):
    """Every budget with its spend, its band, its trend and where it is heading.

    Returns plain dictionaries carrying the budget's own fields rather than the
    ORM object the template used. The template could hold a model; a JSON
    response cannot, and having the service return two different shapes
    depending on its caller is exactly the split this phase exists to remove.

    `committed` is optional and defaults to *not asked*. Recurring detection
    walks the whole ledger, and this function is what `/api/v1/budgets` and
    `ai_context` call on every request; making it unconditional would put a
    full-ledger scan behind a JSON list. A caller that wants the fixed/flexible
    split — the page does — passes `committed_by_category()` in, and rows that
    were not given one carry `committed: None`, which is different from `0.0`
    and is rendered as "not known" rather than "no bills".
    """
    month_start, now, prev_start, prev_end = month_window(today)

    this_month = spend_by_category(month_start, now)
    last_month = spend_by_category(prev_start, prev_end)

    # How far through the month we are. A budget at 60% means opposite things on
    # the 5th and the 25th, and this is the number that tells them apart.
    days_in_month = monthrange(now.year, now.month)[1]
    month_progress = round(now.day / days_in_month * 100)

    rows = []
    for budget in list_budgets():
        limit = float(budget.monthly_limit)
        spent = this_month.get((budget.category, budget.account_name), 0.0)
        prior = last_month.get((budget.category, budget.account_name), 0.0)
        pct = (spent / limit * 100) if limit > 0 else 0.0
        projected = project(spent, month_progress)
        projected_pct = (projected / limit * 100) if limit > 0 else 0.0
        fixed = None if committed is None else round(
            min(committed.get(budget.category, 0.0), limit), 2)
        rows.append({
            **serialize(budget),
            'spent': round(spent, 2),
            'prior': round(prior, 2),
            'remaining': round(limit - spent, 2),
            'pct': round(pct, 1),
            'state': ('danger' if pct > OVER_AT_PCT
                      else 'warn' if pct > WARN_AT_PCT else 'ok'),
            # None rather than 0 when there is no prior spend. A category first
            # budgeted this month has not gone "up 0%" -- there is nothing to
            # compare against, and showing 0% would read as "unchanged".
            'change_pct': (round((spent - prior) / prior * 100)
                           if prior > 0 else None),
            # Where this lands if the rest of the month looks like the start of
            # it. The band is computed on the projection, not on the spend, so
            # a budget pacing to 140% reads as trouble on the 9th rather than
            # on the 27th when there is nothing left to do about it.
            'projected': projected,
            'projected_pct': round(projected_pct, 1),
            'projected_state': ('danger' if projected_pct > OVER_AT_PCT
                                else 'warn' if projected_pct > WARN_AT_PCT
                                else 'ok'),
            'confidence': _confidence(month_progress),
            # How much of this limit is already spoken for by bills that arrive
            # whether or not anybody decides anything. Capped at the limit: a
            # household whose $200 phone bill sits in a $150 budget has a
            # budgeting problem, not 133% of a budget committed.
            'committed': fixed,
            'flexible': None if fixed is None else round(limit - fixed, 2),
        })

    return {
        'budgets': rows,
        'month_label': now.strftime('%B %Y'),
        'month_progress': month_progress,
        'total_budgeted': round(sum(r['monthly_limit'] for r in rows), 2),
        'total_spent': round(sum(r['spent'] for r in rows), 2),
        'total_projected': round(sum(r['projected'] for r in rows), 2),
    }


# ── Where a budget is heading ───────────────────────────────────────────────

def project(spent, month_progress):
    """Month-end spend at the current pace.

    Moved here from `dough/ai/copilot.py::_project_budgets`, which is the only
    place it had ever been written and was reachable from nothing: the copilot
    method that used it, `budget_coaching()`, had no route and no template. The
    number it produces is arithmetic and belongs beside the spend it is derived
    from, not inside the module that asks a model to describe it.

    The floor of 1% matters. On the 1st of the month `month_progress` rounds to
    3% at most and can be lower; dividing by zero on the first request of the
    month is a 500 on the page somebody opened *because* it is a new month.
    """
    elapsed = max(month_progress or 0, 1) / 100.0
    return round(float(spent) / elapsed, 2)


def _confidence(month_progress):
    """How much a projection made this early is worth.

    Named rather than left implicit so the page can say "early days" instead of
    presenting day-three arithmetic in the same weight as day-twenty-five's.
    """
    if month_progress < 25:
        return 'low'
    return 'moderate' if month_progress < 60 else 'high'


def alerts(today=None, *, rows=None):
    """Budgets worth interrupting somebody about, in the dashboard's shape.

    This exists to delete a second implementation. `dough/blueprints/core.py`
    built its own alert list by normalising spend across the dashboard's
    selected window into a monthly average — so a household viewing "last 90
    days" was told about a different set of budgets than the Budgets page
    showed, from different arithmetic, with the same words. A budget is monthly
    by definition; the window the reader happens to have chosen for a chart is
    not a reason to answer "am I over?" differently.
    """
    rows = status(today)['budgets'] if rows is None else rows
    found = [{
        'category': r['category'],
        'pct': round(r['pct']),
        'level': 'over' if r['state'] == 'danger' else 'warning',
        'spent': r['spent'],
        # Kept under its old name for the dashboard's templates. Here it is the
        # month's own spend rather than a window average, which is what the
        # name meant all along.
        'monthly_avg': r['spent'],
        'limit': r['monthly_limit'],
        'projected': r['projected'],
        'projected_pct': r['projected_pct'],
    } for r in rows if r['monthly_limit'] > 0 and r['spent'] > 0
        and r['state'] != 'ok']
    found.sort(key=lambda a: -a['pct'])
    return found


# ── What the household actually brings in ───────────────────────────────────

def income_for(start, end):
    """Take-home landing in a window, transfers excluded.

    `analytics.period_summary` rather than a query of our own, for the reason
    every module in this directory reads its numbers from there: income shown
    beside a budget and income shown on the dashboard being two different
    figures is worse than either of them being wrong.

    This is money that *arrived* — the brief's "use what you actually get, not
    gross pay" is satisfied by the ledger rather than by asking, because a
    deposit is already net of tax and nobody has to type anything.
    """
    return analytics.period_summary(
        analytics.custom_window(start, end))['income']


def bills_due_remaining(today=None):
    """Recurring charges expected between today and the end of the month.

    The one figure that separates "you have $800 left" from "you have $800 left
    and $740 of rent lands on Thursday". Detection already records
    `next_expected` for every group it finds; this reads it rather than
    re-deriving a cadence.
    """
    from dough.services.recurring_service import detect_recurring_full

    _month_start, now, _prev_start, _prev_end = month_window(today)
    today_d = analytics.as_date(now)
    month_end = today_d.replace(
        day=monthrange(today_d.year, today_d.month)[1])

    detected = detect_recurring_full()
    total, items = 0.0, []
    for kind in ('bills', 'subscriptions'):
        for item in detected.get(kind, []):
            expected = analytics.as_date(item['next_expected'])
            if today_d <= expected <= month_end:
                amount = abs(float(item['monthly_amount']))
                total += amount
                items.append({'description': item['description'],
                              'category': item['category'],
                              'amount': round(amount, 2),
                              'expected_on': expected.isoformat()})
    items.sort(key=lambda i: i['expected_on'])
    return {'total': round(total, 2), 'items': items}


def committed_by_category():
    """`{category: monthly committed}` from detected bills and subscriptions.

    What a budget cannot decide its way out of. Coaching somebody about their
    Utilities budget is noise — the money leaves whatever they resolve — and
    the categories where advice can land are the ones with room left after this
    is subtracted.

    No `today`, unlike its neighbours here. Detection reads the whole ledger and
    dates its own recency window off the newest transaction in it rather than
    off the wall clock, so there is no clock for a caller to pin — and a
    parameter that silently did nothing would be worse than not offering one.
    """
    from dough.services.recurring_service import detect_recurring_full

    detected = detect_recurring_full()
    totals = {}
    for kind in ('bills', 'subscriptions'):
        for item in detected.get(kind, []):
            category = item['category']
            totals[category] = round(
                totals.get(category, 0.0) + abs(float(item['monthly_amount'])),
                2)
    return totals


# ── Suggestions, drawn from the household's own months ──────────────────────

def _budgetable(category):
    """Whether a limit against this category would mean anything.

    Transfers, income and investment movement are money moving rather than
    money spent — excluded by the household's own wording, the same rule the
    recurring detector applies, so a household filing them under 'Transfers'
    and one filing them under 'Internal Transfer' get the same answer.
    `Uncategorized` is excluded separately: it is not a category anybody chose.
    """
    return (not is_excluded_category(category)
            and (category or '').strip().lower() != UNBUDGETABLE_CATEGORY)


def _round_limit(amount):
    """A suggested limit somebody would have written down themselves.

    Rounds **up**, always. A ceiling below what this household spends in a
    normal month is one they break in the first week, and the first broken
    budget is where people decide budgeting is not for them.
    """
    if amount <= 0:
        return 0.0
    step = 5 if amount < 100 else 10 if amount < 500 else 25
    return float(-(-amount // step) * step)


def suggested_limits(today=None, *, months=SUGGEST_MONTHS):
    """`{category: {...}}` — what each category has actually cost per month.

    Complete months only. `analytics.lookback_window` is anchored on the last
    day of the *previous* month rather than on today, so a suggestion offered
    on the 3rd is not the median of three days.
    """
    _month_start, _now, _prev_start, prev_end = month_window(today)
    window = analytics.lookback_window(months, anchor=prev_end)
    series = analytics.monthly_category_series(window.start, window.end)

    per_category = {}
    for month_totals in series.values():
        for category, spent in month_totals.items():
            if spent > 0 and _budgetable(category):
                per_category.setdefault(category, []).append(spent)

    suggestions = {}
    for category, amounts in per_category.items():
        if len(amounts) < MIN_MONTHS_FOR_SUGGESTION:
            continue
        typical = median(amounts)
        suggestions[category] = {
            'category': category,
            'suggested': _round_limit(typical),
            'median': round(typical, 2),
            'highest': round(max(amounts), 2),
            # Both numbers, because "based on 6 of 6 months" and "based on 2 of
            # 6" deserve different amounts of trust and only the reader can
            # decide how much.
            'months_seen': len(amounts),
            'months_examined': len(series) or months,
        }
    return dict(sorted(suggestions.items(),
                       key=lambda kv: -kv[1]['median']))


def unplanned(today=None, *, suggestions=None):
    """Categories with real spending this month and no budget against them.

    The page used to loop over budgets alone, which meant the money outside
    them did not appear — and the summary line, "$X of $Y budgeted", sat where
    a reader takes it for total spending. A household tracking four categories
    out of twenty saw a page of green and concluded it was fine.

    What never appears here is decided by `_budgetable`: money moved between
    two accounts somebody owns is not spending, and `Uncategorized` is a filing
    gap rather than a category, so neither is ever offered a ceiling.
    """
    month_start, now, _prev_start, _prev_end = month_window(today)
    budgeted = {b.category for b in list_budgets()}
    spend = spend_by_category(month_start, now)
    suggestions = (suggested_limits(today) if suggestions is None
                   else suggestions)

    rows = []
    for (category, account), spent in spend.items():
        if account != ACCOUNT_ANY or category in budgeted:
            continue
        if spent < UNPLANNED_MIN_USD or not _budgetable(category):
            continue
        hint = suggestions.get(category) or {}
        rows.append({
            'category': category,
            'spent': round(spent, 2),
            # Falls back to this month's own spend, rounded up, when there is
            # not enough history to take a median from. A new category still
            # gets a one-tap limit rather than an empty box.
            'suggested': hint.get('suggested') or _round_limit(spent),
            'median': hint.get('median'),
            'months_seen': hint.get('months_seen', 0),
        })
    rows.sort(key=lambda r: -r['spent'])
    return rows


def balanced_frame(today=None, *, suggestions=None):
    """A 50/30/20 starting shape, priced against this household's own spending.

    Offered as a *frame*, never applied. The split is a widely taught default,
    not a fact about anybody's life, and the categories it sorts are free text
    the household wrote — so every row comes back with the bucket it landed in
    and an amount the user can move before anything is saved.

    Returns `available: False` rather than a shape built on nothing when there
    is no income to divide, because 50% of an unknown number is not advice.
    """
    from recurring import is_bill_category

    _month_start, _now, prev_start, prev_end = month_window(today)
    basis = income_for(prev_start, prev_end)
    suggestions = (suggested_limits(today) if suggestions is None
                   else suggestions)
    if basis <= 0 or not suggestions:
        return {'available': False,
                'reason': ('no income seen last month' if basis <= 0
                           else 'not enough spending history')}

    rows = []
    for category, hint in suggestions.items():
        essential = (is_bill_category(category)
                     or bool(ESSENTIAL_CATEGORY_WORDS
                             & set(category.lower().replace('/', ' ').split())))
        rows.append({**hint, 'bucket': 'needs' if essential else 'wants'})

    buckets = []
    for name, pct in BALANCED_SPLIT:
        target = round(basis * pct / 100.0, 2)
        in_bucket = [r for r in rows if r['bucket'] == name]
        buckets.append({
            'bucket': name,
            'target_pct': pct,
            'target': target,
            'suggested_total': round(sum(r['suggested'] for r in in_bucket), 2),
            'categories': in_bucket,
        })
    return {'available': True, 'basis': round(basis, 2), 'buckets': buckets}


# ── The plan the budgets sit inside ─────────────────────────────────────────

def plan(today=None, *, status_rows=None, suggestions=None):
    """Income, what is planned against it, and what is genuinely free.

    The anchor the page never had. Every figure the bottom line rests on is
    returned in `safe_to_spend['components']` — the same "show the arithmetic"
    rule `affordability.py` holds to, and for the same reason: a single number
    presented without its basis is one the reader either over-trusts or
    ignores, and both are worse than a number they can check.

    The basis is **last complete month's** take-home, not this month's so far.
    On the 4th, income-received-so-far is near zero for anybody paid monthly,
    and a plan measured against it would report every household as
    catastrophically over-committed once a month, every month.

    Two deliberate conservatisms, both erring the same way. Goal contributions
    are subtracted at their full monthly target even where this month's has
    already gone in, and a bill is counted from the day it is expected rather
    than pro-rated. Both understate what is free, and that is the direction to
    be wrong in: a safe-to-spend figure that turns out to have been generous
    costs somebody an overdraft, and one that turns out to have been cautious
    costs them a pleasant surprise.
    """
    month_start, now, prev_start, prev_end = month_window(today)
    rows = status(today)['budgets'] if status_rows is None else status_rows

    take_home = income_for(month_start, now)
    prior_take_home = income_for(prev_start, prev_end)
    if prior_take_home > 0:
        basis, basis_source = prior_take_home, 'last_month'
    elif take_home > 0:
        basis, basis_source = take_home, 'this_month'
    else:
        basis, basis_source = 0.0, 'none'

    planned = round(sum(r['monthly_limit'] for r in rows), 2)
    outside = unplanned(today, suggestions=suggestions)
    unplanned_spend = round(sum(r['spent'] for r in outside), 2)
    spent_total = analytics.period_summary(
        analytics.custom_window(month_start, now))['spending']

    bills = bills_due_remaining(today)
    goal_commitment = _goal_commitment(today)

    components = [
        {'label': 'Take-home', 'amount': round(basis, 2), 'sign': 1},
        {'label': 'Spent so far this month', 'amount': round(spent_total, 2),
         'sign': -1},
        {'label': 'Bills still to come', 'amount': bills['total'], 'sign': -1},
        {'label': 'Goal contributions', 'amount': goal_commitment, 'sign': -1},
    ]
    free = round(basis - spent_total - bills['total'] - goal_commitment, 2)

    return {
        'take_home': round(take_home, 2),
        'take_home_prior': round(prior_take_home, 2),
        'basis': round(basis, 2),
        'basis_source': basis_source,
        'planned': planned,
        'planned_pct': (round(planned / basis * 100, 1) if basis > 0 else None),
        # Negative means the limits promise more than the household earns,
        # which is the one thing a budget page must never render as calm green.
        'unallocated': round(basis - planned, 2) if basis > 0 else None,
        'over_committed': bool(basis > 0 and planned > basis),
        'spent_total': round(spent_total, 2),
        'unplanned_spend': unplanned_spend,
        'unplanned_count': len(outside),
        'unplanned': outside,
        'bills_due': bills,
        'safe_to_spend': {
            'amount': free,
            'components': components,
            'confidence': _confidence(
                round(now.day / monthrange(now.year, now.month)[1] * 100)),
        },
    }


def _goal_commitment(today=None):
    """What active goals ask for each month.

    Read from `dough.services.goals` rather than summed here. A household's
    savings plan having two definitions — one on the Goals page and one inside
    safe-to-spend — is the same class of bug as two answers to "am I over
    budget", and this module is in the middle of deleting one of those.
    """
    from dough.services import goals

    return goals.summary(today=analytics.as_date(today) if today else None
                         )['monthly_commitment']
