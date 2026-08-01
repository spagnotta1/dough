"""Everything that reads or edits the ledger itself: the list, CSV upload,
inline edits, bulk operations, import undo, and export.

Phase 10 moved the *logic* of every write in this file into
`dough/services/ledger.py`, so `/api/v1/transactions` performs the identical
operations rather than a second implementation of them. What is left here is
what a view should be: read the form or the JSON body, call the service, and
turn the result into a redirect with a flash or into the JSON the existing
templates already expect.

The response shapes are unchanged on purpose. `static/js/` calls these routes
and reads `{'success': ...}`; the envelope is `/api/v1`'s contract, and
retrofitting it here would break the pages this phase is not supposed to touch.
"""

from datetime import datetime
import io
import os
import uuid

from flask import (Blueprint, Response, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
import pandas as pd
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from dough.services import ledger
from dough.services.categorization import get_category_rules
from dough.services.transactions import build_transaction_query, sticky_filter

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

        batch_id = str(uuid.uuid4())
        # Resolved once for the whole import rather than per row, and passed in
        # rather than reached for by the service — see this directory's rule 2.
        rules_engine = get_category_rules()

        saved = []
        for file in files:
            if not file.filename:
                continue
            filename = secure_filename(file.filename)
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            saved.append(filepath)

        # Removing the upload is the route's job, not the service's: the service
        # takes paths and has no idea which of them it is allowed to delete.
        # `on_file` rather than a loop afterwards so a file is removed as soon as
        # it has been read, which is what the original did.
        try:
            result = ledger.import_csv(saved, account_name, batch_id=batch_id,
                                       rules_engine=rules_engine,
                                       on_file=os.remove)
        except ledger.CsvFormatError as exc:
            # A file whose headers the importer cannot map. Before this was
            # caught it surfaced as a KeyError, which the error handler turned
            # into a 500 and a trace id — technically honest and completely
            # useless to somebody who just needs to know their export was the
            # wrong one. Listing the headers that *were* read is the part that
            # lets them fix it themselves.
            for leftover in saved:
                if os.path.exists(leftover):
                    os.remove(leftover)
            flash(
                f"I couldn't read {exc.filename} — I need "
                f'{" and ".join(exc.missing)}. The columns I found were: '
                f'{", ".join(exc.headers)}.', 'error')
            return redirect(request.url)

        if result.added > 0:
            session['last_batch_id'] = batch_id
            session['last_batch_count'] = result.added
        flash(f'All done — I added {result.added} new transactions and skipped '
              f'{result.skipped} I already had.', 'success')
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

# ---------------------------------------------------------------------------
# `except HTTPException: raise` appears in the three routes below and it is
# load-bearing, not noise.  [Phase 10]
#
# These routes resolve a caller-supplied id, which the service does with
# `get_owned` -- and `get_owned` refuses a foreign or missing row by *raising*
# werkzeug's NotFound. Before the service extraction that call sat outside the
# try block, so the 404 propagated. Moving it inside a bare `except Exception`
# swallowed it: a PUT naming another household's transaction answered 200 with
# `{'success': False}`, and the authorization failure became a message in a
# field nothing checks.
#
# Caught by `tests/test_tenancy_boundary.py::
# test_routes_deny_foreign_ids_without_the_orm_backstop`, which runs each route
# against another household's row with the ORM backstop switched off. That is
# the test's whole purpose and it earned its place here.
# ---------------------------------------------------------------------------

@bp.route('/update_category', methods=['POST'])
def update_category():
    transaction_id = request.form.get('transaction_id')
    new_category = request.form.get('category')
    try:
        ledger.update_transaction(transaction_id, {'category': new_category})
        return jsonify({'success': True})
    except HTTPException:
        raise
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
        updated = ledger.bulk_update_category(ids, new_category)
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/transactions/<int:transaction_id>', methods=['PUT'])
def edit(transaction_id):
    data = request.json
    changes = {}
    if 'date' in data:
        changes['date'] = datetime.strptime(data['date'], '%Y-%m-%d').date()
    if 'description' in data:
        changes['description'] = data['description']
    if 'amount' in data:
        changes['amount'] = float(data['amount'])
    if 'category' in data:
        changes['category'] = data['category']
    if 'account_name' in data:
        changes['account_name'] = data['account_name']
    if 'notes' in data:
        changes['notes'] = data['notes'] or None
    try:
        t = ledger.update_transaction(transaction_id, changes)
        # The template's JS reads these six keys. `ledger.serialize` is a
        # superset, so the extra fields are harmless and the shape stays the
        # one definition of what a transaction looks like.
        return jsonify({'success': True, 'transaction': ledger.serialize(t)})
    except HTTPException:
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/transactions/<int:transaction_id>', methods=['DELETE'])
def delete(transaction_id):
    try:
        ledger.delete_transaction(transaction_id)
        return jsonify({'success': True})
    except HTTPException:
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@bp.route('/import/<batch_id>/undo', methods=['POST'])
def undo_import(batch_id):
    try:
        deleted = ledger.undo_import(batch_id)
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
        deleted = ledger.bulk_delete(ids)
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

    df = pd.DataFrame(ledger.export_rows(query))
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)

    filename = f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
