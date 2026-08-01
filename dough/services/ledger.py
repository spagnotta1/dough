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

import os
import re

import pandas as pd
from sqlalchemy.exc import IntegrityError

from dough.services.transactions import compute_anomaly_scores
from dough.tenancy import get_owned

__all__ = [
    'EDITABLE_FIELDS',
    'CsvFormatError',
    'ImportResult',
    'bulk_delete',
    'bulk_update_category',
    'create_transaction',
    'delete_transaction',
    'detect_columns',
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


class CsvFormatError(ValueError):
    """A CSV whose columns could not be understood.

    Carries what was found as well as what was missing, because "I need a date
    column" is not actionable when the user is looking at a file that plainly
    has dates in it — the useful half of the message is which headers were
    actually read.
    """

    def __init__(self, filename, headers, missing):
        self.filename = filename
        self.headers = list(headers)
        self.missing = list(missing)
        super().__init__(
            f'{filename}: could not find {" and ".join(missing)}. '
            f'Columns found: {", ".join(self.headers) or "(none)"}')


def _normalise(header):
    """Fold a header to its comparable form: lowercase, alphanumerics only.

    Banks vary the punctuation and spacing far more than the words —
    'Posted Date', 'posted_date', 'POSTED DATE' and 'Posted  Date' are one
    column with four spellings, and matching on the letters alone collapses
    them without needing a synonym for each.
    """
    return re.sub(r'[^a-z0-9]', '', str(header).lower())


#: Header synonyms, by role. Matched against the normalised form above, so
#: 'Transaction Date' is written here as 'transactiondate'.
#:
#: Ordered: the first match in a file wins, so the more specific spelling comes
#: first. That matters for date — an export with both 'Transaction Date' and
#: 'Posted Date' should book on the transaction date, which is the one the user
#: recognises from their statement.
_SYNONYMS = {
    'date': ('transactiondate', 'postingdate', 'posteddate', 'postdate',
             'dateposted', 'transdate', 'bookingdate', 'effectivedate',
             'valuedate', 'date'),
    'description': ('transactiondescription', 'originaldescription',
                    'description', 'payee', 'merchant', 'merchantname',
                    'narrative', 'particulars', 'details', 'memo', 'name',
                    'reference'),
    'amount': ('transactionamount', 'amount', 'amountusd', 'netamount',
               'value'),
    'debit': ('debit', 'debitamount', 'withdrawal', 'withdrawals',
              'withdrawalamount', 'moneyout', 'paidout', 'outflow', 'charges'),
    'credit': ('credit', 'creditamount', 'deposit', 'deposits',
               'depositamount', 'moneyin', 'paidin', 'inflow', 'payments'),
    'balance': ('runningbalance', 'closingbalance', 'ledgerbalance',
                'balanceamount', 'balance'),
}


def detect_columns(headers):
    """Map this file's headers onto the roles the importer needs.

    The importer used to hardcode Capital One's four column names, so every
    other bank's export raised `KeyError` and reached the user as a 500 with a
    trace id and no hint that the file was the problem.

    Two shapes of export exist and both are common enough to be required:

      **one signed-or-inferred amount column** — Capital One's checking export
      and most others. The magnitude may be unsigned, so the direction is still
      inferred by `infer_signed_amount`.

      **separate debit and credit columns** — Capital One's *credit card*
      export, and the default for most UK and many US banks. Here the direction
      is explicit and must not be second-guessed: a row with 42.00 under Debit
      is money out no matter what its description says.

    Returns a dict with keys date, description, amount, debit, credit, balance;
    the ones that do not apply are None. Raises `CsvFormatError` when a file
    cannot be read at all, which the upload route turns into a sentence.
    """
    lookup = {}
    for header in headers:
        lookup.setdefault(_normalise(header), header)

    found = {}
    for role, names in _SYNONYMS.items():
        found[role] = next((lookup[n] for n in names if n in lookup), None)

    missing = []
    if not found['date']:
        missing.append('a date column')
    if not found['description']:
        missing.append('a description column')
    if not found['amount'] and not (found['debit'] or found['credit']):
        missing.append('an amount column (or debit/credit columns)')
    if missing:
        raise CsvFormatError('', headers, missing)

    # A file carrying both shapes is not ambiguous: the signed column is the
    # whole truth and the debit/credit pair is a redundant presentation of it.
    if found['amount']:
        found['debit'] = found['credit'] = None
    return found


def _money(value):
    """A number out of whatever the bank wrote in the cell.

    Exports are full of presentation: '$1,234.56', '1 234,56', '(45.00)' for a
    negative, an empty cell for "not this column". Feeding any of those to
    `float()` raises, and in a per-row loop that aborts an import most of the
    way through.
    """
    if value is None or (isinstance(value, float) and pd.isnull(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text in {'-', '--'}:
        return 0.0

    negative = text.startswith('(') and text.endswith(')')
    text = re.sub(r'[^0-9.,\-]', '', text)
    # A comma is a thousands separator when a dot is also present, and the
    # decimal mark when it is not ('1.234,56' and '1234,56' are both European).
    if ',' in text and '.' in text:
        text = text.replace(',', '') if text.rfind('.') > text.rfind(',') \
            else text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.') if len(text.split(',')[-1]) in (1, 2) \
            else text.replace(',', '')
    if not text or text in {'-', '.'}:
        return 0.0

    number = float(text)
    return -abs(number) if negative else number


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
        try:
            cols = detect_columns(frame.columns)
        except CsvFormatError as exc:
            # Re-raised with the filename attached: `paths` are temp files the
            # route saved, and the user needs to know *which* of the several
            # they dropped in was the one it could not read.
            raise CsvFormatError(os.path.basename(str(path)),
                                 exc.headers, exc.missing) from None

        frame = frame.sort_values(by=cols['date'])
        prev_balance = None

        for _index, row in frame.iterrows():
            description = str(row[cols['description']])
            date = pd.to_datetime(row[cols['date']]).date()
            balance = (_money(row[cols['balance']])
                       if cols['balance'] and not pd.isnull(row[cols['balance']])
                       else None)

            if cols['amount']:
                # One column, magnitude possibly unsigned — the direction is
                # inferred from the description, with the balance delta as the
                # fallback. This is the path Capital One's checking export takes
                # and its behaviour is unchanged.
                amount = infer_signed_amount(
                    description, _money(row[cols['amount']]), balance,
                    prev_balance)
            else:
                # Debit and credit columns: the direction is stated, so it is
                # taken rather than inferred. Guessing here would let a row
                # described as "PAYMENT" under Credit be booked as an outgo.
                debit = _money(row[cols['debit']]) if cols['debit'] else 0.0
                credit = _money(row[cols['credit']]) if cols['credit'] else 0.0
                amount = abs(credit) - abs(debit)

            transaction = Transaction(
                account_name=account_name,
                date=date,
                description=description,
                amount=amount,
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
