"""The manual check-register API.

The /log page itself is retired; these endpoints remain because the
Investments page reads and writes account balances through them."""

from datetime import datetime

from flask import Blueprint, jsonify, request

from dough.services import audit
from dough.tenancy import get_owned

from models import (AccountBalance, EVENT_ACCOUNT_BALANCE_SET, LogEntry, db)

bp = Blueprint('log', __name__)

@bp.route('/api/log/entries', methods=['GET'])
def get_entries():
    entries = LogEntry.query.order_by(LogEntry.date.asc()).all()
    return jsonify([entry.to_dict() for entry in entries])

def _recompute_log_balances(account_type):
    """Recompute and persist snapshot balance fields for all entries of an account."""
    acc_bal = AccountBalance.query.filter_by(account_type=account_type).first()
    sb = float(acc_bal.starting_balance) if acc_bal else 0.0
    all_entries = LogEntry.query.filter_by(account_type=account_type).all()
    cleared_sum = sum(e.amount for e in all_entries if e.cleared)
    pending_sum = sum(e.amount for e in all_entries if not e.cleared)
    for e in all_entries:
        e.starting_balance = sb
        e.cleared_balance = sb + cleared_sum
        e.pending_total = pending_sum
        e.available_balance = sb + cleared_sum + pending_sum

@bp.route('/api/log/entries', methods=['POST'])
def add_entry():
    data = request.json
    account_type = data['account_type']
    amount = float(data['amount'])
    cleared = bool(data.get('cleared', False))

    acc_bal = AccountBalance.query.filter_by(account_type=account_type).first()
    sb = float(acc_bal.starting_balance) if acc_bal else 0.0
    existing = LogEntry.query.filter_by(account_type=account_type).all()

    cleared_sum = sum(e.amount for e in existing if e.cleared) + (amount if cleared else 0)
    pending_sum = sum(e.amount for e in existing if not e.cleared) + (0 if cleared else amount)

    entry = LogEntry(
        account_type=account_type,
        date=datetime.strptime(data['date'], '%Y-%m-%d').date(),
        description=data['description'],
        amount=amount,
        cleared=cleared,
        starting_balance=sb,
        pending_total=pending_sum,
        cleared_balance=sb + cleared_sum,
        available_balance=sb + cleared_sum + pending_sum
    )
    db.session.add(entry)
    try:
        db.session.commit()
        return jsonify(entry.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@bp.route('/api/log/entries/<int:entry_id>', methods=['PUT'])
def update_entry(entry_id):
    entry = get_owned(LogEntry, entry_id)
    data = request.json
    if 'date' in data:
        entry.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    if 'description' in data:
        entry.description = data['description']
    if 'amount' in data:
        entry.amount = float(data['amount'])
    if 'cleared' in data:
        entry.cleared = bool(data['cleared'])
    _recompute_log_balances(entry.account_type)
    try:
        db.session.commit()
        return jsonify(entry.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@bp.route('/api/log/entries/<int:entry_id>', methods=['DELETE'])
def delete_entry(entry_id):
    entry = get_owned(LogEntry, entry_id)
    db.session.delete(entry)
    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@bp.route('/api/log/clear', methods=['POST'])
def clear():
    try:
        LogEntry.query.delete()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@bp.route('/api/log/balances', methods=['GET'])
def get_balances():
    return jsonify([b.to_dict() for b in AccountBalance.query.all()])

@bp.route('/api/log/balances/<account_type>', methods=['PUT'])
def update_balance(account_type):
    balance = AccountBalance.query.filter_by(account_type=account_type).first()
    if not balance:
        balance = AccountBalance(account_type=account_type)
        db.session.add(balance)
    was = balance.starting_balance
    balance.starting_balance = float(request.json['starting_balance'])
    try:
        db.session.commit()
        audit.record(EVENT_ACCOUNT_BALANCE_SET, entity_type='account_balance',
                     entity_id=balance.id,
                     metadata={'account_type': account_type, 'from': was,
                               'to': balance.starting_balance})
        return jsonify(balance.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
