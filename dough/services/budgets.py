"""Monthly budget targets, and where each one stands.  [Phase 10]

Allowed:   models, dough.tenancy, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`

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
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta

from sqlalchemy import func

from dough.tenancy import get_owned

__all__ = [
    'ACCOUNT_ANY',
    'delete_budget',
    'list_budgets',
    'month_window',
    'serialize',
    'spend_by_category',
    'status',
    'upsert_budget',
]

#: The pseudo-account meaning "count this category across every account".
ACCOUNT_ANY = 'both'

#: Where the bands sit. Named rather than inline so the page, the API and any
#: future alerting agree on what "warning" means without three copies of `80`.
WARN_AT_PCT = 80
OVER_AT_PCT = 100


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


def status(today=None):
    """Every budget with its spend, its band and its month-over-month change.

    Returns plain dictionaries carrying the budget's own fields rather than the
    ORM object the template used. The template could hold a model; a JSON
    response cannot, and having the service return two different shapes
    depending on its caller is exactly the split this phase exists to remove.
    """
    month_start, now, prev_start, prev_end = month_window(today)

    this_month = spend_by_category(month_start, now)
    last_month = spend_by_category(prev_start, prev_end)

    rows = []
    for budget in list_budgets():
        limit = float(budget.monthly_limit)
        spent = this_month.get((budget.category, budget.account_name), 0.0)
        prior = last_month.get((budget.category, budget.account_name), 0.0)
        pct = (spent / limit * 100) if limit > 0 else 0.0
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
        })

    # How far through the month we are. A budget at 60% means opposite things on
    # the 5th and the 25th, and this is the number that tells them apart.
    days_in_month = monthrange(now.year, now.month)[1]

    return {
        'budgets': rows,
        'month_label': now.strftime('%B %Y'),
        'month_progress': round(now.day / days_in_month * 100),
        'total_budgeted': round(sum(r['monthly_limit'] for r in rows), 2),
        'total_spent': round(sum(r['spent'] for r in rows), 2),
    }
