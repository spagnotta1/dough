"""Everything that reads or edits the ledger itself: the list, CSV upload,
inline edits, bulk operations, import undo, and export."""

from datetime import datetime
import io
import os
import uuid

from flask import (Blueprint, Response, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
import pandas as pd
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from dough.services.categorization import get_category_rules
from dough.services.transactions import (build_transaction_query,
                                         compute_anomaly_scores, sticky_filter)
from dough.tenancy import get_owned

from models import Transaction, db

bp = Blueprint('transactions', __name__)

@bp.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'files[]' not in request.files:
            flash("I didn't get a file — pick one and try again.", 'error')
            return redirect(request.url)

        files = request.files.getlist('files[]')
        account_name = request.form.get('account_name')

        if not account_name:
            flash('Let me know which account these belong to first.', 'error')
            return redirect(request.url)

        total_new = 0
        total_skipped = 0
        batch_id = str(uuid.uuid4())
        # Resolved once for the whole import rather than per row. It was a
        # closure variable before Phase 3; this keeps the lookup identical
        # in cost as well as in result.
        rules_engine = get_category_rules()

        for file in files:
            if not file.filename:
                continue
            print(f"Processing file: {file.filename}")
            filename = secure_filename(file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            df = pd.read_csv(filepath)
            df = df.sort_values(by='Transaction Date')
            prev_balance = None

            for idx, row in df.iterrows():
                desc = str(row['Transaction Description'])
                date = pd.to_datetime(row['Transaction Date']).date()
                amount = float(row['Transaction Amount'])
                balance = float(row['Balance']) if not pd.isnull(row['Balance']) else None

                signed_amount = amount
                desc_lower = desc.lower()

                if 'deposit from 360 checking' in desc_lower or 'deposit from 360 performance savings' in desc_lower:
                    signed_amount = abs(amount)
                elif 'withdrawal to 360 checking' in desc_lower or 'withdrawal to 360 performance savings' in desc_lower:
                    signed_amount = -abs(amount)
                elif 'monthly interest paid' in desc_lower:
                    signed_amount = abs(amount)
                elif 'credit card' in desc_lower or 'credit crd' in desc_lower:
                    signed_amount = -abs(amount)
                elif 'purchase' in desc_lower:
                    signed_amount = -abs(amount)
                elif 'deposit' in desc_lower or 'credit' in desc_lower:
                    signed_amount = abs(amount)
                elif 'withdraw' in desc_lower or 'payment' in desc_lower:
                    signed_amount = -abs(amount)
                elif prev_balance is not None and balance is not None:
                    if balance < prev_balance:
                        signed_amount = -abs(amount)
                    elif balance > prev_balance:
                        signed_amount = abs(amount)

                category = rules_engine.get_category(desc)
                transaction = Transaction(
                    account_name=account_name,
                    date=date,
                    description=desc,
                    amount=signed_amount,
                    category=category,
                    import_batch_id=batch_id
                )
                try:
                    db.session.add(transaction)
                    db.session.flush()
                    total_new += 1
                except IntegrityError:
                    db.session.rollback()
                    total_skipped += 1
                else:
                    db.session.commit()

                prev_balance = balance

            os.remove(filepath)
            print(f"Finished processing: {file.filename}")

        compute_anomaly_scores()
        if total_new > 0:
            session['last_batch_id'] = batch_id
            session['last_batch_count'] = total_new
        flash(f'All done — I added {total_new} new transactions and skipped '
              f'{total_skipped} I already had.', 'success')
        return redirect(url_for('transactions.upload'))

    return render_template('upload.html',
                           last_batch_id=session.get('last_batch_id'),
                           last_batch_count=session.get('last_batch_count'))

@bp.route('/transactions')
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    if per_page not in (25, 50, 100, 250):
        per_page = 50
    sort_by = request.args.get('sort_by', 'date')
    sort_dir = request.args.get('sort_dir', 'desc')

    start_date_str = sticky_filter('start_date')
    end_date_str = sticky_filter('end_date')
    account_filter = sticky_filter('account', default='both')
    category_filter = sticky_filter('category')
    direction_filter = sticky_filter('direction', 'type', 'direction')
    search_query = sticky_filter('search')

    if request.args.get('type'):
        session.pop('search', None)
        search_query = None

    session['start_date'] = start_date_str
    session['end_date'] = end_date_str
    session['account'] = account_filter
    session['category'] = category_filter
    session['direction'] = direction_filter
    session['search'] = search_query

    query = build_transaction_query(account_filter, category_filter, start_date_str,
                                    end_date_str, direction_filter, search_query)

    sort_col_map = {
        'id': Transaction.id,
        'date': Transaction.date,
        'description': Transaction.description,
        'account': Transaction.account_name,
        'category': Transaction.category,
        'amount': Transaction.amount,
    }
    sort_col = sort_col_map.get(sort_by, Transaction.date)
    order = sort_col.asc() if sort_dir == 'asc' else sort_col.desc()

    categories = db.session.query(Transaction.category).distinct().order_by(Transaction.category).all()
    accounts = db.session.query(Transaction.account_name).distinct().all()

    txn_page = query.order_by(order).paginate(
        page=page, per_page=per_page, error_out=False, max_per_page=250
    )

    return render_template('transactions.html',
                           transactions=txn_page,
                           categories=[c[0] for c in categories],
                           accounts=[a[0] for a in accounts],
                           start_date=start_date_str,
                           end_date=end_date_str,
                           account_filter=account_filter,
                           category_filter=category_filter,
                           direction_filter=direction_filter,
                           search_query=search_query,
                           sort_by=sort_by,
                           sort_dir=sort_dir,
                           per_page=per_page)

@bp.route('/clear_filters')
def clear_filters():
    for key in ['start_date', 'end_date', 'account', 'category', 'direction', 'search']:
        session.pop(key, None)
    flash("Filters cleared — you're seeing everything again.", 'info')
    next_url = request.args.get('next')
    return redirect(next_url if next_url else url_for('transactions.index'))

@bp.route('/update_category', methods=['POST'])
def update_category():
    transaction_id = request.form.get('transaction_id')
    new_category = request.form.get('category')
    transaction = get_owned(Transaction, transaction_id)
    transaction.category = new_category
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/update_categories_bulk', methods=['POST'])
def update_categories_bulk():
    data = request.json
    ids = data.get('ids', [])
    new_category = data.get('category', '')
    if not ids or not new_category:
        return jsonify({'success': False, 'error': 'Missing ids or category'})
    try:
        Transaction.query.filter(Transaction.id.in_(ids)).update(
            {Transaction.category: new_category}, synchronize_session='fetch'
        )
        db.session.commit()
        return jsonify({'success': True, 'updated': len(ids)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/transactions/<int:transaction_id>', methods=['PUT'])
def edit(transaction_id):
    t = get_owned(Transaction, transaction_id)
    data = request.json
    if 'date' in data:
        t.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    if 'description' in data:
        t.description = data['description']
    if 'amount' in data:
        t.amount = float(data['amount'])
    if 'category' in data:
        t.category = data['category']
    if 'account_name' in data:
        t.account_name = data['account_name']
    if 'notes' in data:
        t.notes = data['notes'] or None
    try:
        db.session.commit()
        return jsonify({'success': True, 'transaction': {
            'id': t.id,
            'date': t.date.strftime('%Y-%m-%d'),
            'description': t.description,
            'amount': float(t.amount),
            'category': t.category,
            'account_name': t.account_name,
            'notes': t.notes or '',
        }})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/transactions/<int:transaction_id>', methods=['DELETE'])
def delete(transaction_id):
    transaction = get_owned(Transaction, transaction_id)
    try:
        db.session.delete(transaction)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/import/<batch_id>/undo', methods=['POST'])
def undo_import(batch_id):
    try:
        deleted = Transaction.query.filter_by(import_batch_id=batch_id).delete(synchronize_session='fetch')
        db.session.commit()
        session.pop('last_batch_id', None)
        session.pop('last_batch_count', None)
        flash(f'Undone — I took those {deleted} transaction(s) back out.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Undo failed: {str(e)}', 'error')
    return redirect(url_for('transactions.upload'))

@bp.route('/transactions/bulk_delete', methods=['POST'])
def bulk_delete():
    data = request.json
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'error': 'No IDs provided'})
    try:
        deleted = Transaction.query.filter(Transaction.id.in_(ids)).delete(synchronize_session='fetch')
        db.session.commit()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/export')
def export():
    account_filter = sticky_filter('account', default='both')
    category_filter = sticky_filter('category')
    start_date_str = sticky_filter('start_date')
    end_date_str = sticky_filter('end_date')
    direction_filter = sticky_filter('direction', 'type', 'direction')
    search_query = sticky_filter('search')

    query = build_transaction_query(account_filter, category_filter, start_date_str,
                                    end_date_str, direction_filter, search_query)
    txns = query.order_by(Transaction.date.desc()).all()

    rows = [{
        'ID': t.id,
        'Date': t.date.strftime('%Y-%m-%d'),
        'Description': t.description,
        'Account': t.account_name,
        'Category': t.category,
        'Amount': float(t.amount),
    } for t in txns]

    df = pd.DataFrame(rows)
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)

    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
