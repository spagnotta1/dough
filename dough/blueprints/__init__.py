"""The application's routes, grouped by what they are responsible for.

Phase 7 moved 51 route closures out of `create_app`. The point was never the
line count -- it was that a function 2,700 lines long has no boundaries, so
there is no such thing as "the code that owns budgets", and every change is a
change to the same file.

What stayed in `app.py` is the wiring that genuinely applies to the whole
application: configuration, the database, the tenancy and authentication
request hooks, error handlers, template filters. What moved is everything that
answers a URL.

Two rules for anything added here:

1. **A blueprint may not import `app`.** Anything a route needs from the
   application is reached through `current_app`, and anything it needs from the
   domain comes from `dough/services/`. `tests/test_services.py` enforces this;
   the failure it prevents is a circular import that only appears once two
   blueprints need the same helper.
2. **Endpoint names are `blueprint.view`.** What was `url_for('transactions')` is
   now `url_for('transactions.index')`. `tests/test_url_map_snapshot.py` deliberately
   does *not* assert endpoint names -- it pins the set of (rule, methods) pairs,
   because that is what a browser and a bookmark actually depend on.

`auth` and `household` are registered only when `AUTH_ENABLED`, matching the
`if auth_enabled:` block they came from. With authentication off there is nobody
to sign in, nobody to invite, and `/join` would create a login the application
has no way to use.
"""

from dough.blueprints import (budgets, chat, core, health, insights,
                              investments, log, rules, transactions)

#: Registered in every configuration.
ALWAYS = (core.bp, transactions.bp, insights.bp, budgets.bp, rules.bp, log.bp,
          chat.bp, investments.bp, health.bp)


def register(app):
    """Attach every blueprint this configuration should serve."""
    for bp in ALWAYS:
        app.register_blueprint(bp)

    if app.config['AUTH_ENABLED']:
        # Imported here rather than at module scope so that the import itself
        # is part of the conditional -- an installation with authentication off
        # should not be able to reach these views at all, not even by endpoint
        # name through url_for.
        #
        # `settings` joins them in Phase 10.5 and belongs on the same side of the
        # condition for the same reason: every route in it acts on *an account*
        # -- changing its password, revoking its sessions, listing the tokens it
        # issued -- and with authentication off there is no account to act on.
        # `current_user()` would answer None and every view would fail on the
        # first attribute access.
        from dough.blueprints import auth, household, settings
        app.register_blueprint(auth.bp)
        app.register_blueprint(household.bp)
        app.register_blueprint(settings.bp)
