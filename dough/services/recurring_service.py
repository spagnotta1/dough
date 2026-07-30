"""Recurring-payment detection, wired to the database and to user dismissals.

The detection algorithm itself lives in the framework-free `recurring` module.
This is the thin layer that feeds it transactions, applies the user's manual
dismissals, and memoizes the expensive call. Moved verbatim out of
`create_app()`'s closures in Phase 3.

Named `recurring_service` rather than `recurring` on purpose: a module named
`dough/services/recurring.py` would shadow the top-level `recurring` import
below for anything doing a relative-looking import, and the resulting
`ImportError` would be reported against the wrong file.

Allowed:   models, `recurring`, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators
Exception: `detect_recurring_full` is CONTEXT-BOUND -- it memoizes on `flask.g`.
           `g` belongs to the app context, not the request, so this is still
           callable from the sync scheduler thread; but each work item there
           gets a fresh context, so the memo is per-item rather than
           process-wide. That is the intended lifetime.
"""

from flask import g

from models import RecurringDismissal, Transaction
from recurring import detect_recurring


def dismissed_recurring_keys():
    return [d.desc_key for d in RecurringDismissal.query.all()]


def detect_recurring_summary():
    """Return detected recurring bills and subscriptions for Claude context."""
    txns = Transaction.query.order_by(Transaction.date.asc()).all()
    detected = detect_recurring([{
        'date': t.date,
        'description': t.description,
        'amount': float(t.amount),
        'category': t.category,
    } for t in txns], dismissed_keys=dismissed_recurring_keys())
    # The loop variable was `g` before this module existed, which was safe
    # (comprehensions have their own scope) but now reads as shadowing the
    # `flask.g` imported above. Renamed; nothing else about this changed.
    return {
        kind: [{
            'description': item['description'],
            'category': item['category'],
            'monthly_amount': abs(item['monthly_amount']),
            'occurrences': item['occurrences'],
            'last_seen': item['last_seen'],
        } for item in groups]
        for kind, groups in detected.items()
    }


def detect_recurring_full():
    """Full recurring detection, memoized for the life of one request.

    Detection walks every transaction and clusters them, which is fine
    once but wasteful three times in a single dashboard render — the
    attention center, the bill schedule, and the forecast all want the
    same answer.
    """
    cached = getattr(g, '_recurring_full', None)
    if cached is not None:
        return cached
    txns = Transaction.query.order_by(Transaction.date.asc()).all()
    detected = detect_recurring([{
        'date': t.date,
        'description': t.description,
        'amount': float(t.amount),
        'category': t.category,
        'account_name': t.account_name,
    } for t in txns], dismissed_keys=dismissed_recurring_keys())
    g._recurring_full = detected
    return detected
