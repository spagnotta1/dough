"""Monthly budget targets and their progress.

Phase 10 moved the arithmetic — the upsert, the two-month spend rollup, the
banding and the month-pace marker — into `dough/services/budgets.py`, so
`/api/v1/budgets` answers from the same computation rather than a second one.
What is left is form handling and a template call.
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
    status = budget_service.status()

    return render_template('budgets.html',
                           budgets=budget_service.list_budgets(),
                           budget_status=status['budgets'],
                           month_label=status['month_label'],
                           month_progress=status['month_progress'],
                           total_budgeted=status['total_budgeted'],
                           total_spent=status['total_spent'],
                           categories=categories, accounts=accounts)
