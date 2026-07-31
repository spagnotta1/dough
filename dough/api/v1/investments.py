"""`/api/v1/investments` — the portfolio, and the positions in it.

The read side is `dough/services/networth.py::wealth_snapshot`, which is the
single derivation the Investments page renders, the two copilot endpoints send
to the model, and the tests assert on. Adding a fourth consumer here is exactly
what that function was built for; recomputing any of it would reintroduce the
possibility of this API reporting a figure the page does not show.

The write side is `dough/services/holdings.py`, which owns the one rule that
matters: a synchronized holding cannot be edited by hand.
"""

from __future__ import annotations

from flask import Blueprint

from dough.api.envelope import created, no_content, ok
from dough.api.errors import Conflict, ValidationError
from dough.api.pagination import int_arg, str_arg
from dough.api.validation import (MISSING, body, optional_number, optional_str,
                                  require_number, require_str)
from dough.services import holdings as holdings_service
from dough.services.networth import wealth_snapshot
from dough.tenancy import get_owned

import investments_intel
from models import Holding

bp = Blueprint('api_v1_investments', __name__)


@bp.route('/investments', methods=['GET'])
def overview():
    """The whole wealth snapshot: allocation, risk, performance, projection.

    One large response rather than eight small ones, and that is a deliberate
    trade against the usual advice. Every part of it comes from a single
    derivation over the same inputs — splitting it into endpoints would mean a
    client assembling a dashboard makes eight calls that each redo the position
    build, and any two of them could be computed either side of a sync and
    disagree. A mobile client on a slow link is better served by one round trip
    it can cache whole.

    The three query parameters are the same ones the web page takes, validated
    the same way, so a client and the page asking identical questions get
    identical answers.
    """
    benchmark = str_arg('benchmark', default='sp500',
                        choices=tuple(investments_intel.BENCHMARKS))
    # Clamped rather than refused, matching the page. A 40-year horizon is the
    # furthest the projection is meaningful over; asking for 100 is a caller
    # exploring, not a caller in error.
    horizon = max(1, min(40, int_arg('horizon', default=10) or 10))
    contribution = _contribution()

    return ok(wealth_snapshot(benchmark, horizon, contribution))


def _contribution():
    """The monthly contribution for the projection, as a non-negative float.

    Read through `str_arg` and converted here rather than via `int_arg` because
    a contribution is money and may have cents, and there is no float variant in
    `pagination` — deliberately, since this is the only place one is wanted and
    a general helper would invite float query parameters where they do not
    belong.
    """
    raw = str_arg('contribution')
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        raise ValidationError('contribution must be a number.',
                              details={'contribution': f'Got {raw!r}.'})


@bp.route('/investments/holdings', methods=['GET'])
def list_holdings():
    """Every position, with cost basis and gain where a basis is known.

    `Holding.to_dict` is reused rather than redefined. It already computes
    `cost_basis`, `gain_loss` and `gain_pct` as properties that return None
    where no basis exists — which is the honest answer for a hand-entered
    holding, and better than a zero that a client would happily average.
    """
    return ok([h.to_dict() for h in holdings_service.list_holdings()])


@bp.route('/investments/holdings/<int:holding_id>', methods=['GET'])
def get_holding(holding_id):
    return ok(get_owned(Holding, holding_id).to_dict())


@bp.route('/investments/holdings', methods=['POST'])
def create_holding():
    data = body()
    holding = holdings_service.create_holding(
        ticker=require_str(data, 'ticker', max_length=20, allow_empty=False),
        name=require_str(data, 'name', max_length=100, allow_empty=False),
        shares=require_number(data, 'shares', minimum=0),
        current_value=require_number(data, 'current_value'),
        asset_class=(optional_str(data, 'asset_class',
                                  choices=holdings_service.ASSET_CLASSES)
                     or 'Stock'),
        account_name=optional_str(data, 'account_name',
                                  max_length=50) or 'Brokerage',
    )
    return created(holding.to_dict(),
                   location=f'/api/v1/investments/holdings/{holding.id}')


@bp.route('/investments/holdings/<int:holding_id>', methods=['PATCH'])
def update_holding(holding_id):
    """Change some fields of a manually-entered holding.

    A synchronized holding answers 409 with the service's message, which names
    the account it comes from and where to manage it. Silently accepting the
    edit would be worse than refusing: it would appear to work and be reverted
    at the next sync, which reads as the application losing data at random.
    """
    data = body()
    changes = {}

    for field, max_length in (('ticker', 20), ('name', 100),
                              ('account_name', 50)):
        value = optional_str(data, field, max_length=max_length,
                             allow_empty=False, allow_null=False)
        if value is not MISSING:
            changes[field] = value

    asset_class = optional_str(data, 'asset_class',
                               choices=holdings_service.ASSET_CLASSES,
                               allow_null=False)
    if asset_class is not MISSING:
        changes['asset_class'] = asset_class

    shares = optional_number(data, 'shares', minimum=0, allow_null=False)
    if shares is not MISSING:
        changes['shares'] = shares

    current_value = optional_number(data, 'current_value', allow_null=False)
    if current_value is not MISSING:
        changes['current_value'] = current_value

    if not changes:
        raise ValidationError(
            'No changes were supplied.',
            details={'body': f'Send at least one of: '
                             f'{", ".join(holdings_service.EDITABLE_FIELDS)}.'})

    try:
        return ok(holdings_service.update_holding(holding_id, changes).to_dict())
    except holdings_service.SyncedHoldingError as exc:
        raise Conflict(str(exc))


@bp.route('/investments/holdings/<int:holding_id>', methods=['DELETE'])
def delete_holding(holding_id):
    try:
        holdings_service.delete_holding(holding_id)
    except holdings_service.SyncedHoldingError as exc:
        raise Conflict(str(exc))
    return no_content()
