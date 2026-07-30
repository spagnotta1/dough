"""Monthly budget targets and their progress."""

from calendar import monthrange
from datetime import datetime, timedelta

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)
from sqlalchemy import func

from dough.tenancy import get_owned

from models import Budget, Transaction, db

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
            existing = Budget.query.filter_by(category=category, account_name=account_name).first()
            if existing:
                existing.monthly_limit = monthly_limit
                flash(f"Updated — I'll watch {category} against the new limit.", 'success')
            else:
                db.session.add(Budget(category=category, account_name=account_name, monthly_limit=monthly_limit))
                flash(f"Budget set — I'll keep an eye on {category} for you.", 'success')
            db.session.commit()
        elif action == 'delete':
            budget_id = request.form.get('budget_id')
            b = get_owned(Budget, budget_id) if budget_id else None
            if b:
                db.session.delete(b)
                db.session.commit()
                flash("Budget removed — I'll stop tracking that one.", 'success')
        return redirect(url_for('budgets.index'))

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
