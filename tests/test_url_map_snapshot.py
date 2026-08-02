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
    # ---------------------------------------------------------------------
    # The versioned API.  [Phase 10]
    #
    # 49 rules added in one commit, deliberately and recorded here rather than
    # silently. Every one of them is a public contract from the moment it
    # exists, which is the reason this file's rule -- update EXPECTED_RULES in
    # the same commit and say so in the message -- matters more for these than
    # for anything above.
    #
    # Registered unconditionally, unlike /login and /household: the URL surface
    # must not depend on the authentication mode, because "this endpoint does
    # not exist" and "you are not signed in" are different facts and a client
    # probing for the second should not be told the first. They therefore do
    # NOT appear in AUTH_ONLY_RULES.
    # ---------------------------------------------------------------------
    ("/api/v1/accounts", ("GET",)),
    ("/api/v1/accounts/<int:account_id>", ("GET",)),
    ("/api/v1/accounts/balances", ("GET",)),
    ("/api/v1/accounts/balances/<account_type>", ("PUT",)),
    ("/api/v1/accounts/connections", ("GET",)),
    ("/api/v1/accounts/net-worth", ("GET",)),
    ("/api/v1/auth/login", ("POST",)),
    ("/api/v1/auth/me", ("GET",)),
    ("/api/v1/auth/tokens", ("GET",)),
    ("/api/v1/auth/tokens", ("POST",)),
    ("/api/v1/auth/tokens/<int:token_id>", ("DELETE",)),
    ("/api/v1/budgets", ("GET",)),
    ("/api/v1/budgets", ("POST",)),
    ("/api/v1/budgets/<int:budget_id>", ("DELETE",)),
    ("/api/v1/chat/conversations", ("GET",)),
    ("/api/v1/chat/conversations", ("POST",)),
    ("/api/v1/chat/conversations/<conversation_id>", ("DELETE",)),
    ("/api/v1/chat/conversations/<conversation_id>", ("GET",)),
    ("/api/v1/chat/conversations/<conversation_id>", ("PATCH",)),
    ("/api/v1/chat/conversations/<conversation_id>/messages", ("DELETE",)),
    ("/api/v1/chat/conversations/<conversation_id>/messages", ("GET",)),
    ("/api/v1/chat/conversations/<conversation_id>/messages", ("POST",)),
    ("/api/v1/copilot/ask", ("POST",)),
    ("/api/v1/copilot/brief", ("GET",)),
    ("/api/v1/copilot/investments/ask", ("POST",)),
    ("/api/v1/copilot/investments/brief", ("GET",)),
    ("/api/v1/household", ("GET",)),
    ("/api/v1/household/activity", ("GET",)),
    ("/api/v1/household/invites", ("GET",)),
    ("/api/v1/household/invites", ("POST",)),
    ("/api/v1/household/invites/<int:invite_id>", ("DELETE",)),
    ("/api/v1/household/members", ("GET",)),
    ("/api/v1/household/members/<int:user_id>", ("DELETE",)),
    ("/api/v1/household/members/<int:user_id>", ("PATCH",)),
    ("/api/v1/investments", ("GET",)),
    ("/api/v1/investments/holdings", ("GET",)),
    ("/api/v1/investments/holdings", ("POST",)),
    ("/api/v1/investments/holdings/<int:holding_id>", ("DELETE",)),
    ("/api/v1/investments/holdings/<int:holding_id>", ("GET",)),
    ("/api/v1/investments/holdings/<int:holding_id>", ("PATCH",)),
    ("/api/v1/settings", ("GET",)),
    ("/api/v1/transactions", ("GET",)),
    ("/api/v1/transactions", ("POST",)),
    ("/api/v1/transactions/<int:transaction_id>", ("DELETE",)),
    ("/api/v1/transactions/<int:transaction_id>", ("GET",)),
    ("/api/v1/transactions/<int:transaction_id>", ("PATCH",)),
    ("/api/v1/transactions/bulk", ("POST",)),
    ("/api/v1/transactions/categories", ("GET",)),
    ("/api/v1/transactions/imports/<batch_id>", ("DELETE",)),
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
    # [Phase 11B] Goal tracking -- the only part of Phase 11 with its own table.
    ("/goals", ("GET",)),
    ("/goals/<int:goal_id>/contribute", ("POST",)),
    ("/goals/<int:goal_id>/delete", ("POST",)),
    ("/goals/<int:goal_id>/edit", ("POST",)),
    ("/goals/new", ("POST",)),
    # [Phase 11A] The consolidated Insights hub. Added, not moved: /anomalies
    # and /recurring below are still served and still linked, they simply no
    # longer each hold a slot in the primary nav.
    ("/insights", ("GET",)),
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
    # Registered unconditionally, like /health: the privacy policy and the terms
    # are read by people deciding whether to sign up, and Plaid's production
    # review fetches the privacy URL with no session.  [Phase 10.7]
    ("/privacy", ("GET",)),
    ("/recurring", ("GET",)),
    ("/recurring/dismiss", ("POST",)),
    ("/recurring/restore", ("POST",)),
    ("/rules", ("GET", "POST")),
    ("/rules/ai-apply", ("POST",)),
    ("/rules/ai-suggest", ("POST",)),
    ("/rules/reorder", ("POST",)),
    ("/rules/test", ("POST",)),
    # ---------------------------------------------------------------------
    # The identity lifecycle.  [Phase 10.5]
    #
    # Nine rules, added deliberately and recorded here. Note what is *not* in
    # this list: `/` is unchanged. The landing page shares the dashboard's rule
    # and branches inside the view (dough/blueprints/core.py), so the marketing
    # page cost the URL surface nothing -- which is why it was done that way.
    # ---------------------------------------------------------------------
    ("/forgot-password", ("GET", "POST")),
    ("/register", ("GET", "POST")),
    ("/reset-password/<token>", ("GET", "POST")),
    ("/settings", ("GET",)),
    ("/settings/delete", ("GET",)),
    ("/settings/delete", ("POST",)),
    ("/settings/email", ("POST",)),
    ("/settings/export", ("GET",)),
    ("/settings/password", ("POST",)),
    ("/settings/sessions/revoke", ("POST",)),
    ("/settings/tokens", ("POST",)),
    ("/settings/tokens/<int:token_id>/revoke", ("POST",)),
    ("/settings/verify-email/resend", ("POST",)),
    ("/verify-email/<token>", ("GET",)),
    ("/setup", ("GET", "POST")),
    ("/static/<path:filename>", ("GET",)),
    ("/sync-history", ("GET",)),
    ("/terms", ("GET",)),
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
    # Phase 10.5. Same condition, same reason: registering, recovering and
    # managing an account all need accounts to exist. `/settings` is in the
    # `settings` blueprint, which `dough/blueprints/__init__.py` registers
    # inside the same `if app.config['AUTH_ENABLED']` block as `auth` and
    # `household`.
    #
    # Note that `/` is deliberately NOT here. It exists in every configuration
    # and always has -- the landing page changed what it renders, not whether
    # the route exists. A URL that appeared and disappeared with the
    # authentication mode would tell a prober the difference between "this
    # endpoint does not exist" and "you are not signed in", which is the same
    # reasoning that keeps `/api/v1` out of this set.
    ("/forgot-password", ("GET", "POST")),
    ("/register", ("GET", "POST")),
    ("/reset-password/<token>", ("GET", "POST")),
    ("/settings", ("GET",)),
    ("/settings/delete", ("GET",)),
    ("/settings/delete", ("POST",)),
    ("/settings/email", ("POST",)),
    ("/settings/export", ("GET",)),
    ("/settings/password", ("POST",)),
    ("/settings/sessions/revoke", ("POST",)),
    ("/settings/tokens", ("POST",)),
    ("/settings/tokens/<int:token_id>/revoke", ("POST",)),
    ("/settings/verify-email/resend", ("POST",)),
    ("/verify-email/<token>", ("GET",)),
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
