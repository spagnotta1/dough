"""`/api/v1/budgets` — monthly targets and where each one stands.

All arithmetic is `dough/services/budgets.py`. This module reads a body, calls
the service, and shapes a response.
"""

from __future__ import annotations

from flask import Blueprint

from dough.api.envelope import created, no_content, ok
from dough.api.validation import body, require_number, require_str
from dough.services import budgets as budget_service

bp = Blueprint('api_v1_budgets', __name__)


@bp.route('/budgets', methods=['GET'])
def list_budgets():
    """Every budget with its spend, band and month-over-month change.

    Returns the status view rather than the bare limits, because a budget
    without its spend cannot answer the only question anybody asks of one. A
    client wanting just the limits reads the same fields and ignores the rest;
    a client wanting the progress would otherwise have to fetch transactions and
    recompute it, which is the duplication this phase exists to prevent.

    Unpaged for the same reason `/accounts` is: a household has a handful.
    """
    return ok(budget_service.status())


@bp.route('/budgets', methods=['POST'])
def upsert_budget():
    """Set a budget for a category.

    An upsert, which is why it answers 201 or 200 rather than 201 or 409.
    Setting a budget for an already-budgeted category is changing the number,
    not a conflict — and a client that had to GET first, branch, then POST or
    PATCH would be doing three round trips to express one intent.

    `created` in the response body says which happened, so a client can word its
    confirmation correctly without comparing the status code it may not have
    kept.
    """
    data = body()
    budget, was_created = budget_service.upsert_budget(
        require_str(data, 'category', max_length=50, allow_empty=False),
        require_str(data, 'account_name', max_length=50,
                    allow_empty=False) if 'account_name' in data
        else budget_service.ACCOUNT_ANY,
        require_number(data, 'monthly_limit', minimum=0),
    )
    payload = {**budget_service.serialize(budget), 'created': was_created}
    if was_created:
        return created(payload, location=f'/api/v1/budgets/{budget.id}')
    return ok(payload)


@bp.route('/budgets/<int:budget_id>', methods=['DELETE'])
def delete_budget(budget_id):
    budget_service.delete_budget(budget_id)
    return no_content()
