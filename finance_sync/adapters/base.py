"""Abstract base adapter — the single seam between this app and any bank API.

Every institution adapter implements the same interface; no other component
in the application may talk to a provider API directly. The base class is a
template method design: ``sync()`` / ``get_*()`` orchestrate the pipeline and
subclasses supply only raw fetching and normalization.

Live vs sandbox
---------------
An adapter runs **live** when its API credentials are configured via
environment variables (see each subclass's ``required_env``); otherwise it
transparently uses the institution's deterministic sandbox backend so the
whole application works without external credentials.
"""

from __future__ import annotations

import os
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, List, Optional, Tuple

import requests

from ..canonical import (
    Balance,
    Holding,
    InstitutionAccount,
    SyncPayload,
    Transaction,
)
from ..exceptions import (
    AuthenticationError,
    NetworkError,
    ProviderOutageError,
    RateLimitError,
    TokenExpiredError,
)
from ..sandbox import SandboxBackend, get_sandbox

TOKEN_LIFETIME_HOURS = 24


class FinancialInstitutionAdapter(ABC):
    """Common interface every financial institution adapter must implement."""

    # -- class-level metadata (override per institution) --------------------
    institution: ClassVar[str] = ""            # slug, e.g. "coinbase"
    display_name: ClassVar[str] = ""           # e.g. "Coinbase"
    auth_type: ClassVar[str] = "oauth2"        # oauth2 | oauth1 | api_key
    required_env: ClassVar[Tuple[str, ...]] = ()  # env vars enabling live mode
    supports_transactions: ClassVar[bool] = True
    supports_holdings: ClassVar[bool] = True
    live_api_available: ClassVar[bool] = True  # False = no public API exists
    # UI badge colour. The catalog derives the label ink from this
    # (dough/contrast.py), and an adapter that does not override it inherits
    # whatever accessibility this value has: the previous #6366f1 topped out
    # at 4.47:1 against white and 4.35:1 against the near-black — no ink
    # clears AA on it, because none exists. This one carries white at 6.3:1.
    accent_color: ClassVar[str] = "#4f46e5"
    # Whether to offer this institution in the "Add an Institution" catalog.
    # False withdraws it from the picker *only* — the adapter stays registered,
    # so connections already made with it keep syncing and disconnecting
    # normally. This is a product decision (is this integration ready to put in
    # front of users?), not a capability one.
    offered: ClassVar[bool] = True

    def __init__(self, credentials: Optional[Dict[str, Any]] = None,
                 timeout: float = 20.0):
        self.credentials: Dict[str, Any] = dict(credentials or {})
        self.timeout = timeout
        self._sandbox: Optional[SandboxBackend] = None
        self._connected = False

    # -- mode ----------------------------------------------------------------

    @classmethod
    def is_live_configured(cls) -> bool:
        """True when every env var needed for the real API is present."""
        return bool(cls.required_env) and all(os.environ.get(v) for v in cls.required_env)

    @property
    def is_live(self) -> bool:
        """Whether this adapter instance talks to the real institution API."""
        return self.credentials.get("mode") == "live"

    @property
    def sandbox(self) -> SandboxBackend:
        if self._sandbox is None:
            self._sandbox = get_sandbox(self.institution)
        return self._sandbox

    # -- connection lifecycle -------------------------------------------------

    def connect(self, authorization_code: Optional[str] = None,
                redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        """Establish a connection and return credentials to store (encrypted).

        In live mode this exchanges the OAuth authorization code for tokens;
        in sandbox mode it mints simulated tokens so the rest of the pipeline
        behaves identically.
        """
        if self.is_live_configured() and authorization_code:
            self.credentials = self._exchange_code_live(authorization_code, redirect_uri)
            self.credentials["mode"] = "live"
        else:
            now = datetime.utcnow()
            self.credentials = {
                "mode": "sandbox",
                "access_token": f"sbx-{self.institution}-{secrets.token_hex(16)}",
                "refresh_token": f"sbx-refresh-{secrets.token_hex(16)}",
                "expires_at": (now + timedelta(hours=TOKEN_LIFETIME_HOURS)).isoformat(),
            }
        self._connected = True
        return self.credentials

    def disconnect(self) -> None:
        """Revoke tokens with the provider (best effort) and forget credentials."""
        if self.is_live:
            try:
                self._revoke_live()
            except Exception:
                pass  # local disconnect must succeed even if the provider is down
        self.credentials = {}
        self._connected = False

    def refresh_access_token(self) -> Dict[str, Any]:
        """Refresh the access token if expired; returns credentials to re-store."""
        if not self.credentials:
            raise TokenExpiredError(f"{self.display_name}: no stored credentials")
        expires_at = self.credentials.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            if expiry > datetime.utcnow() + timedelta(minutes=5):
                return self.credentials  # still valid
        if self.is_live:
            self.credentials = self._refresh_token_live()
            self.credentials["mode"] = "live"
        else:
            self.credentials["access_token"] = f"sbx-{self.institution}-{secrets.token_hex(16)}"
            self.credentials["expires_at"] = (
                datetime.utcnow() + timedelta(hours=TOKEN_LIFETIME_HOURS)
            ).isoformat()
        return self.credentials

    def validate_connection(self) -> bool:
        """Cheap check that stored credentials still work."""
        if not self.credentials.get("access_token"):
            return False
        if self.is_live:
            return self._validate_live()
        return True

    # -- canonical data access (public API used by the engine) ----------------

    def get_accounts(self) -> List[InstitutionAccount]:
        """All accounts at this institution, in canonical form."""
        return self._normalize_accounts(self._fetch_accounts_raw())

    def get_balances(self) -> List[Balance]:
        """Current balances for all accounts, in canonical form."""
        raw_accounts = self._fetch_accounts_raw()
        return self._normalize_balances(raw_accounts, self._fetch_balances_raw(raw_accounts))

    def get_transactions(self) -> List[Transaction]:
        """Recent cash-account transactions, in canonical form."""
        if not self.supports_transactions:
            return []
        raw_accounts = self._fetch_accounts_raw()
        return self._normalize_transactions(
            raw_accounts, self._fetch_transactions_raw(raw_accounts))

    def get_holdings(self) -> List[Holding]:
        """Investment/crypto positions, in canonical form."""
        if not self.supports_holdings:
            return []
        raw_accounts = self._fetch_accounts_raw()
        return self._normalize_holdings(raw_accounts, self._fetch_holdings_raw(raw_accounts))

    def get_positions(self) -> List[Holding]:
        """Alias for :meth:`get_holdings` (brokerage terminology)."""
        return self.get_holdings()

    # -- normalization / validation -------------------------------------------

    def normalize_data(self, raw: Dict[str, Any]) -> SyncPayload:
        """Translate a raw provider bundle into a canonical SyncPayload."""
        raw_accounts = raw.get("accounts")
        payload = SyncPayload(institution=self.institution)
        payload.accounts = self._normalize_accounts(raw_accounts)
        payload.balances = self._normalize_balances(raw_accounts, raw.get("balances"))
        if self.supports_holdings:
            payload.holdings = self._normalize_holdings(raw_accounts, raw.get("holdings"))
        if self.supports_transactions:
            payload.transactions = self._normalize_transactions(
                raw_accounts, raw.get("transactions"))
        return payload

    def validate_data(self, payload: SyncPayload) -> SyncPayload:
        """Validate a canonical payload; raises DataValidationError on bad data."""
        payload.validate()
        return payload

    # -- full sync (template method) -------------------------------------------

    def sync(self) -> SyncPayload:
        """Fetch everything, normalize, validate, and return canonical data."""
        raw_accounts = self._fetch_accounts_raw()
        raw = {
            "accounts": raw_accounts,
            "balances": self._fetch_balances_raw(raw_accounts),
            "holdings": self._fetch_holdings_raw(raw_accounts) if self.supports_holdings else None,
            "transactions": (self._fetch_transactions_raw(raw_accounts)
                             if self.supports_transactions else None),
        }
        return self.validate_data(self.normalize_data(raw))

    # -- provider-specific raw fetching (implemented per institution) ----------

    @abstractmethod
    def _fetch_accounts_raw(self) -> Any:
        """Fetch the provider's raw accounts response."""

    @abstractmethod
    def _fetch_balances_raw(self, raw_accounts: Any) -> Any:
        """Fetch raw balance data (may reuse the accounts response)."""

    @abstractmethod
    def _fetch_transactions_raw(self, raw_accounts: Any) -> Any:
        """Fetch raw transaction data for cash accounts."""

    @abstractmethod
    def _fetch_holdings_raw(self, raw_accounts: Any) -> Any:
        """Fetch raw position/holding data for investment accounts."""

    # -- provider-specific normalization (implemented per institution) ---------

    @abstractmethod
    def _normalize_accounts(self, raw: Any) -> List[InstitutionAccount]:
        """Translate raw accounts into canonical InstitutionAccounts."""

    @abstractmethod
    def _normalize_balances(self, raw_accounts: Any, raw: Any) -> List[Balance]:
        """Translate raw balances into canonical Balances."""

    @abstractmethod
    def _normalize_transactions(self, raw_accounts: Any, raw: Any) -> List[Transaction]:
        """Translate raw transactions into canonical Transactions."""

    @abstractmethod
    def _normalize_holdings(self, raw_accounts: Any, raw: Any) -> List[Holding]:
        """Translate raw positions into canonical Holdings."""

    # -- live-API hooks (override where a real API exists) ----------------------

    def authorization_url(self, redirect_uri: str, state: str) -> Optional[str]:
        """OAuth authorize URL for live mode; None when not applicable."""
        return None

    def _exchange_code_live(self, code: str, redirect_uri: Optional[str]) -> Dict[str, Any]:
        raise AuthenticationError(f"{self.display_name}: live OAuth exchange not configured")

    def _refresh_token_live(self) -> Dict[str, Any]:
        raise TokenExpiredError(f"{self.display_name}: live token refresh not configured")

    def _validate_live(self) -> bool:
        return True

    def _revoke_live(self) -> None:
        pass

    # -- shared HTTP helper -------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """HTTP call with provider-error mapping used by all live adapters."""
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"{self.display_name}: network failure — {exc}") from exc
        if response.status_code == 401:
            raise TokenExpiredError(f"{self.display_name}: access token rejected (401)")
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 1))
            raise RateLimitError(f"{self.display_name}: rate limited", retry_after=retry_after)
        if response.status_code >= 500:
            raise ProviderOutageError(
                f"{self.display_name}: provider error {response.status_code}")
        if response.status_code >= 400:
            raise AuthenticationError(
                f"{self.display_name}: request rejected ({response.status_code}) {response.text[:200]}")
        return response

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.get('access_token', '')}"}
