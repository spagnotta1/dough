"""Manually entered investment positions.  [Phase 10]

Allowed:   models, dough.tenancy, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`

Extracted from `dough/blueprints/investments.py`. The read side of investments
already lived in `dough/services/networth.py::wealth_snapshot`, which is why the
Investments page, both copilot endpoints and the tests cannot narrate different
numbers. The *write* side did not, so this closes that gap on the same argument.

## The one rule this module enforces

**A synchronized holding cannot be edited by hand.** `source == 'sync'` means
the row is owned by `finance_sync`, which overwrites it on every run. Accepting
an edit would produce the worst kind of failure: the change appears to work,
shows correctly on the page, and is silently reverted at the next sync — so the
user concludes the application loses their data at random.

It was already enforced in the route, as a 409 with a written explanation. It
moves here because the API needs the identical refusal, and a rule that lives in
one of two callers is a rule the other one does not have.

`SyncedHoldingError` carries the message rather than a code, matching
`MembershipError`: there is exactly one thing to say and the caller should not
have to look it up.
"""

from __future__ import annotations

from dough.tenancy import get_owned

__all__ = [
    'ASSET_CLASSES',
    'EDITABLE_FIELDS',
    'HoldingError',
    'SyncedHoldingError',
    'create_holding',
    'delete_holding',
    'list_holdings',
    'update_holding',
]

#: The asset classes the UI offers. Not enforced as a database constraint --
#: a synced holding may report something outside this list and must not be
#: rejected for it -- but used to validate what a person types.
ASSET_CLASSES = ('Stock', 'ETF', 'Mutual Fund', 'Bond', 'Crypto', 'Cash', 'Other')

#: Fields a manual edit may set. Allow-listed for the same reason as
#: `ledger.EDITABLE_FIELDS`: `source`, `account_id` and `external_id` are
#: `finance_sync`'s to write, and a payload that set `source='manual'` on a
#: synced row would take it out of the sync engine's control permanently.
EDITABLE_FIELDS = ('ticker', 'name', 'shares', 'current_value', 'asset_class',
                   'account_name')


class HoldingError(Exception):
    """Base for refusals from this module. Carries a user-safe message."""


class SyncedHoldingError(HoldingError):
    """The holding is maintained by a sync and may not be edited by hand."""


def list_holdings():
    from models import Holding

    return Holding.query.order_by(Holding.asset_class, Holding.ticker).all()


def _editable(holding):
    """Raise unless this holding is one a person may change.

    The message names the account, because "cannot be edited" without saying
    where it comes from leaves the reader with nowhere to go. Managing it from
    the Connections page is the actual next step and the message says so.
    """
    if holding.source == 'sync':
        raise SyncedHoldingError(
            f'This holding is synchronized automatically from '
            f'{holding.account_name} and cannot be edited manually. '
            f'Manage it from the Connections page.')
    return holding


def create_holding(*, ticker, name, shares=0, current_value=0,
                   asset_class='Stock', account_name='Brokerage'):
    """Add a position by hand. Returns the row.

    The ticker is upper-cased here rather than at the two call sites, which is
    the sort of thing that stays consistent only when it happens in one place:
    `vti` and `VTI` are the same position, and storing both makes the allocation
    charts show it twice.
    """
    from models import Holding, db

    holding = Holding(
        ticker=(ticker or '').strip().upper(),
        name=(name or '').strip(),
        shares=shares,
        current_value=current_value,
        asset_class=asset_class,
        account_name=account_name,
        source='manual',
    )
    db.session.add(holding)
    db.session.commit()
    return holding


def update_holding(holding_id, changes):
    """Apply `changes` to an owned, manually-entered holding. Returns the row."""
    from models import Holding, db

    holding = _editable(get_owned(Holding, holding_id))
    for field in EDITABLE_FIELDS:
        if field in changes:
            value = changes[field]
            if field == 'ticker':
                value = (value or '').strip().upper()
            setattr(holding, field, value)
    db.session.commit()
    return holding


def delete_holding(holding_id):
    """Remove an owned, manually-entered holding.

    Guarded by the same rule as editing, and for a stronger reason: deleting a
    synced holding would have it reappear at the next sync, which reads as the
    application ignoring the request entirely.
    """
    from models import Holding, db

    holding = _editable(get_owned(Holding, holding_id))
    db.session.delete(holding)
    db.session.commit()
    return holding
