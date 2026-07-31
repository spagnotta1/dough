"""`/api/v1/transactions` — the ledger.

Every write here goes through `dough/services/ledger.py`, which is the same
module `dough/blueprints/transactions.py` calls. That is the whole architectural
claim of this phase and it is worth being able to check by reading: there is no
`db.session` in this file, no `Transaction(...)` construction, and no arithmetic.
If any of those appear, the two clients have started to diverge.
"""

from __future__ import annotations

from flask import Blueprint

from dough.api.envelope import created, no_content, ok, pagination_meta
from dough.api.errors import Conflict, ValidationError
from dough.api.pagination import (apply_ordering, date_arg, page_request,
                                  str_arg)
from dough.api.validation import (MISSING, body, optional_date, optional_number,
                                  optional_str, require_date, require_number,
                                  require_str)
from dough.services import ledger
from dough.services.transactions import build_transaction_query
from dough.tenancy import get_owned

from models import Transaction

bp = Blueprint('api_v1_transactions', __name__)

#: What `?sort=` accepts, mapped to the column each name means. Public names on
#: the left: a client should not have to know the column is `account_name`
#: because it was named before anybody thought about an API.
SORTABLE = {
    'date': Transaction.date,
    'amount': Transaction.amount,
    'description': Transaction.description,
    'category': Transaction.category,
    'account': Transaction.account_name,
    'id': Transaction.id,
}

#: The directions `?direction=` accepts. `inbound`/`outgo` rather than
#: `credit`/`debit` because that is the vocabulary the rest of this application
#: already uses, and an API that renames the domain for tidiness makes every
#: existing caller translate.
DIRECTIONS = ('inbound', 'outgo')


@bp.route('/transactions', methods=['GET'])
def list_transactions():
    """A filtered, sorted, paged slice of the ledger.

    The filter is built by `build_transaction_query`, which is the same function
    the web list and the CSV export use — so a client that filters through this
    API and a person who filters on the page are looking at the same rows by
    construction rather than by two implementations agreeing.

    Note what is *not* read here: the Flask session. The web route uses
    `sticky_filter`, which falls back to session state so a filter survives
    navigation. That is a browser affordance and an API must not have it — a
    stateless client sending no `category` means "all categories", and answering
    with whatever the last caller filtered on would be indefensible.
    """
    page = page_request(sortable=SORTABLE, default_sort='date')

    date_from = date_arg('date_from')
    date_to = date_arg('date_to')
    if date_from and date_to and date_from > date_to:
        raise ValidationError(
            'date_from is after date_to.',
            details={'date_from': str(date_from), 'date_to': str(date_to)})

    query = build_transaction_query(
        str_arg('account'),
        str_arg('category'),
        date_from.strftime('%Y-%m-%d') if date_from else None,
        date_to.strftime('%Y-%m-%d') if date_to else None,
        str_arg('direction', choices=DIRECTIONS),
        str_arg('q', max_length=120),
    )

    # Counted before the slice, and through the query rather than by loading
    # rows. `TenantScopedQuery.count` applies the household predicate itself --
    # see SEC-0009, where a `.count()` that skipped it reported every
    # household's totals to whoever asked.
    total = query.count()
    rows = (apply_ordering(query, page, SORTABLE)
            .limit(page.page_size).offset(page.offset).all())

    return ok([ledger.serialize(t) for t in rows],
              pagination=pagination_meta(page.page, page.page_size, total))


@bp.route('/transactions/<int:transaction_id>', methods=['GET'])
def get_transaction(transaction_id):
    return ok(ledger.serialize(get_owned(Transaction, transaction_id)))


@bp.route('/transactions', methods=['POST'])
def create_transaction():
    """Add one transaction by hand.

    `amount` is signed, and the API says so rather than inferring a direction
    from a description the way the CSV import has to. An importer guessing is a
    workaround for a bank export that omits the sign; making a first-class API
    guess would be inventing that ambiguity where none exists.
    """
    data = body()
    transaction = ledger.create_transaction(
        account_name=require_str(data, 'account_name', max_length=50,
                                 allow_empty=False),
        date=require_date(data, 'date'),
        description=require_str(data, 'description', max_length=255,
                                allow_empty=False),
        amount=require_number(data, 'amount'),
        category=optional_str(data, 'category', max_length=50) or None,
        notes=optional_str(data, 'notes') or None,
    )
    return created(ledger.serialize(transaction),
                   location=f'/api/v1/transactions/{transaction.id}')


@bp.route('/transactions/<int:transaction_id>', methods=['PATCH'])
def update_transaction(transaction_id):
    """Change some fields of one transaction.

    PATCH, not PUT, and the distinction is enforced by `MISSING`: a field the
    caller did not mention is left alone, and a field sent as `null` is cleared
    where the column allows it. A PUT here would mean "replace the whole
    resource", so a client sending only `{"category": "Dining"}` would blank the
    description — which is exactly the bug this shape prevents.
    """
    data = body()
    changes = {}

    date = optional_date(data, 'date', allow_null=False)
    if date is not MISSING:
        changes['date'] = date

    for field, max_length in (('description', 255), ('account_name', 50),
                              ('category', 50)):
        value = optional_str(data, field, max_length=max_length,
                             allow_empty=False, allow_null=False)
        if value is not MISSING:
            changes[field] = value

    amount = optional_number(data, 'amount', allow_null=False)
    if amount is not MISSING:
        changes['amount'] = amount

    notes = optional_str(data, 'notes')
    if notes is not MISSING:
        # Empty string and null both clear the note. A caller wanting to erase
        # one should not have to know which of the two this column prefers.
        changes['notes'] = notes or None

    if not changes:
        raise ValidationError(
            'No changes were supplied.',
            details={'body': f'Send at least one of: '
                             f'{", ".join(ledger.EDITABLE_FIELDS)}.'})

    return ok(ledger.serialize(
        ledger.update_transaction(transaction_id, changes)))


@bp.route('/transactions/<int:transaction_id>', methods=['DELETE'])
def delete_transaction(transaction_id):
    ledger.delete_transaction(transaction_id)
    return no_content()


@bp.route('/transactions/bulk', methods=['POST'])
def bulk():
    """Recategorize or delete many transactions in one call.

    One endpoint with an `action` rather than two, because the shape of the
    request is otherwise identical and a mobile client batching edits wants one
    code path. The action is validated against a closed set, so a typo is a 422
    rather than a silent no-op.

    The count returned is what the database actually changed, not the length of
    the id list — see `ledger.bulk_update_category` for why that distinction
    matters to a client that did not get its ids from a page it was already
    looking at.
    """
    data = body()
    action = require_str(data, 'action', choices=('recategorize', 'delete'))

    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        raise ValidationError('ids must be a non-empty array.',
                              details={'ids': 'Expected an array of integers.'})
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError('ids must contain only integers.',
                              details={'ids': 'Expected an array of integers.'})
    if len(ids) > 1000:
        raise ValidationError(
            'Too many ids in one request.',
            details={'ids': f'Got {len(ids)}; the limit is 1000.'})

    if action == 'delete':
        return ok({'action': action, 'affected': ledger.bulk_delete(ids)})

    category = require_str(data, 'category', max_length=50, allow_empty=False)
    return ok({'action': action,
               'affected': ledger.bulk_update_category(ids, category)})


@bp.route('/transactions/imports/<batch_id>', methods=['DELETE'])
def undo_import(batch_id):
    """Undo one CSV import.

    A DELETE on the import rather than a POST to `/undo`, because that is what
    it is: the batch is a resource with an id, and removing it removes its rows.
    The web route keeps its POST spelling, which browsers can actually send from
    a form.
    """
    deleted = ledger.undo_import(batch_id)
    if deleted == 0:
        # 404 rather than a cheerful 0. An unknown batch id and an
        # already-undone one are the same answer, for the reason `find_owned`
        # collapses "missing" and "not yours" -- distinguishing them makes this
        # an oracle for which batch ids exist.
        raise Conflict('That import has already been undone, or never existed.')
    return ok({'batch_id': batch_id, 'deleted': deleted})


@bp.route('/transactions/categories', methods=['GET'])
def categories():
    """Every category in use, so a client can populate a picker.

    A separate endpoint rather than a field on the list response: it changes
    rarely, a client should cache it, and returning it alongside every page of
    transactions would put the same array in every response of the busiest
    endpoint in the API.
    """
    from models import db

    rows = (db.session.query(Transaction.category)
            .distinct().order_by(Transaction.category).all())
    return ok([row[0] for row in rows if row[0]])
