"""Feature 10 — goal tracking, progress, momentum and projected completion.

The brief asks for progress, estimated completion, remaining amount and recent
momentum. Three of those are subtraction; the fourth is the only interesting
one, and it is where this module spends its care.

## Projected completion is a projection, and is labelled as one

"You will reach $20,000 in March" is a sentence about the future, produced from
a few months of the past. It is worth saying — it is the single most motivating
number a goals feature has — and it is worth never saying with certainty.

So `projection` returns a date **and** the basis it rests on: how many months
of contributions it was computed from, what the monthly rate was, and a
`confidence`. Where there is no momentum it returns `None` rather than a date
far in the future, because "at this rate, never" and "in 84 months" are
different statements and only the first is true.

## Momentum is the recent rate, not the lifetime average

A goal funded hard for three months and then abandoned has a healthy lifetime
average and no momentum at all. `MOMENTUM_MONTHS` is the window, and the
comparison against the household's own `monthly_target` — the plan they set —
is what turns "you have saved $4,200" into "you are $300 a month behind your
own plan", which is the sentence somebody can act on.

## What this module does not do

It does not decide *whether* a goal is affordable — `affordability.py` owns
that, and a goal is one of the scenarios it assesses. It does not move money.
And it does not infer goals from spending: nobody's transactions reveal that
they are saving for a wedding, which is the whole reason this data is stored
rather than derived.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func

from dough.services.analytics import as_date, month_keys_between
from models import Goal, GoalContribution, db

#: The window `momentum` measures over. Three months is short enough that
#: abandoning a goal shows up quickly and long enough that one skipped month
#: does not read as a stall.
MOMENTUM_MONTHS = 3

#: Below this many months with a contribution, no completion date is projected.
#: Two points make a line and not a habit.
MIN_MONTHS_FOR_PROJECTION = 2

#: A projection further out than this is reported as "beyond the horizon"
#: rather than as a date. A date in 2041 is arithmetic, not information.
MAX_PROJECTION_MONTHS = 120

_AVG_MONTH_DAYS = 30.44


# ── Reads ───────────────────────────────────────────────────────────────────

def list_goals(status='active', *, today=None):
    """Every goal with its progress, momentum and projection.

    `status=None` returns all of them. Ordered by how close each is to its
    target date, then by how far along it is — the goal with a deadline is the
    one somebody needs to see first, and a goal with none should not outrank it.
    """
    query = Goal.query
    if status:
        query = query.filter(Goal.status == status)
    goals = query.all()

    described = [describe(goal, today=today) for goal in goals]
    described.sort(key=lambda g: (
        g['target_date'] is None,                     # dated goals first
        g['target_date'] or '9999-12-31',
        -g['progress_pct'],
    ))
    return described


def describe(goal, *, today=None):
    """One goal, fully worked out: progress, remaining, momentum, projection."""
    today = as_date(today or date.today())

    target = float(goal.target_amount or 0)
    saved = float(goal.saved_amount or 0)
    remaining = max(0.0, target - saved)
    progress = (saved / target * 100.0) if target > 0 else 0.0

    recent = momentum(goal, today=today)
    forecast = projection(goal, recent, today=today)

    return {
        **goal.to_dict(),
        'remaining': round(remaining, 2),
        # Capped at 100 for display; `saved_amount` keeps the real figure, so an
        # overfunded goal still reports the money honestly while the bar stops
        # at full.
        'progress_pct': round(min(progress, 100.0), 1),
        'is_complete': target > 0 and saved >= target,
        'momentum': recent,
        'projection': forecast,
        'pace': _pace(goal, recent, forecast, today),
    }


def momentum(goal, *, months=MOMENTUM_MONTHS, today=None):
    """How much has actually gone in recently, per month.

    The recent rate rather than the lifetime average: a goal funded hard and
    then abandoned has a healthy average and no momentum, and the average would
    keep projecting a completion date that is no longer coming.
    """
    today = as_date(today or date.today())
    start = _months_back(today, months - 1)

    rows = (GoalContribution.query
            .filter(GoalContribution.goal_id == goal.id,
                    GoalContribution.occurred_on >= start,
                    GoalContribution.occurred_on <= today)
            .all())

    total = sum(float(row.amount) for row in rows)
    axis = month_keys_between(start, today)
    active = {f'{row.occurred_on:%Y-%m}' for row in rows}

    return {
        'window_months': len(axis),
        'contributed': round(total, 2),
        'per_month': round(total / len(axis), 2) if axis else 0.0,
        'months_with_a_contribution': len(active),
        'contributions': len(rows),
        'last_contribution': (max(r.occurred_on for r in rows).isoformat()
                              if rows else None),
        'stalled': not rows,
    }


def projection(goal, recent=None, *, today=None):
    """When this goal completes at the current rate, or None.

    None — not a distant date — when there is no rate to project from. "At this
    rate, never" is the true statement, and a date eleven years out reads as a
    plan rather than as the absence of one.
    """
    today = as_date(today or date.today())
    recent = recent or momentum(goal, today=today)

    target = float(goal.target_amount or 0)
    saved = float(goal.saved_amount or 0)
    remaining = target - saved

    if target <= 0 or remaining <= 0:
        return {'complete': True, 'date': None, 'months': 0,
                'basis': 'already at target', 'confidence': 'certain'}

    rate = recent['per_month']
    if rate <= 0 or recent['months_with_a_contribution'] < MIN_MONTHS_FOR_PROJECTION:
        return {
            'complete': False, 'date': None, 'months': None,
            'basis': ('no contributions in the last '
                      f"{recent['window_months']} months" if recent['stalled']
                      else 'not enough contribution history to project'),
            'confidence': 'none',
        }

    months = remaining / rate
    if months > MAX_PROJECTION_MONTHS:
        return {'complete': False, 'date': None, 'months': round(months),
                'basis': f'more than {MAX_PROJECTION_MONTHS // 12} years at '
                         f'{_money(rate)} a month',
                'confidence': 'low'}

    landing = today + _days(months * _AVG_MONTH_DAYS)
    return {
        'complete': False,
        'date': landing.isoformat(),
        'months': round(months, 1),
        'basis': f'{_money(rate)} a month over the last '
                 f"{recent['window_months']} months",
        'confidence': _projection_confidence(recent),
    }


def _pace(goal, recent, forecast, today):
    """Whether the goal is on track for its own target date and monthly plan.

    Two separate comparisons, because a household can be ahead of its plan and
    still miss a date it set optimistically, and the two need different words.
    """
    pace = {'vs_plan': None, 'vs_target_date': None, 'required_per_month': None}

    plan = float(goal.monthly_target) if goal.monthly_target is not None else None
    if plan:
        difference = recent['per_month'] - plan
        pace['vs_plan'] = {
            'planned': round(plan, 2),
            'actual': recent['per_month'],
            'difference': round(difference, 2),
            'on_plan': difference >= 0,
        }

    if goal.target_date:
        target_date = as_date(goal.target_date)
        remaining = max(0.0, float(goal.target_amount or 0)
                        - float(goal.saved_amount or 0))
        days_left = (target_date - today).days
        months_left = max(days_left / _AVG_MONTH_DAYS, 0.0)

        required = (remaining / months_left) if months_left > 0 else None
        pace['required_per_month'] = (round(required, 2)
                                      if required is not None else None)
        pace['vs_target_date'] = {
            'target_date': target_date.isoformat(),
            'days_left': days_left,
            'overdue': days_left < 0 and remaining > 0,
            # None when the date has passed: there is no rate that reaches a
            # deadline already behind you, and a huge number would imply there is.
            'on_track': (None if required is None
                         else recent['per_month'] >= required),
        }
    return pace


def summary(*, today=None):
    """Household-level totals, for a card and for the AI context."""
    goals = list_goals(status='active', today=today)
    target = sum(g['target_amount'] for g in goals)
    saved = sum(g['saved_amount'] for g in goals)
    return {
        'count': len(goals),
        'total_target': round(target, 2),
        'total_saved': round(saved, 2),
        'total_remaining': round(max(0.0, target - saved), 2),
        'overall_progress_pct': (round(saved / target * 100, 1)
                                 if target > 0 else None),
        'monthly_commitment': round(
            sum(g['monthly_target'] or 0 for g in goals), 2),
        'stalled': [g['name'] for g in goals if g['momentum']['stalled']],
        'behind_plan': [g['name'] for g in goals
                        if (g['pace']['vs_plan'] or {}).get('on_plan') is False],
        'goals': goals,
    }


# ── Writes ──────────────────────────────────────────────────────────────────

def create_goal(*, name, target_amount, kind='custom', target_date=None,
                monthly_target=None, note=None):
    """Add a goal. Returns it, or raises ValueError with a usable message."""
    name = (name or '').strip()
    if not name:
        raise ValueError('A goal needs a name.')
    if kind not in Goal.KINDS:
        kind = 'custom'

    amount = _positive(target_amount, 'target amount')
    if Goal.query.filter(func.lower(Goal.name) == name.lower()).first():
        raise ValueError(f'You already have a goal called "{name}".')

    goal = Goal(
        name=name, kind=kind, target_amount=Decimal(str(amount)),
        saved_amount=Decimal('0'),
        target_date=as_date(target_date) if target_date else None,
        monthly_target=(Decimal(str(_positive(monthly_target, 'monthly amount')))
                        if monthly_target else None),
        note=(note or '').strip() or None)
    db.session.add(goal)
    db.session.commit()
    return goal


def update_goal(goal_id, **changes):
    """Edit a goal's own fields. Never touches `saved_amount`.

    Progress moves through `contribute()` so the history stays complete — an
    edit that silently changed the total would leave the contributions and the
    balance disagreeing, and the contributions are what momentum is measured
    from.
    """
    goal = _owned(goal_id)

    if 'name' in changes:
        name = (changes['name'] or '').strip()
        if not name:
            raise ValueError('A goal needs a name.')
        clash = Goal.query.filter(func.lower(Goal.name) == name.lower(),
                                  Goal.id != goal.id).first()
        if clash:
            raise ValueError(f'You already have a goal called "{name}".')
        goal.name = name

    if 'target_amount' in changes:
        goal.target_amount = Decimal(str(_positive(changes['target_amount'],
                                                   'target amount')))
    if 'target_date' in changes:
        value = changes['target_date']
        goal.target_date = as_date(value) if value else None
    if 'monthly_target' in changes:
        value = changes['monthly_target']
        goal.monthly_target = (Decimal(str(_positive(value, 'monthly amount')))
                               if value else None)
    if 'kind' in changes and changes['kind'] in Goal.KINDS:
        goal.kind = changes['kind']
    if 'status' in changes and changes['status'] in Goal.STATUSES:
        goal.status = changes['status']
    if 'note' in changes:
        goal.note = (changes['note'] or '').strip() or None

    _settle_achievement(goal)
    db.session.commit()
    return goal


def contribute(goal_id, amount, *, occurred_on=None, note=None):
    """Record money into (or out of) a goal, and move the total with it.

    A negative amount is a withdrawal and is allowed: money comes back out of a
    holiday fund, and recording it keeps the history and the balance honest.
    Refusing it would push people to edit the total directly, which loses the
    record entirely.
    """
    goal = _owned(goal_id)
    amount = float(amount or 0)
    if not amount:
        raise ValueError('A contribution needs an amount.')

    contribution = GoalContribution(
        goal_id=goal.id, amount=Decimal(str(round(amount, 2))),
        occurred_on=as_date(occurred_on or date.today()),
        note=(note or '').strip() or None)
    db.session.add(contribution)

    # Floored at zero: a withdrawal larger than the balance is a data-entry
    # slip, and a negative "saved" figure would render as a negative progress
    # bar rather than as the mistake it is.
    goal.saved_amount = Decimal(str(round(
        max(0.0, float(goal.saved_amount or 0) + amount), 2)))
    _settle_achievement(goal)

    db.session.commit()
    return contribution


def delete_goal(goal_id):
    """Remove a goal and its contributions. Returns the name that went."""
    goal = _owned(goal_id)
    name = goal.name
    db.session.delete(goal)          # cascade removes the contributions
    db.session.commit()
    return name


def contributions(goal_id, limit=50):
    """A goal's deposit history, most recent first."""
    goal = _owned(goal_id)
    rows = (GoalContribution.query
            .filter(GoalContribution.goal_id == goal.id)
            .order_by(GoalContribution.occurred_on.desc(),
                      GoalContribution.id.desc())
            .limit(limit).all())
    return [row.to_dict() for row in rows]


# ── internals ───────────────────────────────────────────────────────────────

def _owned(goal_id):
    """A goal belonging to the current household, or ValueError.

    An explicit lookup through `Goal.query`, which is `TenantScopedQuery`. The
    services README's rule for a caller-supplied id: the ORM backstop already
    filters, and this is the second lock on the same door.
    """
    goal = Goal.query.filter(Goal.id == goal_id).first()
    if goal is None:
        raise ValueError('That goal does not exist.')
    return goal


def _settle_achievement(goal):
    """Keep `status` and `achieved_at` agreeing with the numbers.

    Both directions. A goal whose target is raised after being met goes back to
    active, because leaving it 'achieved' would quietly stop tracking a goal the
    user has just extended.
    """
    reached = (float(goal.target_amount or 0) > 0
               and float(goal.saved_amount or 0) >= float(goal.target_amount))
    if reached and goal.status == 'active':
        goal.status = 'achieved'
        goal.achieved_at = datetime.utcnow()
    elif not reached and goal.status == 'achieved':
        goal.status = 'active'
        goal.achieved_at = None


def _projection_confidence(recent):
    if recent['months_with_a_contribution'] < recent['window_months']:
        return 'low'
    return 'moderate' if recent['contributions'] < 3 else 'high'


def _positive(value, what):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'That {what} is not a number.') from None
    if amount <= 0:
        raise ValueError(f'The {what} needs to be more than zero.')
    return round(amount, 2)


def _months_back(anchor, n):
    year, month = anchor.year, anchor.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _days(count):
    from datetime import timedelta
    return timedelta(days=int(round(count)))


def _money(value):
    return f'${value:,.0f}'


__all__ = ['list_goals', 'describe', 'momentum', 'projection', 'summary',
           'create_goal', 'update_goal', 'contribute', 'delete_goal',
           'contributions', 'MOMENTUM_MONTHS', 'MIN_MONTHS_FOR_PROJECTION',
           'MAX_PROJECTION_MONTHS']
