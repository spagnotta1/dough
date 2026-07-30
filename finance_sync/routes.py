"""Flask blueprint: connection pages and the synchronization JSON API."""

from __future__ import annotations

import secrets

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dough.services import audit
from dough.tenancy import find_owned
from models import (
    EVENT_CONNECTION_CREATED,
    EVENT_CONNECTION_REMOVED,
    FinancialAccount,
    InstitutionConnection,
    PortfolioSnapshotRow,
    SyncErrorLog,
    SyncRun,
)

from .adapters import get_adapter_class
from .exceptions import SyncError, UnsupportedInstitutionError
from .repository import SyncRepository
from .scheduler import get_scheduler
from .service import ConnectionService

sync_bp = Blueprint("finance_sync", __name__)

_service = ConnectionService()

# Single-user local app (see README) — Plaid still requires a stable
# per-end-user id to scope Link sessions to.
_PLAID_CLIENT_USER_ID = "checkbook-app-user"


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
        link_token = adapter_cls().create_link_token(_PLAID_CLIENT_USER_ID)
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
    return jsonify(connection.to_dict()), 201


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
