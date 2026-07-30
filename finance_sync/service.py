"""Connection lifecycle service.

Handles connecting/disconnecting institutions and encrypted token storage.
Routes talk to this service; the service talks to adapters and the engine —
no raw provider access anywhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from dough.contrast import on_color
from dough.tenancy import find_owned
from models import InstitutionConnection, db

from .adapters import available_institutions, get_adapter_class
from .crypto import TokenCipher
from .exceptions import ConfigurationError


class ConnectionService:
    """Create, list, and remove institution connections."""

    def __init__(self, cipher: Optional[TokenCipher] = None):
        self.cipher = cipher or TokenCipher()

    def list_available(self) -> List[dict]:
        """Institutions the user can connect, with live/sandbox capability info."""
        connected = {c.institution for c in InstitutionConnection.query
                     .filter(InstitutionConnection.status != "disconnected").all()}
        catalog = []
        for cls in available_institutions():
            catalog.append({
                "institution": cls.institution,
                "display_name": cls.display_name,
                "auth_type": cls.auth_type,
                "mode": "live" if cls.is_live_configured() else "sandbox",
                "live_api_available": cls.live_api_available,
                "supports_transactions": cls.supports_transactions,
                "supports_holdings": cls.supports_holdings,
                "accent_color": cls.accent_color,
                # Derived here, not in CSS or JS: the brand colour is data, so
                # no theme token can know what it will be laid over, and a
                # script that derives it leaves the label unreadable until it
                # runs. Plaid's #000000 made that visible — black on black.
                "ink_color": on_color(cls.accent_color),
                "connected": cls.institution in connected,
            })
        return catalog

    def connect(self, institution: str,
                authorization_code: Optional[str] = None,
                redirect_uri: Optional[str] = None) -> InstitutionConnection:
        """Connect an institution: authenticate, store encrypted tokens.

        Re-connecting an existing (e.g. expired) connection refreshes its
        credentials in place instead of creating a duplicate.
        """
        adapter_cls = get_adapter_class(institution)
        if institution == "plaid" and adapter_cls.is_live_configured():
            # Plaid's live handshake needs a Link token + public_token exchange,
            # not this generic authorization_code flow — see connect_plaid().
            raise ConfigurationError(
                "Plaid is live-configured; use the Plaid Link connect flow "
                "instead of the generic connection endpoint")
        adapter = adapter_cls()
        credentials = adapter.connect(authorization_code=authorization_code,
                                      redirect_uri=redirect_uri)

        connection = InstitutionConnection.query.filter_by(institution=institution).first()
        if connection is None:
            connection = InstitutionConnection(
                institution=institution,
                display_name=adapter_cls.display_name,
            )
            db.session.add(connection)
        connection.status = "connected"
        connection.auth_blob = self.cipher.encrypt(credentials)
        expires_at = credentials.get("expires_at")
        connection.token_expires_at = (
            datetime.fromisoformat(expires_at) if expires_at else None)
        connection.last_error = None
        db.session.commit()
        return connection

    def connect_plaid(self, public_token: str, item_display_name: str) -> InstitutionConnection:
        """Exchange a Plaid Link `public_token` and store the new item.

        Each linked institution becomes its own connection row, sharing
        ``institution="plaid"`` but distinguished by ``item_id`` — unlike the
        single-item adapters, connecting a second institution here creates a
        new row rather than replacing the first.
        """
        adapter_cls = get_adapter_class("plaid")
        if not adapter_cls.is_live_configured():
            raise ConfigurationError(
                "Plaid is not configured (set PLAID_CLIENT_ID / PLAID_SECRET in .env)")
        adapter = adapter_cls()
        credentials = adapter.connect_with_public_token(public_token)
        item_id = credentials["item_id"]

        connection = InstitutionConnection.query.filter_by(
            institution="plaid", item_id=item_id).first()
        if connection is None:
            connection = InstitutionConnection(institution="plaid", item_id=item_id)
            db.session.add(connection)
        connection.display_name = item_display_name or adapter_cls.display_name
        connection.status = "connected"
        connection.auth_blob = self.cipher.encrypt(credentials)
        expires_at = credentials.get("expires_at")
        connection.token_expires_at = (
            datetime.fromisoformat(expires_at) if expires_at else None)
        connection.last_error = None
        db.session.commit()
        return connection

    def authorization_url(self, institution: str, redirect_uri: str,
                          state: str) -> Optional[str]:
        """OAuth authorize URL when the institution runs in live mode."""
        adapter_cls = get_adapter_class(institution)
        if not adapter_cls.is_live_configured():
            return None
        return adapter_cls().authorization_url(redirect_uri, state)

    def disconnect(self, connection_id: int) -> None:
        """Revoke provider tokens (best effort) and remove the connection.

        Synced financial accounts, holdings, and sync history are removed with
        the connection; imported transactions are kept (they are the user's
        historical record, same as CSV imports).
        """
        connection = find_owned(InstitutionConnection, connection_id)
        if connection is None:
            raise ConfigurationError(f"Connection {connection_id} not found")
        adapter_cls = get_adapter_class(connection.institution)
        credentials = self.cipher.decrypt(connection.auth_blob) if connection.auth_blob else {}
        adapter_cls(credentials=credentials).disconnect()

        from models import Holding, SyncRun, Transaction
        account_ids = [a.id for a in connection.accounts]
        if account_ids:
            # Detach imported transactions from the accounts we're deleting.
            (Transaction.query.filter(Transaction.account_id.in_(account_ids))
             .update({Transaction.account_id: None}, synchronize_session=False))
            Holding.query.filter(Holding.account_id.in_(account_ids)).delete(
                synchronize_session=False)
        (SyncRun.query.filter_by(connection_id=connection.id)
         .update({SyncRun.connection_id: None}, synchronize_session=False))
        db.session.delete(connection)  # cascades to financial_accounts
        db.session.commit()
