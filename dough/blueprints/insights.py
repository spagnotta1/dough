"""Derived views of the ledger: the Insights hub, anomaly scores and recurring.

None of these own any data -- all are opinions about transactions -- which is
why they share a module rather than each getting one.

## The hub [Phase 11A]

`/insights` is the consolidated view. Before it, the nav carried seven
destinations and two of them ("Anomalies", "Recurring") were narrow, one-table
pages that a person visits rarely and that cost a permanent slot in a bar which
already did not fit tablets in portrait -- see the comment above `#primary-nav`
in `base.html`.

The hub puts the health score, the proactive insights and the spending trends on
one page, with unusual activity as a **collapsed** `<details>` section beneath
them. That inverts the old emphasis correctly: the anomaly table was the whole
page and is in fact the least-read thing on it, while the score and the
observations were nowhere.

Both original pages are still served and still linked from here. Retiring a
route would break bookmarks and lose the paginated review workflow, which the
hub deliberately does not reimplement -- it shows the open items and links to
the full table rather than growing a second pager.

Every figure on the hub comes from `dough/services/`, so the page and the
copilot cannot disagree about what the household's finances look like.
"""

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from dough.ai.copilot import current_copilot
from dough.tenancy import get_owned

from models import RecurringDismissal, Transaction, db
from recurring import detect_recurring, normalize_description

bp = Blueprint('insights', __name__)

#: How many open anomalies the collapsed section shows before deferring to the
#: full table. Twenty is enough to make "review them here" the common path and
#: few enough that the section stays a section.
HUB_ANOMALY_LIMIT = 20


@bp.route('/insights')
def hub():
    """Health, observations, trends, and unusual activity in one page.

    Everything is computed through the services rather than here: the route
    reads no rows of its own beyond the anomaly list it renders, which is the
    same rule the rest of `dough/blueprints/` follows.
    """
    if not Transaction.query.first():
        flash("I don't have any transactions yet — upload some and I'll get to work.",
              'info')
        return redirect(url_for('transactions.upload'))

    open_anomalies = (Transaction.query
                      .filter(Transaction.anomaly_score == -1.0,
                              Transaction.anomaly_reviewed == False)  # noqa: E712
                      .order_by(Transaction.date.desc())
                      .limit(HUB_ANOMALY_LIMIT).all())

    # One coordinated pass for the whole page. The route used to call
    # `detect()`, `summary()`, `insights()`, `health.score()` and
    # `category_trends()` separately -- and three of those run the detector
    # internally, so rendering this page cost three full passes over a year of
    # transactions. `FinancialCopilot.analytics()` computes each expensive
    # service once and threads the result through the rest.
    run = current_copilot().analytics()

    return render_template(
        'insights.html',
        health=run['health'],
        insights=run['insights'],
        category_trends=[t for t in run['trends'][:6]
                         if t['direction'] in ('rising', 'falling')],
        detected=run['findings'][:8],
        anomaly_summary=run['anomaly_summary'],
        open_anomalies=open_anomalies,
        open_anomaly_count=_open_anomaly_count(),
        hub_anomaly_limit=HUB_ANOMALY_LIMIT)


def _open_anomaly_count():
    return (Transaction.query
            .filter(Transaction.anomaly_score == -1.0,
                    Transaction.anomaly_reviewed == False)  # noqa: E712
            .count())

@bp.route('/anomalies/<int:transaction_id>/dismiss', methods=['POST'])
def dismiss_anomaly(transaction_id):
    t = get_owned(Transaction, transaction_id)
    t.anomaly_reviewed = True
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/anomalies/dismiss_all', methods=['POST'])
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

@bp.route('/anomalies')
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
        flash("I don't have any transactions yet — upload some and I'll get to work.", 'info')
        return redirect(url_for('transactions.upload'))

    anomaly_page = query.order_by(order).paginate(page=page, per_page=50, error_out=False)

    return render_template('anomalies.html',
                           anomalies=anomaly_page,
                           sort_by=sort_by,
                           show_reviewed=show_reviewed)

@bp.route('/recurring')
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

@bp.route('/recurring/dismiss', methods=['POST'])
def recurring_dismiss():
    description = (request.form.get('description') or '').strip()
    kind = request.form.get('kind', 'subscription')
    desc_key = normalize_description(description)
    if desc_key and not RecurringDismissal.query.filter_by(desc_key=desc_key).first():
        db.session.add(RecurringDismissal(desc_key=desc_key, description=description,
                                          kind=kind))
        db.session.commit()
        flash(f'Got it — I\'ll leave "{description}" out of your recurring view.', 'success')
    return redirect(url_for('insights.recurring'))

@bp.route('/recurring/restore', methods=['POST'])
def recurring_restore():
    dismissal_id = request.form.get('id', type=int)
    dismissal = db.session.get(RecurringDismissal, dismissal_id) if dismissal_id else None
    if dismissal:
        db.session.delete(dismissal)
        db.session.commit()
        flash(f'"{dismissal.description}" is back in your recurring view.', 'success')
    return redirect(url_for('insights.recurring'))
