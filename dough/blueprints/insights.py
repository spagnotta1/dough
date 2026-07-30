"""Derived views of the ledger: anomaly scores and recurring detection.

Neither owns any data of its own -- both are opinions about transactions --
which is why they share a module rather than each getting one."""



from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from dough.tenancy import get_owned

from models import RecurringDismissal, Transaction, db
from recurring import detect_recurring, normalize_description

bp = Blueprint('insights', __name__)

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
