"""Goal tracking. [Phase 11B]

A thin blueprint over `dough/services/goals.py`: every route reads the form,
calls one service function, and redirects with a message. The projections, the
momentum window and the pace comparisons are all decided in the service, which
is what lets the same figures reach the copilot without a second implementation.

`ValueError` is the service's way of saying "a person got this wrong" — a blank
name, a duplicate, a target of zero — and it carries the sentence to show them.
Catching it once here is why no route needs its own validation.
"""

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from dough.services import goals as goal_service

bp = Blueprint('goals', __name__)


@bp.route('/goals')
def index():
    """Every goal, with progress, momentum and a projected completion."""
    return render_template('goals.html',
                           summary=goal_service.summary(),
                           kinds=_KIND_LABELS)


@bp.route('/goals/new', methods=['POST'])
def create():
    try:
        goal_service.create_goal(
            name=request.form.get('name'),
            target_amount=request.form.get('target_amount'),
            kind=request.form.get('kind') or 'custom',
            target_date=request.form.get('target_date') or None,
            monthly_target=request.form.get('monthly_target') or None,
            note=request.form.get('note'))
        flash('Goal added. Record what you put aside and I will track the pace.',
              'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('goals.index'))


@bp.route('/goals/<int:goal_id>/edit', methods=['POST'])
def edit(goal_id):
    changes = {key: request.form.get(key) for key in
               ('name', 'target_amount', 'target_date', 'monthly_target',
                'kind', 'status', 'note')
               if key in request.form}
    try:
        goal_service.update_goal(goal_id, **changes)
        flash('Goal updated.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('goals.index'))


@bp.route('/goals/<int:goal_id>/contribute', methods=['POST'])
def contribute(goal_id):
    """Record money in or out. A negative amount is a withdrawal, deliberately."""
    try:
        goal_service.contribute(goal_id, request.form.get('amount'),
                                occurred_on=request.form.get('occurred_on') or None,
                                note=request.form.get('note'))
        flash('Recorded.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('goals.index'))


@bp.route('/goals/<int:goal_id>/delete', methods=['POST'])
def delete(goal_id):
    try:
        name = goal_service.delete_goal(goal_id)
        flash(f'Deleted "{name}" and its history.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('goals.index'))


#: Display names for `Goal.KINDS`. Here rather than on the model because they
#: are copy, and the model should not need editing to reword a dropdown.
_KIND_LABELS = (
    ('emergency_fund', 'Emergency fund'),
    ('debt_payoff', 'Debt payoff'),
    ('vacation', 'Vacation'),
    ('home', 'Home'),
    ('retirement', 'Retirement'),
    ('custom', 'Something else'),
)
