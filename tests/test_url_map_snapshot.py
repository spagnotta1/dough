"""A frozen snapshot of every URL the application serves.

This was the safety net for the blueprint extraction, which happened in Phase 7.
Moving 51 route closures out of `create_app` into blueprints was a mechanical
change that is very easy to get subtly wrong -- an endpoint renamed, a method
dropped, a converter changed from `<int:id>` to `<id>`. None of those show up
as an import error; they show up as a 404 or a 405 in production.

It passed unchanged through that extraction, which is the evidence that no path
and no method moved. It keeps earning its place afterwards for the same reason:
a route added to the wrong blueprint, or given a `url_prefix` that shifts its
path, still fails here.

So the rule is: extraction may change *endpoint names* (that is the point --
`transactions` becomes `transactions.list`), but it may never change the set of
(rule, methods) pairs the app answers on. If you are deliberately adding or
removing a route, update EXPECTED_RULES in the same commit and say so in the
commit message. If you did not mean to change the URL surface, this test just
caught a bug.

Endpoint names are deliberately NOT asserted here, for that reason.
"""

import pytest

from app import create_app


# (rule, sorted methods excluding HEAD/OPTIONS) with AUTH_ENABLED=True.
# Generated from `app.url_map`; keep sorted.
EXPECTED_RULES = {
    ("/", ("GET",)),
    ("/anomalies", ("GET",)),
    ("/anomalies/<int:transaction_id>/dismiss", ("POST",)),
    ("/anomalies/dismiss_all", ("POST",)),
    ("/api/accounts", ("GET",)),
    ("/api/chat", ("POST",)),
    ("/api/chat_clear", ("POST",)),
    ("/api/chat_history", ("GET",)),
    ("/api/chat_stream", ("POST",)),
    ("/api/chat_truncate", ("POST",)),
    ("/api/connections", ("GET",)),
    ("/api/connections", ("POST",)),
    ("/api/connections/<int:connection_id>", ("DELETE",)),
    ("/api/connections/<int:connection_id>/sync", ("POST",)),
    ("/api/connections/plaid/exchange", ("POST",)),
    ("/api/conversations", ("GET",)),
    ("/api/conversations", ("POST",)),
    ("/api/conversations/<conv_id>", ("DELETE",)),
    ("/api/conversations/<conv_id>", ("PATCH",)),
    ("/api/copilot/ask", ("POST",)),
    ("/api/copilot/brief", ("GET",)),
    ("/api/dashboard-insight", ("GET",)),
    ("/api/holdings", ("POST",)),
    ("/api/holdings/<int:hid>", ("DELETE", "PUT")),
    ("/api/institutions", ("GET",)),
    ("/api/investments/ask", ("POST",)),
    ("/api/investments/brief", ("GET",)),
    ("/api/log/balances", ("GET",)),
    ("/api/log/balances/<account_type>", ("PUT",)),
    ("/api/log/clear", ("POST",)),
    ("/api/log/entries", ("GET",)),
    ("/api/log/entries", ("POST",)),
    ("/api/log/entries/<int:entry_id>", ("DELETE",)),
    ("/api/log/entries/<int:entry_id>", ("PUT",)),
    ("/api/net-worth", ("GET",)),
    ("/api/plaid/link-token", ("POST",)),
    ("/api/sync/all", ("POST",)),
    ("/api/sync/history", ("GET",)),
    ("/api/sync/status", ("GET",)),
    ("/budgets", ("GET", "POST")),
    ("/chat", ("GET",)),
    ("/clear_filters", ("GET",)),
    ("/connections", ("GET",)),
    ("/connections/callback/<institution>", ("GET",)),
    ("/export", ("GET",)),
    # Added in Phase 8, deliberately and recorded here rather than silently:
    # these are the two URLs a supervisor and a load balancer call, and they are
    # public by design (a health check behind a login is not a health check).
    ("/health/live", ("GET",)),
    ("/health/ready", ("GET",)),
    ("/household", ("GET",)),
    ("/household/invites", ("POST",)),
    ("/household/invites/<int:invite_id>/revoke", ("POST",)),
    ("/household/members/<int:user_id>/remove", ("POST",)),
    ("/household/members/<int:user_id>/role", ("POST",)),
    ("/import/<batch_id>/undo", ("POST",)),
    ("/investments", ("GET",)),
    ("/join/<token>", ("GET", "POST")),
    ("/login", ("GET", "POST")),
    ("/logout", ("POST",)),
    ("/recurring", ("GET",)),
    ("/recurring/dismiss", ("POST",)),
    ("/recurring/restore", ("POST",)),
    ("/rules", ("GET", "POST")),
    ("/rules/ai-apply", ("POST",)),
    ("/rules/ai-suggest", ("POST",)),
    ("/rules/reorder", ("POST",)),
    ("/rules/test", ("POST",)),
    ("/setup", ("GET", "POST")),
    ("/static/<path:filename>", ("GET",)),
    ("/sync-history", ("GET",)),
    ("/transactions", ("GET",)),
    ("/transactions/<int:transaction_id>", ("DELETE",)),
    ("/transactions/<int:transaction_id>", ("PUT",)),
    ("/transactions/bulk_delete", ("POST",)),
    ("/update_categories_bulk", ("POST",)),
    ("/update_category", ("POST",)),
    ("/upload", ("GET", "POST")),
}

# Routes that only exist when AUTH_ENABLED is on -- they are defined inside the
# `if auth_enabled:` block in app.py, which is why the test suite (TESTING=True,
# auth off by default) does not see them.
AUTH_ONLY_RULES = {
    # Membership needs accounts to exist: there is nobody to invite, nobody to
    # promote, and /join creates a login.  [Phase 6]
    ("/household", ("GET",)),
    ("/household/invites", ("POST",)),
    ("/household/invites/<int:invite_id>/revoke", ("POST",)),
    ("/household/members/<int:user_id>/remove", ("POST",)),
    ("/household/members/<int:user_id>/role", ("POST",)),
    ("/join/<token>", ("GET", "POST")),
    ("/login", ("GET", "POST")),
    ("/logout", ("POST",)),
    ("/setup", ("GET", "POST")),
}


def url_map_of(app):
    return {
        (str(rule.rule), tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in app.url_map.iter_rules()
    }


@pytest.fixture()
def auth_on_app(tmp_path):
    import finance_sync.scheduler as scheduler_module
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    yield application
    scheduler_module._scheduler = None


def test_url_map_matches_snapshot(auth_on_app):
    actual = url_map_of(auth_on_app)

    added = actual - EXPECTED_RULES
    removed = EXPECTED_RULES - actual
    assert not added and not removed, (
        "The URL surface changed.\n"
        f"  Added:   {sorted(added)}\n"
        f"  Removed: {sorted(removed)}\n"
        "If this was intentional, update EXPECTED_RULES in this file."
    )


def test_auth_routes_only_exist_when_auth_enabled(app):
    """`app` is the shared fixture, which runs with auth off.

    This documents a real quirk: /login, /logout and /setup are not merely
    unguarded under TESTING, they do not exist at all, because they are
    defined inside `if auth_enabled:` (app.py). Any test that expects a 302
    to /login must build its own app with AUTH_ENABLED=True.
    """
    actual = url_map_of(app)
    assert actual == EXPECTED_RULES - AUTH_ONLY_RULES


def test_no_duplicate_rule_method_pairs(auth_on_app):
    """Two views answering the same (rule, method) is always a bug.

    Flask allows it -- the first registration wins and the second is silently
    shadowed. That is exactly the failure mode blueprint extraction can
    introduce when a route is moved but the original is not deleted.
    """
    seen = {}
    for rule in auth_on_app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            key = (str(rule.rule), method)
            assert key not in seen, (
                f"{method} {rule.rule} is served by both {seen[key]!r} "
                f"and {rule.endpoint!r}"
            )
            seen[key] = rule.endpoint
