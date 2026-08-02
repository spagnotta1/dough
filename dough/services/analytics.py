"""The aggregation layer every analytic in Phase 11 reads from.

Seven features arrived at once that all needed the same four numbers — what a
window earned, what it spent, how that split by category, and who was paid.
Written per feature that is four implementations of "is a transfer spending?"
and four chances to disagree with the dashboard. Written once it is this module,
and the disagreement becomes impossible rather than unlikely.

## Everything here is a GROUP BY, never a row scan

`period_summary` over a decade of transactions costs the same as over a month:
one aggregate query returning one row per category. That is not an optimisation
applied afterwards — it is the reason the module exists in this shape. The
version that loaded `Transaction.query.filter(...).all()` and summed in Python
is what `copilot_context` still does, and it is affordable there only because
that window is one month. An analytic that looks back twenty-four months cannot
pay it, and a copilot that answers in eight seconds is a copilot nobody asks a
second question.

`tests/test_analytics.py::test_period_summary_issues_one_query` holds this: the
query count is asserted, not the wall clock, so the guarantee survives a fast
laptop.

## Transfers

Money moved between two accounts the household owns is not spending, and every
surface in this app has to decide what to do about it. The decision is a
parameter here rather than a policy, because both answers are right in different
places: across accounts a transfer double-counts money that only moved, and
within one account the money really did leave. `finance_context` already states
this rule in prose to the model; this module is the same rule in code, which is
what stops the prose and the arithmetic drifting.

The default is `include_transfers=False` — the cross-account reading — because
that is what every caller in Phase 11 wants and a default that is wrong for the
common case is a default that gets forgotten.

## Household scoping

Nothing here filters on `household_id`. It does not have to: every query goes
through `Transaction.query`, which is `TenantScopedQuery`, and `dough/tenancy.py`
applies the predicate in a `do_orm_execute` hook. That is the same guarantee the
rest of `dough/services/` runs on. No function here takes a household argument,
so there is no parameter a caller could pass the wrong value to —
`tests/test_analytics.py::test_analytics_never_crosses_a_household` asserts it
behaviourally, from two households with deliberately identical figures.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Float, case, cast, func

from models import Transaction, db

#: Categories that represent movement between the household's own accounts
#: rather than money entering or leaving it. Compared case-insensitively, and
#: both spellings are in the wild — the CSV importer writes one and the sync
#: pipeline the other.
TRANSFER_CATEGORIES = ('transfer', 'transfers')

#: How many merchants a rollup returns before it stops being a summary. Thirty
#: is `finance_context.CHAT_TOP_MERCHANT_LIMIT`; the number is repeated rather
#: than imported because that constant sizes a *context window* and this one
#: sizes a *query*, and a future change to either has no business moving the
#: other.
DEFAULT_MERCHANT_LIMIT = 30


# ── Periods ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Window:
    """A closed date range with a name a person would recognise.

    Both ends are inclusive. Half-open would be the more usual choice for a
    range type, and it is the wrong one here: every window in this application
    is spoken about as well as computed with, and "March 1 to April 1" is not
    what anybody means by March. The inclusive end is what lets `label` be
    generated rather than passed in.
    """

    start: date
    end: date
    label: str
    kind: str = 'custom'

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def months(self):
        """Length in months, as a float, for rates like "per month".

        Never zero — a one-day window would otherwise turn every per-month
        figure into a division by zero, and reporting a single Tuesday's
        groceries as an infinite monthly rate is worse than reporting it as a
        month's worth.
        """
        return max(self.days / 30.44, 0.01)

    def as_dict(self):
        return {'start': self.start.isoformat(), 'end': self.end.isoformat(),
                'label': self.label, 'kind': self.kind, 'days': self.days}


def month_bounds(anchor):
    """First and last day of the month containing `anchor`."""
    anchor = as_date(anchor)
    last = calendar.monthrange(anchor.year, anchor.month)[1]
    return date(anchor.year, anchor.month, 1), date(anchor.year, anchor.month, last)


def quarter_bounds(anchor):
    """First and last day of the calendar quarter containing `anchor`."""
    anchor = as_date(anchor)
    first_month = 3 * ((anchor.month - 1) // 3) + 1
    start = date(anchor.year, first_month, 1)
    end_month = first_month + 2
    return start, date(anchor.year, end_month,
                       calendar.monthrange(anchor.year, end_month)[1])


def year_bounds(anchor):
    anchor = as_date(anchor)
    return date(anchor.year, 1, 1), date(anchor.year, 12, 31)


def resolve_window(kind='month', anchor=None):
    """The named period containing `anchor` (default: today).

    `kind` is 'month', 'quarter' or 'year'. Anything else raises rather than
    silently defaulting: a typo'd period name that quietly returns this month
    produces a comparison whose labels look right and whose numbers are not.
    """
    anchor = as_date(anchor or date.today())
    if kind == 'month':
        start, end = month_bounds(anchor)
    elif kind == 'quarter':
        start, end = quarter_bounds(anchor)
    elif kind == 'year':
        start, end = year_bounds(anchor)
    else:
        raise ValueError(f'unknown period kind: {kind!r}')
    return Window(start, end, label_for(start, end), kind)


def preceding_window(window):
    """The window immediately before `window`, of the same shape.

    For a named period this is the previous calendar one, so the month before
    March is February and not "the 31 days before March" — those differ, and the
    difference is what makes a February comparison read as wrong. For a custom
    range it is the same number of days ending the day before it starts, which
    is the only definition available.
    """
    if window.kind == 'month':
        start, _ = month_bounds(window.start - timedelta(days=1))
        return resolve_window('month', start)
    if window.kind == 'quarter':
        start, _ = quarter_bounds(window.start - timedelta(days=1))
        return resolve_window('quarter', start)
    if window.kind == 'year':
        return resolve_window('year', date(window.start.year - 1, 1, 1))
    end = window.start - timedelta(days=1)
    start = end - timedelta(days=window.days - 1)
    return Window(start, end, label_for(start, end), 'custom')


def lookback_window(months, anchor=None):
    """The last `months` whole months, ending with the month containing `anchor`.

    `lookback_window(6)` in August covers March through August — six month keys,
    not seven. That off-by-one is worth a named function rather than a
    subtraction at each call site: every trend, baseline and rolling average in
    this package is sized in months, and a window one month longer than the
    caller believes silently shifts every slope it feeds.

    The window ends on `anchor` itself rather than at the end of its month, so a
    lookback taken mid-month does not include a fortnight of future dates that
    no transaction can fall into and every average would then divide by.
    """
    if months < 1:
        raise ValueError(f'lookback needs at least one month, got {months!r}')
    anchor = as_date(anchor or date.today())
    year, month = anchor.year, anchor.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    start = date(year, month, 1)
    return Window(start, anchor, label_for(start, anchor), 'custom')


def custom_window(start, end):
    """A caller-supplied range, normalised and labelled.

    Reversed ends are swapped rather than rejected. A date picker that hands
    back the two clicks in the order they happened is a real thing, and an
    exception there is a 500 on a page where the user did nothing wrong.
    """
    start, end = as_date(start), as_date(end)
    if end < start:
        start, end = end, start
    return Window(start, end, label_for(start, end), 'custom')


def label_for(start, end):
    """"March 2026", "Q1 2026", "2026", or "Mar 1 – Jun 30, 2026"."""
    if (start, end) == month_bounds(start):
        return f'{start:%B %Y}'
    if (start, end) == quarter_bounds(start):
        return f'Q{(start.month - 1) // 3 + 1} {start.year}'
    if (start, end) == year_bounds(start):
        return str(start.year)
    if start.year == end.year:
        return f'{start:%b} {start.day} – {end:%b} {end.day}, {end.year}'
    return f'{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}'


def as_date(value):
    """A `date` from a date, datetime or ISO-ish string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    raise TypeError(f'unsupported date value: {value!r}')


def month_key(value):
    """'YYYY-MM' — the key every monthly series in this package is keyed by."""
    return f'{as_date(value):%Y-%m}'


def month_keys_between(start, end):
    """Every 'YYYY-MM' from `start` to `end` inclusive, in order.

    Series are reindexed onto this rather than onto the months that happen to
    have rows. A month with no transactions is a real and meaningful zero — it
    is what a gap in a trend line means — and letting SQL's `GROUP BY` decide
    the axis silently closes those gaps, turning "you spent nothing in August"
    into "August did not happen".
    """
    start, end = as_date(start), as_date(end)
    keys, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f'{year:04d}-{month:02d}')
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return keys


# ── The query primitives ────────────────────────────────────────────────────

def _amount():
    """`Transaction.amount` as a float in SQL.

    The column is `Numeric(10, 2)`, which SQLAlchemy hands back as `Decimal`.
    Summing Decimals is correct and is what the ledger does; every consumer here
    then divides them by floats to get rates and percentages, and
    `Decimal / float` raises `TypeError`. Casting once in SQL is cheaper and far
    harder to forget than a `float()` at each of the thirty call sites.
    """
    return cast(Transaction.amount, Float)


def _is_transfer():
    """SQL predicate: this row is movement between the household's own accounts."""
    return func.lower(func.trim(Transaction.category)).in_(TRANSFER_CATEGORIES)


def base_query(start=None, end=None, *, account=None, include_transfers=False):
    """The filtered row set every aggregate below is built on.

    Returns a SQLAlchemy `Query` on `Transaction`, already household-scoped by
    `TenantScopedQuery`. Exposed rather than kept private because
    `finsearch` needs to page the underlying rows with exactly these filters,
    and re-deriving them there is how the search results and the totals above
    them stop agreeing.
    """
    query = Transaction.query
    if start is not None:
        query = query.filter(Transaction.date >= as_date(start))
    if end is not None:
        query = query.filter(Transaction.date <= as_date(end))
    if account and account not in ('both', 'all'):
        query = query.filter(Transaction.account_name == account)
    if not include_transfers:
        query = query.filter(~_is_transfer())
    return query


def _aggregate(columns, start, end, account, include_transfers, group_by=None,
               order_by=None, limit=None, sign=None):
    """One GROUP BY over the filtered rows. The shared body of every rollup."""
    query = db.session.query(*columns)
    if start is not None:
        query = query.filter(Transaction.date >= as_date(start))
    if end is not None:
        query = query.filter(Transaction.date <= as_date(end))
    if account and account not in ('both', 'all'):
        query = query.filter(Transaction.account_name == account)
    if not include_transfers:
        query = query.filter(~_is_transfer())
    if sign == 'spending':
        query = query.filter(Transaction.amount < 0)
    elif sign == 'income':
        query = query.filter(Transaction.amount > 0)
    for column in (group_by or []):
        query = query.group_by(column)
    if order_by is not None:
        query = query.order_by(order_by)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def period_summary(window, *, account=None, include_transfers=False):
    """Income, spending, net flow and the category split for one window.

    **One query.** Income and spending come back from the same aggregate via
    conditional sums rather than two round trips, because this is the single
    most-called function in the package — `periods`, `health`, `proactive` and
    `ai_context` each call it at least twice — and two queries here is two
    hundred over a briefing.

    `spending` is returned positive. Every consumer displays it as a magnitude
    and the sign flips were a steady source of bugs; the ledger keeps the sign,
    and this layer, which exists to be read, does not.
    """
    rows = _aggregate(
        [Transaction.category,
         func.sum(case((Transaction.amount < 0, -_amount()), else_=0.0)).label('spent'),
         func.sum(case((Transaction.amount > 0, _amount()), else_=0.0)).label('earned'),
         func.count(Transaction.id).label('n')],
        window.start, window.end, account, include_transfers,
        group_by=[Transaction.category])

    by_category, income, spending, count = {}, 0.0, 0.0, 0
    for row in rows:
        spent = round(float(row.spent or 0.0), 2)
        income += float(row.earned or 0.0)
        spending += spent
        count += int(row.n or 0)
        if spent:
            by_category[row.category] = spent

    return {
        'window': window.as_dict(),
        'income': round(income, 2),
        'spending': round(spending, 2),
        'net': round(income - spending, 2),
        'savings_rate': (round((income - spending) / income * 100.0, 1)
                         if income > 0 else None),
        'transaction_count': count,
        'by_category': dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
    }


def category_totals(window, *, account=None, include_transfers=False):
    """Spending per category for one window, largest first. Positive amounts."""
    return period_summary(window, account=account,
                          include_transfers=include_transfers)['by_category']


def merchant_totals(window, *, limit=DEFAULT_MERCHANT_LIMIT, account=None,
                    include_transfers=False):
    """Who was paid the most in this window.

    Grouped on the raw description rather than a normalised merchant key. The
    normaliser in `recurring.py` exists to cluster *one* payee's charges across
    the small variations a bank adds, and it is deliberately aggressive about
    it — good for detecting a subscription, wrong for a spend ranking, where it
    would merge two genuinely different merchants that share a prefix and report
    a total neither of them charged.
    """
    rows = _aggregate(
        [Transaction.description,
         func.sum(-_amount()).label('total'),
         func.count(Transaction.id).label('n'),
         func.max(Transaction.date).label('last_seen')],
        window.start, window.end, account, include_transfers,
        group_by=[Transaction.description],
        order_by=func.sum(_amount()).asc(), limit=limit, sign='spending')
    return [{'description': r.description,
             'total': round(float(r.total or 0.0), 2),
             'transactions': int(r.n or 0),
             'last_seen': as_date(r.last_seen).isoformat() if r.last_seen else None}
            for r in rows]


def largest_purchases(window, *, limit=10, account=None,
                      include_transfers=False):
    """The biggest individual outflows in the window.

    Rows, not a rollup: "your biggest purchase" is a specific line the user can
    recognise, and a category total cannot be recognised.
    """
    rows = (base_query(window.start, window.end, account=account,
                       include_transfers=include_transfers)
            .filter(Transaction.amount < 0)
            .order_by(Transaction.amount.asc())
            .limit(limit).all())
    return [{'date': t.date.isoformat(), 'description': t.description,
             'amount': round(abs(float(t.amount)), 2), 'category': t.category,
             'account': t.account_name, 'id': t.id}
            for t in rows]


def monthly_series(start, end, *, account=None, include_transfers=False):
    """Income and spending per month across a range, gaps included.

    The axis comes from `month_keys_between`, not from the rows — see its
    docstring for why a missing month has to survive as a zero.
    """
    rows = _aggregate(
        [func.strftime('%Y-%m', Transaction.date).label('month'),
         func.sum(case((Transaction.amount < 0, -_amount()), else_=0.0)).label('spent'),
         func.sum(case((Transaction.amount > 0, _amount()), else_=0.0)).label('earned')],
        start, end, account, include_transfers, group_by=['month'])
    found = {r.month: (round(float(r.spent or 0.0), 2),
                       round(float(r.earned or 0.0), 2)) for r in rows}
    series = {}
    for key in month_keys_between(start, end):
        spent, earned = found.get(key, (0.0, 0.0))
        series[key] = {'spending': spent, 'income': earned,
                       'net': round(earned - spent, 2)}
    return series


def monthly_category_series(start, end, *, account=None,
                            include_transfers=False):
    """`{'YYYY-MM': {category: spent}}` — the series behind every trend.

    Only months with spending appear per category; a category absent from a
    month means zero there. `trends.py` reindexes onto the full axis, which is
    the one place that needs the zeros made explicit, and carrying them here
    would multiply the size of a twenty-four-month context by the number of
    categories for no reader's benefit.
    """
    rows = _aggregate(
        [func.strftime('%Y-%m', Transaction.date).label('month'),
         Transaction.category,
         func.sum(-_amount()).label('total')],
        start, end, account, include_transfers,
        group_by=['month', Transaction.category], sign='spending')
    series = {}
    for row in rows:
        total = round(float(row.total or 0.0), 2)
        if total:
            series.setdefault(row.month, {})[row.category] = total
    return {month: dict(sorted(cats.items(), key=lambda kv: -kv[1]))
            for month, cats in sorted(series.items())}


def coverage():
    """What ledger history actually exists, in one query.

    Every surface that says "I can only see back to March" reads this. Saying it
    from the newest slice of transactions instead is the bug it exists to
    prevent — `finance_context` carries the same note in prose for the same
    reason.
    """
    first, last, count = db.session.query(
        func.min(Transaction.date), func.max(Transaction.date),
        func.count(Transaction.id)).one()
    accounts = sorted(name for (name,) in
                      db.session.query(Transaction.account_name).distinct().all()
                      if name)
    return {'first_transaction': as_date(first).isoformat() if first else None,
            'last_transaction': as_date(last).isoformat() if last else None,
            'total_transactions': int(count or 0),
            'accounts': accounts,
            'months_of_history': (
                len(month_keys_between(first, last)) if first and last else 0)}


def pct_change(current, previous):
    """Percent change, or None when there is no baseline.

    None rather than 0.0 or infinity, and every consumer has to handle it. That
    is deliberate: "spending rose 100%" and "there was nothing to compare
    against" are different statements, and a copilot that says the first when it
    means the second has fabricated a trend.
    """
    if not previous:
        return None
    return round((current - previous) / abs(previous) * 100.0, 1)


__all__ = [
    'TRANSFER_CATEGORIES', 'DEFAULT_MERCHANT_LIMIT', 'Window',
    'month_bounds', 'quarter_bounds', 'year_bounds', 'resolve_window',
    'preceding_window', 'custom_window', 'lookback_window', 'label_for',
    'as_date', 'month_key',
    'month_keys_between', 'base_query', 'period_summary', 'category_totals',
    'merchant_totals', 'largest_purchases', 'monthly_series',
    'monthly_category_series', 'coverage', 'pct_change',
]
