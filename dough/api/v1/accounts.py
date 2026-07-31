"""`/api/v1/accounts` — every place this household keeps money.

The merge across synced accounts, hand-typed balances and CSV-only account names
lives in `dough/services/accounts.py`, not here. See that module for why the
three kinds are reported distinctly rather than flattened into one list of
numbers that look equally trustworthy.
"""

from __future__ import annotations

from flask import Blueprint

from dough.api.envelope import ok
from dough.api.errors import NotFound, ValidationError
from dough.api.pagination import str_arg
from dough.api.validation import body, require_number
from dough.services import accounts, audit
from dough.services.networth import compute_net_worth, portfolio_snapshot

from models import EVENT_ACCOUNT_BALANCE_SET

bp = Blueprint('api_v1_accounts', __name__)


@bp.route('/accounts', methods=['GET'])
def list_accounts():
    """Every account, whatever kind. Optionally filtered to one kind.

    Not paged, deliberately, and it is the only collection in v1 that is not. A
    household has a handful of accounts, not thousands; paging it would make
    every client write a loop to reassemble something that always fits in one
    response. The convention in `docs/api/README.md` says collections page —
    this is the stated exception rather than an oversight.
    """
    kind = str_arg('kind', choices=accounts.ACCOUNT_KINDS)
    entries = accounts.overview()
    if kind:
        entries = [e for e in entries if e['kind'] == kind]
    return ok(entries)


@bp.route('/accounts/net-worth', methods=['GET'])
def net_worth():
    """The net-worth breakdown, from the same derivation the dashboard renders.

    `compute_net_worth` is `SyncRepository.compute_totals`, which is what the
    dashboard, the AI context builders and the nightly snapshot all read. A
    second calculation here would be a second answer to "what is this family
    worth", and the two would diverge the first time either changed.
    """
    return ok({'net_worth': compute_net_worth(),
               'portfolio': portfolio_snapshot()})


@bp.route('/accounts/balances', methods=['GET'])
def list_balances():
    """The hand-entered starting balances only.

    Kept as its own endpoint alongside `/accounts` because it is the collection
    the PUT below writes to, and a client that just set a balance wants to read
    back the thing it wrote rather than filter it out of a merged list.
    """
    return ok([b.to_dict() for b in accounts.manual_balances()])


@bp.route('/accounts/balances/<account_type>', methods=['PUT'])
def set_balance(account_type):
    """Set a hand-entered starting balance.

    PUT rather than POST because the resource is identified by its type and the
    operation is idempotent — setting checking to 1200 twice leaves it at 1200,
    and creating the row if absent is what makes the URL addressable before it
    exists.

    Audited, because this is one of the few places a person can change a figure
    that feeds net worth without a corresponding transaction. `from`/`to` are
    both recorded: "balance set to 1200" does not say what an incident review
    needs, which is what it was before.
    """
    account_type = (account_type or '').strip()
    if not account_type or len(account_type) > 50:
        raise ValidationError('Unusable account type.',
                              details={'account_type': 'Expected 1-50 characters.'})

    balance, previous = accounts.set_manual_balance(
        account_type, require_number(body(), 'starting_balance'))

    audit.record(EVENT_ACCOUNT_BALANCE_SET, entity_type='account_balance',
                 entity_id=balance.id,
                 metadata={'account_type': account_type, 'from': previous,
                           'to': balance.starting_balance, 'surface': 'api'})
    return ok(balance.to_dict())


@bp.route('/accounts/connections', methods=['GET'])
def list_connections():
    """Linked institutions and their sync health.

    Read-only here. Creating and removing a connection is an OAuth dance with a
    browser redirect in the middle of it, which `finance_sync/routes.py` owns
    and which a JSON API cannot usefully wrap — so v1 reports connection state
    and points a client at the web flow rather than pretending to offer one.
    """
    last_sync = accounts.last_sync_at()
    return ok({
        'connections': [c.to_dict() for c in accounts.connections()],
        'last_sync_at': last_sync.isoformat() + 'Z' if last_sync else None,
    })


@bp.route('/accounts/<int:account_id>', methods=['GET'])
def get_account(account_id):
    """One synced account.

    Only synced accounts have an id — a hand-typed balance is keyed by type and
    a CSV account name is keyed by nothing at all. That is why this route is
    `<int:account_id>` and the other two kinds are reached through their own
    collections rather than through a shared identifier that two of the three
    would have to invent.
    """
    for entry in accounts.overview():
        if entry['kind'] == 'synced' and entry['id'] == account_id:
            return ok(entry)
    raise NotFound('No such account.')
