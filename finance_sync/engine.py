"""Centralized synchronization engine.

The engine discovers connected institutions, instantiates their adapters via
the registry, and runs the same pipeline for each:

    refresh token → validate connection → adapter.sync() → persist → log

It contains **zero institution-specific logic** — everything provider-shaped
lives inside adapters. Transient failures (network, rate limits, provider
outages) are retried with exponential backoff; every run and every error is
recorded in sync_history / sync_errors.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from dough.tenancy import find_owned
from dough.services import audit
from models import (EVENT_SYNC_COMPLETED, EVENT_SYNC_FAILED,
                    InstitutionConnection, SyncErrorLog, SyncRun, db)

from .adapters import get_adapter_class
from .adapters.base import FinancialInstitutionAdapter
from .crypto import TokenCipher
from .exceptions import SyncError, TokenExpiredError, TransientSyncError
from .repository import SyncRepository

logger = logging.getLogger("finance_sync.engine")


@dataclass
class ConnectionSyncResult:
    """Outcome of syncing one connection."""

    connection_id: int
    institution: str
    status: str  # success | error
    run_id: Optional[int] = None
    error: Optional[str] = None
    transactions_added: int = 0
    holdings_synced: int = 0
    balances_updated: int = 0


@dataclass
class EngineRunResult:
    """Outcome of an engine pass over all connections."""

    trigger: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    results: List[ConnectionSyncResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.results:
            return "success"
        statuses = {r.status for r in self.results}
        if statuses == {"success"}:
            return "success"
        if "success" in statuses:
            return "partial"
        return "error"


class SyncEngine:
    """Loops over all connected adapters and synchronizes each one."""

    def __init__(self, repository: Optional[SyncRepository] = None,
                 cipher: Optional[TokenCipher] = None,
                 max_attempts: int = 3,
                 backoff_base_seconds: float = 0.5):
        self.repository = repository or SyncRepository()
        self.cipher = cipher or TokenCipher()
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds

    # -- public API -----------------------------------------------------------

    def sync_all(self, trigger: str = "manual") -> EngineRunResult:
        """Synchronize every active connection; never raises for one bad bank."""
        run_result = EngineRunResult(trigger=trigger)
        connections = (InstitutionConnection.query
                       .filter(InstitutionConnection.status != "disconnected")
                       .order_by(InstitutionConnection.id).all())
        for connection in connections:
            run_result.results.append(self.sync_connection(connection.id, trigger))
        return run_result

    def sync_connection(self, connection_id: int,
                        trigger: str = "manual") -> ConnectionSyncResult:
        """Synchronize a single connection, recording history and errors."""
        # find_owned, not session.get: session.get answers from the identity
        # map when it can, which is the one lookup the ORM tenant backstop
        # never sees. A sync is the entry point most likely to be handed an
        # id from a different household -- it runs on a background thread
        # against ids read from a table.
        connection = find_owned(InstitutionConnection, connection_id)
        if connection is None:
            return ConnectionSyncResult(
                connection_id=connection_id, institution="unknown",
                status="error", error="Connection not found")

        run = SyncRun(connection_id=connection.id, institution=connection.institution,
                      trigger=trigger, status="running")
        db.session.add(run)
        db.session.commit()

        try:
            adapter = self._build_adapter(connection)
            save = self._sync_with_retries(connection, adapter, run)
            self._persist_credentials_if_changed(connection, adapter)
        except SyncError as exc:
            return self._finish_error(connection, run, exc)
        except Exception as exc:  # defensive: unknown bug must not kill the loop
            logger.exception("Unexpected error syncing %s", connection.institution)
            return self._finish_error(connection, run, exc)

        run.status = "success"
        run.finished_at = datetime.utcnow()
        run.accounts_synced = save.accounts_synced
        run.balances_updated = save.balances_updated
        run.holdings_synced = save.holdings_synced
        run.transactions_added = save.transactions_added
        run.transactions_skipped = save.transactions_skipped
        connection.status = "connected"
        connection.last_sync_at = run.finished_at
        connection.last_sync_status = "success"
        connection.last_error = None
        db.session.commit()
        logger.info("Synced %s: %d accounts, %d holdings, +%d transactions",
                    connection.institution, save.accounts_synced,
                    save.holdings_synced, save.transactions_added)
        # Recorded here rather than in the route: this is the only point every
        # sync passes through. The scheduled ones have no request behind them
        # at all, and they are the ones nobody is watching.
        audit.record(EVENT_SYNC_COMPLETED,
                     household_id=connection.household_id,
                     entity_type='connection', entity_id=connection.id,
                     metadata={'institution': connection.institution,
                               'trigger': trigger, 'run_id': run.id,
                               'accounts': save.accounts_synced,
                               'holdings': save.holdings_synced,
                               'transactions_added': save.transactions_added})
        return ConnectionSyncResult(
            connection_id=connection.id, institution=connection.institution,
            status="success", run_id=run.id,
            transactions_added=save.transactions_added,
            holdings_synced=save.holdings_synced,
            balances_updated=save.balances_updated)

    # -- internals --------------------------------------------------------------

    def _build_adapter(self, connection: InstitutionConnection) -> FinancialInstitutionAdapter:
        """Instantiate the connection's adapter with decrypted credentials."""
        adapter_cls = get_adapter_class(connection.institution)
        credentials = self.cipher.decrypt(connection.auth_blob) if connection.auth_blob else {}
        return adapter_cls(credentials=credentials)

    def _sync_with_retries(self, connection: InstitutionConnection,
                           adapter: FinancialInstitutionAdapter, run: SyncRun):
        """Run the full pipeline, retrying transient failures with backoff."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._refresh_credentials(connection, adapter)
                if not adapter.validate_connection():
                    raise TokenExpiredError(
                        f"{adapter.display_name}: stored credentials are no longer valid")
                payload = adapter.sync()  # fetch + normalize + validate
                return self.repository.save_payload(connection, payload)
            except TransientSyncError as exc:
                last_exc = exc
                self._log_error(run, connection, exc, attempt, is_transient=True)
                if attempt < self.max_attempts:
                    delay = getattr(exc, "retry_after", None) or (
                        self.backoff_base_seconds * (2 ** (attempt - 1)))
                    logger.warning("Transient failure syncing %s (attempt %d/%d): %s — retrying in %.1fs",
                                   connection.institution, attempt, self.max_attempts, exc, delay)
                    time.sleep(delay)
        raise last_exc  # transient failure persisted through all attempts

    def _persist_credentials_if_changed(self, connection: InstitutionConnection,
                                        adapter: FinancialInstitutionAdapter) -> None:
        """Save adapter.credentials back to the connection if sync() mutated
        them (e.g. Plaid advancing its transaction-sync cursor mid-fetch)."""
        blob = self.cipher.encrypt(adapter.credentials)
        if blob != connection.auth_blob:
            connection.auth_blob = blob
            db.session.commit()

    def _refresh_credentials(self, connection: InstitutionConnection,
                             adapter: FinancialInstitutionAdapter) -> None:
        """Refresh the access token if needed and re-store it encrypted."""
        refreshed = adapter.refresh_access_token()
        blob = self.cipher.encrypt(refreshed)
        if blob != connection.auth_blob:
            connection.auth_blob = blob
            expires_at = refreshed.get("expires_at")
            if expires_at:
                connection.token_expires_at = datetime.fromisoformat(expires_at)
            db.session.commit()

    def _log_error(self, run: SyncRun, connection: InstitutionConnection,
                   exc: Exception, attempt: int, is_transient: bool) -> None:
        db.session.add(SyncErrorLog(
            run_id=run.id,
            connection_id=connection.id,
            institution=connection.institution,
            error_type=getattr(exc, "error_type", "unexpected"),
            message=str(exc),
            is_transient=is_transient,
            attempt=attempt,
        ))
        db.session.commit()

    def _finish_error(self, connection: InstitutionConnection, run: SyncRun,
                      exc: Exception) -> ConnectionSyncResult:
        db.session.rollback()  # discard any partial uncommitted writes
        self._log_error(run, connection, exc,
                        attempt=self.max_attempts,
                        is_transient=isinstance(exc, TransientSyncError))
        run.status = "error"
        run.finished_at = datetime.utcnow()
        run.error_message = str(exc)
        connection.status = "expired" if isinstance(exc, TokenExpiredError) else "error"
        connection.last_sync_status = "error"
        connection.last_error = str(exc)
        db.session.commit()
        logger.error("Sync failed for %s: %s", connection.institution, exc)
        # `error_type`, not the message. Adapter exceptions quote provider
        # responses, and a provider response is exactly the kind of string that
        # turns out to contain an item id or a token. The full text is already
        # in SyncErrorLog, which is not append-only and can be pruned.
        audit.record(EVENT_SYNC_FAILED,
                     household_id=connection.household_id,
                     entity_type='connection', entity_id=connection.id,
                     metadata={'institution': connection.institution,
                               'run_id': run.id,
                               'error_type': getattr(exc, 'error_type',
                                                     type(exc).__name__)})
        return ConnectionSyncResult(
            connection_id=connection.id, institution=connection.institution,
            status="error", run_id=run.id, error=str(exc))
