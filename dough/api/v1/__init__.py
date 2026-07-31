"""The v1 surface: nine resources, one URL prefix, one response shape.

Each module holds one resource's routes and nothing else. The rules, repeated
from `dough/blueprints/__init__.py` because they apply here at least as
strongly:

1. **No business logic.** A module here reads the request, calls something in
   `dough/services/`, and shapes a response. No `db.session` writes outside a
   service, no arithmetic, no domain rules. This is the architectural claim of
   Phase 10 and it is checkable by reading: `tests/test_api_v1.py` asserts these
   modules import no model-mutating helpers directly.
2. **A resource module may not import `app` or another blueprint.** Anything
   from the application comes through `current_app`.
3. **Endpoint names are `api_v1_<resource>.<view>`.** The prefix keeps them from
   colliding with the HTML blueprints, several of which have a `list` or an
   `index` too.

`url_prefix` is set once, here, rather than in each blueprint. That is what
makes `/api/v2` a change to one line plus a new package, and it means no module
can accidentally register itself outside the versioned namespace -- which would
be a route that is public API without ever having been reviewed as one.
"""

from dough.api.v1 import (accounts, auth, budgets, chat, copilot, household,
                          investments, settings, transactions)

#: Mounted in this order. Order does not matter to routing -- no two claim the
#: same rule, which `tests/test_url_map_snapshot.py` would catch -- so it is
#: alphabetical, which is the order a person looks for something in.
RESOURCES = (
    accounts.bp,
    auth.bp,
    budgets.bp,
    chat.bp,
    copilot.bp,
    household.bp,
    investments.bp,
    settings.bp,
    transactions.bp,
)

URL_PREFIX = '/api/v1'


def register(app):
    """Mount every v1 resource under `/api/v1`."""
    for blueprint in RESOURCES:
        app.register_blueprint(blueprint, url_prefix=URL_PREFIX)
