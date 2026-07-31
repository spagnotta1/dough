"""Writing to the ledger: import, edit, bulk operations, undo, export.  [Phase 10]

Allowed:   models, dough.tenancy, dough.services.*, SQLAlchemy, pandas, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`

`dough/services/transactions.py` holds the *read* side — building a filtered
query, scoring anomalies. This module holds the write side, and it exists
because Phase 10 needs one implementation of each of these operations rather
than two.

Before this the logic lived in `dough/blueprints/transactions.py`, mixed into
the view functions. That was fine while a Jinja page was the only caller. It
stops being fine the moment `/api/v1/transactions` needs to do the same thing:
either the API reimplements the CSV sign inference and the two drift, or the API
calls the view function and inherits its flashes and redirects. Neither is a
thing to build on, so the logic moved here and both callers reach it.

## What moved, and what deliberately did not

Moved: sign inference, transaction creation, field-by-field edit, bulk category
update, bulk delete, import undo, export row shaping.

Not moved: the flash messages, the redirects, the session's `last_batch_id`
bookkeeping. Those are presentation, and they are exactly what the API must not
inherit. A service returns what happened; deciding whether that becomes a
redirect with a cheerful sentence or a JSON envelope is the caller's job.

## The commit boundaries are the ones the routes had

Per `dough/services/README.md` rule 3: a function that committed before the move
still commits, at the same point. `import_csv` commits per row, which is not how
anybody would write it today — but it is what produces the existing dedupe
behaviour, where one duplicate row is skipped and the rest of the file still
imports. Batching the commit would change which rows survive a partial failure,
and that is a behaviour change wearing a performance improvement's clothes.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy.exc import IntegrityError

from dough.services.transactions import compute_anomaly_scores
from dough.tenancy import get_owned

__all__ = [
    'EDITABLE_FIELDS',
    'ImportResult',
    'bulk_delete',
    'bulk_update_category',
    'create_transaction',
    'delete_transaction',
    'export_rows',
    'import_csv',
    'infer_signed_amount',
    'serialize',
    'undo_import',
    'update_transaction',
]

#: The fields a caller may change on an existing transaction. An allow-list
#: rather than "whatever keys are in the payload": without it, a request
#: carrying `household_id` would re-parent a row, which the tenancy write guard
#: would refuse — but as a 500 from deep inside a flush rather than as a clear
#: refusal, and only because that guard happens to exist.
EDITABLE_FIELDS = ('date', 'description', 'amount', 'category', 'account_name',
                   'notes')


class ImportResult:
    """What one CSV import did. Returned rather than flashed.

    A small class rather than a tuple because three call sites read these
    fields, and `result[1]` at the third one is how a skipped count gets
    reported as a new count.
    """

    __slots__ = ('batch_id', 'added', 'skipped', 'files')

    def __init__(self, batch_id, added=0, skipped=0, files=0):
        self.batch_id = batch_id
        self.added = added
        self.skipped = skipped
        self.files = files

    def to_dict(self):
        return {'batch_id': self.batch_id, 'added': self.added,
                'skipped': self.skipped, 'files': self.files}


def serialize(transaction):
    """The public shape of one transaction.

    The single definition of what a transaction looks like to a client, used by
    the API and by the web route's JSON replies. Written out field by field
    rather than reflected off the model on purpose: a column added to
    `Transaction` in a later phase must not silently become part of the public
    contract, and `anomaly_score` is exactly the kind of internal number that
    would.
    """
    return {
        'id': transaction.id,
        'date': transaction.date.strftime('%Y-%m-%d'),
        'description': transaction.description,
        'amount': float(transaction.amount),
        'category': transaction.category,
        'account_name': transaction.account_name,
        'notes': transaction.notes or '',
        'source': transaction.source,
        'import_batch_id': transaction.import_batch_id,
        'is_anomaly': transaction.anomaly_score == -1.0,
        'anomaly_reviewed': bool(transaction.anomaly_reviewed),
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def infer_signed_amount(description, amount, balance, prev_balance):
    """Decide whether a CSV row is money in or money out.

    Lifted verbatim from the upload route, where it was an inline `if/elif`
    chain. Extracted unchanged — including the order of the branches, which is
    load-bearing: 'credit card' is tested before 'credit', so a credit-card
    payment is correctly an outgo rather than being caught by the generic
    'credit' branch and booked as income.

    The bank exports an unsigned magnitude and a description, so the direction
    has to be inferred. The balance-delta fallback at the end is the only branch
    that uses evidence rather than vocabulary, and it is last because it needs a
    previous row — the first row of a file has none.

    Pure, and that is the point of extracting it: this is the part of importing
    that is worth testing directly, and it needed a file on disk and a request
    to reach before.
    """
    lowered = (description or '').lower()

    if ('deposit from 360 checking' in lowered
            or 'deposit from 360 performance savings' in lowered):
        return abs(amount)
    if ('withdrawal to 360 checking' in lowered
            or 'withdrawal to 360 performance savings' in lowered):
        return -abs(amount)
    if 'monthly interest paid' in lowered:
        return abs(amount)
    if 'credit card' in lowered or 'credit crd' in lowered:
        return -abs(amount)
    if 'purchase' in lowered:
        return -abs(amount)
    if 'deposit' in lowered or 'credit' in lowered:
        return abs(amount)
    if 'withdraw' in lowered or 'payment' in lowered:
        return -abs(amount)
    if prev_balance is not None and balance is not None:
        if balance < prev_balance:
            return -abs(amount)
        if balance > prev_balance:
            return abs(amount)
    return amount


def import_csv(paths, account_name, *, batch_id, rules_engine, on_file=None):
    """Import one or more CSV files into `account_name`. Returns an `ImportResult`.

    Takes filesystem paths rather than upload objects, so the sync engine and a
    CLI import can call it too — the route keeps responsibility for saving an
    upload somewhere and removing it afterwards, which is request handling.

    `rules_engine` is passed in rather than resolved here, per this directory's
    rule 2: a service that reaches for configuration is a service that cannot be
    tested without one. It is also resolved once for the whole import rather
    than per row, which is how the route did it.

    Commits per row. See the module docstring — that is what makes a duplicate
    row skip while the rest of the file still imports, and it is the behaviour
    the dedupe index was designed around.
    """
    from models import Transaction, db

    result = ImportResult(batch_id)

    for path in paths:
        frame = pd.read_csv(path)
        frame = frame.sort_values(by='Transaction Date')
        prev_balance = None

        for _index, row in frame.iterrows():
            description = str(row['Transaction Description'])
            date = pd.to_datetime(row['Transaction Date']).date()
            amount = float(row['Transaction Amount'])
            balance = (float(row['Balance'])
                       if not pd.isnull(row['Balance']) else None)

            transaction = Transaction(
                account_name=account_name,
                date=date,
                description=description,
                amount=infer_signed_amount(description, amount, balance,
                                           prev_balance),
                category=rules_engine.get_category(description),
                import_batch_id=batch_id,
            )
            try:
                db.session.add(transaction)
                db.session.flush()
            except IntegrityError:
                # The content-unique index caught a row this household already
                # has. Expected during a re-import of an overlapping export, so
                # it is counted rather than raised.
                db.session.rollback()
                result.skipped += 1
            else:
                db.session.commit()
                result.added += 1

            prev_balance = balance

        result.files += 1
        if on_file is not None:
            on_file(path)

    # Recomputed once for the whole import rather than per file, matching the
    # route. Scoring is a model fit over every transaction in the household, so
    # doing it per file would be the same work repeated.
    compute_anomaly_scores()
    return result


def undo_import(batch_id):
    """Delete every transaction from one import batch. Returns the count.

    Scoped by the ORM backstop like any other bulk delete, so a batch id
    belonging to another household deletes nothing rather than raising — which
    is the right answer, since a caller guessing batch ids must not be able to
    tell a wrong guess from an already-undone one.
    """
    from models import Transaction, db

    deleted = Transaction.query.filter_by(
        import_batch_id=batch_id).delete(synchronize_session='fetch')
    db.session.commit()
    return deleted


# ---------------------------------------------------------------------------
# Single-row writes
# ---------------------------------------------------------------------------

def create_transaction(*, account_name, date, description, amount,
                       category=None, notes=None, source='manual'):
    """Add one transaction by hand. Returns the row.

    `source='manual'` distinguishes it from `'csv'` and `'sync'`, which matters
    for the same reason the column exists: a row a person typed must not be
    deleted by a sync reconciling against what the institution reported.
    """
    from models import Transaction, db

    transaction = Transaction(
        account_name=account_name,
        date=date,
        description=description,
        amount=amount,
        category=category or 'Uncategorized',
        notes=(notes or None),
        source=source,
    )
    db.session.add(transaction)
    db.session.commit()
    return transaction


def update_transaction(transaction_id, changes):
    """Apply `changes` to one owned transaction. Returns the row.

    `changes` is a mapping of already-validated values, keyed by the names in
    `EDITABLE_FIELDS`. Anything else is ignored rather than refused: the caller
    has already validated, and this is the last line of defence rather than the
    place errors are reported from.

    `get_owned` rather than a bare query — the id came from a caller, so the
    household predicate has to be explicit and not merely inherited from the
    backstop. See `dough/tenancy.py` on why that distinction is load-bearing.
    """
    from models import Transaction, db

    transaction = get_owned(Transaction, transaction_id)
    for field in EDITABLE_FIELDS:
        if field in changes:
            setattr(transaction, field, changes[field])
    db.session.commit()
    return transaction


def delete_transaction(transaction_id):
    """Delete one owned transaction."""
    from models import Transaction, db

    transaction = get_owned(Transaction, transaction_id)
    db.session.delete(transaction)
    db.session.commit()
    return transaction


# ---------------------------------------------------------------------------
# Bulk writes
# ---------------------------------------------------------------------------

def bulk_update_category(ids, category):
    """Recategorize many transactions at once. Returns the number changed.

    The count comes from the UPDATE rather than from `len(ids)`, which is what
    the route reported. That was wrong in a way nobody would notice from the web
    UI, where the ids came from rows already on screen: an id belonging to
    another household is filtered out by the backstop and updates nothing, while
    the response claimed it had been changed. An API client passing ids it
    guessed would be told the guess worked.
    """
    from models import Transaction, db

    if not ids:
        return 0
    updated = Transaction.query.filter(Transaction.id.in_(ids)).update(
        {Transaction.category: category}, synchronize_session='fetch')
    db.session.commit()
    return updated


def bulk_delete(ids):
    """Delete many transactions at once. Returns the number deleted."""
    from models import Transaction, db

    if not ids:
        return 0
    deleted = Transaction.query.filter(
        Transaction.id.in_(ids)).delete(synchronize_session='fetch')
    db.session.commit()
    return deleted


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_rows(query):
    """Shape a transaction query into the rows the CSV export writes.

    Separate from the CSV rendering so the API can offer the same columns as
    JSON without a second definition of what "the export" contains. The column
    names are capitalized because they are a spreadsheet's headers, and they are
    what the existing export produced — a person with a saved workbook keyed on
    `Date` should not find it broken by this phase.
    """
    from models import Transaction

    return [{
        'ID': t.id,
        'Date': t.date.strftime('%Y-%m-%d'),
        'Description': t.description,
        'Account': t.account_name,
        'Category': t.category,
        'Amount': float(t.amount),
    } for t in query.order_by(Transaction.date.desc()).all()]
