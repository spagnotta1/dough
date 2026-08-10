"""Flask blueprint: connection pages and the synchronization JSON API."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dough.auth import csrf_exempt, current_user, public
from dough.services import audit
from dough.tenancy import current_household, find_owned
from models import (
    EVENT_CONNECTION_CREATED,
    EVENT_CONNECTION_REMOVED,
    FinancialAccount,
    InstitutionConnection,
    PortfolioSnapshotRow,
    SyncErrorLog,
    SyncRun,
)

from . import plaid_backfill, plaid_webhook
from .adapters import get_adapter_class
from .exceptions import SyncError, UnsupportedInstitutionError
from .repository import SyncRepository
from .scheduler import get_scheduler
from .service import ConnectionService

sync_bp = Blueprint("finance_sync", __name__)

_service = ConnectionService()


def _plaid_client_user_id() -> str:
    """Who Plaid should think is opening Link. One per *person*.

    This used to be the constant `"checkbook-app-user"`, dating from when this
    was a single-user local app, and it survived the move to households as a
    cross-account leak with no database involvement at all.

    `client_user_id` is the key Plaid's returning-user experience remembers
    people by. Send the same one for everybody and Plaid concludes everybody is
    the same person: the second account to open Link is offered the *first*
    account's phone number to send its one-time code to. Nothing in this
    application ever sent that number anywhere — Plaid recalled it, correctly,
    for the user id we told it this was.

    ## Per user, not per household

    Households share connections; they do not share phone numbers. Two people in
    one household are two people, and the number that receives an SMS code
    belongs to whichever of them is sitting there. Scoping this to the household
    would fix the reported bug and leave a smaller one behind.

    ## Why it is derived rather than stored, and why it is not just `user.id`

    An HMAC over the application secret, so the value is:

    - **stable** for a given user in a given deployment, which is what makes the
      returning-user experience work for the person it belongs to;
    - **unique across deployments** even when they share Plaid API credentials.
      Sending the raw row id would put staging's user 1 and production's user 1
      on the same Plaid identity — the same bug again, one level up, and much
      harder to notice;
    - **opaque**, so an internal identifier does not become part of a third
      party's records.

    Rotating `SECRET_KEY` changes it, and the cost of that is bounded: Plaid
    stops recognising the person and asks for their phone number again. Existing
    connections are unaffected — an Item lives on the access token exchanged for
    it, not on this.

    With `AUTH_ENABLED` off there is no user to key on (an install that has
    turned authentication off has said it is one person), so it falls back to
    the bound household, which is still narrower than the constant it replaces.
    """
    user = current_user()
    if user is not None:
        identity = f"user:{user.id}"
    else:
        identity = f"household:{current_household()}"
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode()
    digest = hmac.new(secret, f"plaid:{identity}".encode(), hashlib.sha256)
    # Prefixed so the value is recognisable in a Plaid dashboard, and truncated
    # because 128 bits is far past what distinguishing users needs.
    return "dough-" + digest.hexdigest()[:32]


def _audit_connected(connection, how):
    """One record for the three routes that can create a connection.

    The credential is never in scope here -- `_service.connect*` encrypts it
    into `auth_blob` and hands back the row -- and the metadata below is chosen
    so it stays that way if a future field is added to `to_dict()`.
    """
    audit.record(EVENT_CONNECTION_CREATED, entity_type="connection",
                 entity_id=connection.id,
                 metadata={"institution": connection.institution,
                           "display_name": connection.display_name,
                           "how": how})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@sync_bp.route("/connections")
def connections_page():
    """Manage connected institutions."""
    return render_template(
        "connections.html",
        institutions=_service.list_available(),
        connections=[c.to_dict() for c in InstitutionConnection.query
                     .order_by(InstitutionConnection.display_name).all()],
        accounts=[a.to_dict() for a in FinancialAccount.query
                  .filter_by(is_active=True)
                  .order_by(FinancialAccount.account_type, FinancialAccount.name).all()],
    )


@sync_bp.route("/sync-history")
def sync_history_page():
    """Synchronization run history and error log."""
    runs = (SyncRun.query.order_by(SyncRun.started_at.desc()).limit(100).all())
    errors = (SyncErrorLog.query.order_by(SyncErrorLog.created_at.desc()).limit(50).all())
    return render_template("sync_history.html",
                           runs=[r.to_dict() for r in runs],
                           errors=[e.to_dict() for e in errors])


# ---------------------------------------------------------------------------
# Connection management API
# ---------------------------------------------------------------------------

@sync_bp.route("/api/institutions")
def api_institutions():
    """Institutions available to connect (from the adapter registry)."""
    return jsonify(_service.list_available())


@sync_bp.route("/api/connections", methods=["GET"])
def api_list_connections():
    connections = InstitutionConnection.query.order_by(
        InstitutionConnection.display_name).all()
    return jsonify([c.to_dict() for c in connections])


@sync_bp.route("/api/connections", methods=["POST"])
def api_create_connection():
    """Connect an institution.

    Sandbox institutions connect immediately and start their first sync in
    the background. Live (OAuth-configured) institutions get an authorize
    URL back; the provider redirects to the callback route below.
    """
    data = request.get_json(force=True)
    institution = (data or {}).get("institution", "")
    try:
        state = secrets.token_urlsafe(16)
        redirect_uri = url_for("finance_sync.oauth_callback",
                               institution=institution, _external=True)
        authorize_url = _service.authorization_url(institution, redirect_uri, state)
        if authorize_url:
            session[f"oauth_state_{institution}"] = state
            return jsonify({"authorize_url": authorize_url}), 202
        connection = _service.connect(institution)
    except UnsupportedInstitutionError as exc:
        return jsonify({"error": str(exc)}), 404
    except SyncError as exc:
        return jsonify({"error": str(exc)}), 400
    _audit_connected(connection, "direct")
    scheduler = get_scheduler()
    if scheduler:
        # queue=True: never drop the initial sync, even if another is running
        scheduler.run_sync(trigger="connect", connection_id=connection.id, queue=True)
    return jsonify(connection.to_dict()), 201


@sync_bp.route("/connections/callback/<institution>")
def oauth_callback(institution: str):
    """OAuth redirect target for live-mode institutions."""
    code = request.args.get("code") or request.args.get("oauth_verifier")
    state = request.args.get("state")
    expected_state = session.pop(f"oauth_state_{institution}", None)
    if expected_state and state and state != expected_state:
        flash("That connection didn't complete safely, so I stopped it. Please try connecting again.", "error")
        return redirect(url_for("finance_sync.connections_page"))
    if not code:
        flash("No problem — I've cancelled that connection.", "warning")
        return redirect(url_for("finance_sync.connections_page"))
    try:
        redirect_uri = url_for("finance_sync.oauth_callback",
                               institution=institution, _external=True)
        connection = _service.connect(institution, authorization_code=code,
                                      redirect_uri=redirect_uri)
    except SyncError as exc:
        flash(f"Connection failed: {exc}", "error")
        return redirect(url_for("finance_sync.connections_page"))
    _audit_connected(connection, "oauth")
    scheduler = get_scheduler()
    if scheduler:
        scheduler.run_sync(trigger="connect", connection_id=connection.id, queue=True)
    flash(f"{connection.display_name} is connected — I'm pulling your transactions in now.", "success")
    return redirect(url_for("finance_sync.connections_page"))


@sync_bp.route("/api/plaid/link-token", methods=["POST"])
def api_plaid_link_token():
    """Create a Plaid Link token for the frontend widget to open with."""
    adapter_cls = get_adapter_class("plaid")
    if not adapter_cls.is_live_configured():
        return jsonify({"error": "Plaid is not configured (set PLAID_CLIENT_ID / "
                                  "PLAID_SECRET in .env)"}), 404
    try:
        link_token = adapter_cls().create_link_token(_plaid_client_user_id())
    except SyncError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"link_token": link_token})


@sync_bp.route("/api/connections/plaid/exchange", methods=["POST"])
def api_plaid_exchange():
    """Exchange a Plaid Link `public_token` and store the new linked item."""
    data = request.get_json(force=True) or {}
    public_token = data.get("public_token", "")
    institution_name = data.get("institution_name", "")
    if not public_token:
        return jsonify({"error": "Missing public_token"}), 400
    try:
        connection = _service.connect_plaid(public_token, institution_name)
    except SyncError as exc:
        return jsonify({"error": str(exc)}), 400
    _audit_connected(connection, "plaid_link")
    scheduler = get_scheduler()
    if scheduler:
        scheduler.run_sync(trigger="connect", connection_id=connection.id, queue=True)
    # The connect sync above gets the user something to look at immediately; it
    # does *not* get them their history. Plaid is still backfilling at this
    # point and will answer `has_more: false` to a question it has not finished
    # -- which is how one UAT tester ended up with one month of a promised two
    # years. The watcher is what comes back for the rest.  [UAT round 1]
    #
    # `_get_current_object()`, not `current_app`: the watcher runs on its own
    # thread, and the proxy resolves against *that* thread's app context, which
    # does not exist. It has to be handed the real application.
    plaid_backfill.watch(current_app._get_current_object(), connection.id,
                         connection.household_id)
    return jsonify(connection.to_dict()), 201


@sync_bp.route("/api/plaid/webhook", methods=["POST"])
@public
@csrf_exempt
def api_plaid_webhook():
    """Plaid's notification that an Item has changed.

    `@public` and `@csrf_exempt` because the caller is Plaid's servers: there is
    no session to require and no token to carry. What replaces both is the
    signature check below -- see `finance_sync/plaid_webhook.py`, which is
    fail-closed, and `tests/test_csrf.py`, where this exemption is argued for
    alongside the only other one.

    Always 200 once verified. Plaid retries a non-2xx, and every failure past
    this point (an unknown Item, a webhook type we do not act on, a sync that
    errors) is one that redelivery cannot fix -- retrying it just means handling
    the same dead event five more times.
    """
    if not plaid_webhook.verify(request.get_data(),
                                request.headers.get(plaid_webhook.HEADER)):
        # Deliberately terse. This is an internet-facing endpoint and the
        # response is the one thing an attacker probing it can read; which of
        # the several checks in `verify` refused is in the log, not the body.
        return jsonify({"error": "Invalid signature"}), 401
    payload = request.get_json(silent=True) or {}
    result = plaid_backfill.handle_webhook(current_app._get_current_object(), payload)
    current_app.logger.info("Plaid webhook %s/%s: %s",
                            payload.get("webhook_type"),
                            payload.get("webhook_code"), result)
    return jsonify({"ok": True})


@sync_bp.route("/api/connections/<int:connection_id>", methods=["DELETE"])
def api_delete_connection(connection_id: int):
    # Read before disconnect: afterwards the row is gone and "connection 3 was
    # removed" names nothing.
    doomed = find_owned(InstitutionConnection, connection_id)
    was = ({"institution": doomed.institution,
            "display_name": doomed.display_name} if doomed else {})
    try:
        _service.disconnect(connection_id)
    except SyncError as exc:
        return jsonify({"error": str(exc)}), 404
    # Before the audit record and after the row is gone: a backfill watcher
    # sleeping on this connection would otherwise wake up an hour from now and
    # sync an id that no longer exists.
    plaid_backfill.stop_watching(connection_id)
    audit.record(EVENT_CONNECTION_REMOVED, entity_type="connection",
                 entity_id=connection_id, metadata=was)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Synchronization API
# ---------------------------------------------------------------------------

@sync_bp.route("/api/connections/<int:connection_id>/sync", methods=["POST"])
def api_sync_connection(connection_id: int):
    """Refresh a single institution (background, non-blocking)."""
    if find_owned(InstitutionConnection, connection_id) is None:
        return jsonify({"error": "Connection not found"}), 404
    scheduler = get_scheduler()
    if scheduler is None:
        return jsonify({"error": "Scheduler not running"}), 503
    if not scheduler.run_sync(trigger="manual", connection_id=connection_id):
        return jsonify({"error": "A sync is already in progress"}), 409
    return jsonify({"started": True}), 202


@sync_bp.route("/api/sync/all", methods=["POST"])
def api_sync_all():
    """Refresh every connected institution (background, non-blocking)."""
    scheduler = get_scheduler()
    if scheduler is None:
        return jsonify({"error": "Scheduler not running"}), 503
    if not scheduler.run_sync(trigger="manual"):
        return jsonify({"error": "A sync is already in progress"}), 409
    return jsonify({"started": True}), 202


@sync_bp.route("/api/sync/status")
def api_sync_status():
    """Current background-sync state (polled by the UI)."""
    scheduler = get_scheduler()
    status = scheduler.status() if scheduler else {"running": False}
    connections = InstitutionConnection.query.all()
    status["connections"] = [c.to_dict() for c in connections]
    return jsonify(status)


@sync_bp.route("/api/sync/history")
def api_sync_history():
    limit = min(int(request.args.get("limit", 50)), 200)
    runs = SyncRun.query.order_by(SyncRun.started_at.desc()).limit(limit).all()
    return jsonify([r.to_dict() for r in runs])


@sync_bp.route("/api/accounts")
def api_accounts():
    """All synced financial accounts."""
    accounts = (FinancialAccount.query.filter_by(is_active=True)
                .order_by(FinancialAccount.account_type, FinancialAccount.name).all())
    return jsonify([a.to_dict() for a in accounts])


@sync_bp.route("/api/net-worth")
def api_net_worth():
    """Current totals plus the daily snapshot series for charts."""
    snapshots = (PortfolioSnapshotRow.query
                 .order_by(PortfolioSnapshotRow.snapshot_date).all())
    return jsonify({
        "current": SyncRepository.compute_totals(),
        "history": [s.to_dict() for s in snapshots],
    })
