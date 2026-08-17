"""Monthly budget targets and the plan they sit inside.

Phase 10 moved the arithmetic — the upsert, the two-month spend rollup, the
banding and the month-pace marker — into `dough/services/budgets.py`, so
`/api/v1/budgets` answers from the same computation rather than a second one.
What is left here is form handling and a template call.

The page grew three things after UAT, and all three are service calls rather
than template arithmetic:

* **The income anchor** (`plan`) — limits mean nothing without the take-home
  they are drawn against, and the page had never asked for it or shown it.
* **Unplanned spending** (`plan()['unplanned']`) — categories with money in
  them and no limit, which the old loop-over-budgets could not render.
* **The builder** (`suggested_limits`, `balanced_frame`) — a first budget
  proposed from the household's own months, because the alternative was a
  blank dropdown and a number somebody had to guess.

`status()` is asked for the committed split explicitly. It defaults to *not*
computing it because recurring detection walks the whole ledger and
`/api/v1/budgets` should not pay for that; a page render already has the walk
memoized on the app context, so here it is free.
"""

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from dough.services import budgets as budget_service

from models import Transaction, db

bp = Blueprint('budgets', __name__)


@bp.route('/budgets', methods=['GET', 'POST'])
def index():
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
                flash("That budget amount didn't look like a number — mind checking it?", 'error')
                return redirect(url_for('budgets.index'))
            _budget, created = budget_service.upsert_budget(
                category, account_name, monthly_limit)
            flash(f"Budget set — I'll keep an eye on {category} for you." if created
                  else f"Updated — I'll watch {category} against the new limit.",
                  'success')
        elif action == 'apply':
            created, updated = _apply_proposed_plan()
            if created or updated:
                flash(_applied_message(created, updated), 'success')
            else:
                flash("Nothing to set — tick the categories you want me to "
                      "watch and I'll take it from there.", 'error')
        elif action == 'delete':
            budget_id = request.form.get('budget_id')
            if budget_id:
                budget_service.delete_budget(budget_id)
                flash("Budget removed — I'll stop tracking that one.", 'success')
        return redirect(url_for('budgets.index'))

    # One call, and the same one `/api/v1/budgets` makes. Actual spend for the
    # current month and the one before it, so each budget can show where it
    # stands rather than only what it is — a page of limits with no spend
    # against them cannot answer the only question anyone opens it with.
    status = budget_service.status(
        committed=budget_service.committed_by_category())

    # Computed once and threaded through. `plan` would otherwise call `status`
    # and `suggested_limits` itself, and the builder below wants the same
    # suggestions — three derivations of one set of medians on one page render.
    suggestions = budget_service.suggested_limits()
    plan = budget_service.plan(status_rows=status['budgets'],
                               suggestions=suggestions)

    # The builder proposes only what is *not* already budgeted. It ships with
    # every row ticked, so leaving the budgeted ones in would mean one click on
    # "Save this plan" silently replaced limits somebody had deliberately set
    # with Dough's suggestion for them — the page would be overwriting a
    # decision while appearing to make a new one.
    #
    # The frame keeps the full set, because 50/30/20 is a statement about all
    # of a household's spending and one costed from the leftovers would be a
    # different, wrong number.
    budgeted = {b.category for b in budget_service.list_budgets()}
    proposals = [hint for category, hint in suggestions.items()
                 if category not in budgeted]

    return render_template('budgets.html',
                           budgets=budget_service.list_budgets(),
                           budget_status=status['budgets'],
                           month_label=status['month_label'],
                           month_progress=status['month_progress'],
                           total_budgeted=status['total_budgeted'],
                           total_spent=status['total_spent'],
                           total_projected=status['total_projected'],
                           plan=plan,
                           suggestions=proposals,
                           balanced=budget_service.balanced_frame(
                               suggestions=suggestions),
                           categories=categories, accounts=accounts)


def _apply_proposed_plan():
    """Read the builder's ticked rows into `(category, account, limit)` tuples.

    A category the user unticked is absent from `pick`, and an amount they
    cleared or mistyped is dropped rather than rejected — one bad box must not
    discard the eleven good ones somebody just reviewed. `upsert_many` skips
    non-positive limits, so this only has to parse.
    """
    entries = []
    for category in request.form.getlist('pick'):
        raw = request.form.get(f'limit_{category}', '')
        try:
            limit = float(raw)
        except (TypeError, ValueError):
            continue
        entries.append((category, budget_service.ACCOUNT_ANY, limit))
    return budget_service.upsert_many(entries)


def _applied_message(created, updated):
    """Dough's line after a plan is applied. Counts, because "done" is not news.

    Never phrased as a completed budget: the whole page argues that a plan is
    something you adjust, and a message saying "you're all set" would tell the
    opposite story to somebody who has just accepted six suggestions they were
    invited to edit.
    """
    parts = []
    if created:
        parts.append(f"{created} new budget{'s' if created != 1 else ''}")
    if updated:
        parts.append(f"{updated} updated")
    return (f"Plan saved — {' and '.join(parts)}. Change any of them whenever "
            "the month tells you something different.")
