"""Plaid adapter — aggregator covering many real institutions through one
linked "Item" per connection (checking/savings/credit transactions plus
brokerage/crypto investment holdings).

Unlike the single-institution adapters, Plaid's auth handshake is not an
OAuth redirect: the frontend embeds Plaid Link (a JS widget) which returns a
``public_token`` that the backend exchanges server-side for an
``access_token`` + ``item_id``. That handshake is implemented here as
:meth:`create_link_token` / :meth:`connect_with_public_token` rather than the
base class's ``authorization_url`` / ``connect(authorization_code=...)``
pair — see ``finance_sync/service.py`` and ``finance_sync/routes.py`` for the
dedicated link-token/exchange endpoints that drive it.

Live mode requires ``PLAID_CLIENT_ID`` / ``PLAID_SECRET`` (plus optional
``PLAID_ENV``, default ``sandbox``); without them the deterministic local
sandbox backend is used, same as every other adapter.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests

from ..canonical import (
    AccountType,
    AssetClass,
    Balance,
    Holding,
    InstitutionAccount,
    Transaction,
)
from ..exceptions import (
    AuthenticationError,
    ConfigurationError,
    NetworkError,
    ProviderOutageError,
    RateLimitError,
    TokenExpiredError,
)
from . import register_adapter
from .base import FinancialInstitutionAdapter

# Plaid access tokens don't expire on a fixed schedule like OAuth2 — they stay
# valid until revoked or the item needs re-auth (ITEM_LOGIN_REQUIRED). A
# far-future expiry lets the base class's refresh-skip logic apply.
_FAR_FUTURE_EXPIRY = "9999-01-01T00:00:00"

_ACCOUNT_TYPE_MAP = {
    ("depository", "savings"): AccountType.SAVINGS,
    ("credit", None): AccountType.CREDIT,
    ("investment", None): AccountType.BROKERAGE,
}

_SECURITY_TYPE_MAP = {
    "equity": AssetClass.STOCK,
    "etf": AssetClass.ETF,
    "mutual fund": AssetClass.MUTUAL_FUND,
    "fixed income": AssetClass.BOND,
    "cryptocurrency": AssetClass.CRYPTO,
    "cash": AssetClass.CASH,
}


@register_adapter
class PlaidAdapter(FinancialInstitutionAdapter):
    """Syncs balances, transactions, and investment holdings via Plaid."""

    institution = "plaid"
    display_name = "Plaid"
    auth_type = "plaid_link"
    required_env = ("PLAID_CLIENT_ID", "PLAID_SECRET")
    supports_transactions = True
    supports_holdings = True
    accent_color = "#000000"

    # -- Plaid Link handshake (not the base OAuth authorization_url/connect) --

    def create_link_token(self, client_user_id: str) -> str:
        """Create a Link token for the frontend to open Plaid Link with."""
        payload = {
            "client_name": "Dough",
            "user": {"client_user_id": client_user_id},
            # Only *require* transactions — requiring a product hides every
            # institution that can't provide it (e.g. Capital One has no
            # investments support and would be blocked entirely).
            "products": ["transactions"],
            "required_if_supported_products": ["investments"],
            # Plaid defaults to ~90 days of history; ask for the maximum
            # (2 years) so the initial backfill covers everything available.
            "transactions": {"days_requested": 730},
            "country_codes": ["US"],
            "language": "en",
        }
        # OAuth institutions (Capital One, Chase, Schwab, …) require a
        # redirect URI that is also registered in the Plaid dashboard under
        # API → Allowed redirect URIs; without it Link fails with an
        # "internal error" as soon as such an institution is selected.
        redirect_uri = self._env_setting("PLAID_REDIRECT_URI")
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        data = self._plaid_call("link/token/create", payload)
        return data["link_token"]

    def connect_with_public_token(self, public_token: str) -> Dict[str, Any]:
        """Exchange a Link `public_token` for a stored access token + item id."""
        data = self._plaid_call("item/public_token/exchange", {"public_token": public_token})
        self.credentials = {
            "mode": "live",
            "access_token": data["access_token"],
            "item_id": data["item_id"],
            "cursor": None,
            "expires_at": _FAR_FUTURE_EXPIRY,
        }
        self._connected = True
        return self.credentials

    # -- raw fetching ---------------------------------------------------------

    def _fetch_accounts_raw(self) -> Dict[str, Any]:
        if not self.is_live:
            return self.sandbox.get_accounts()
        return self._plaid_call("accounts/get", {"access_token": self.credentials["access_token"]})

    def _fetch_balances_raw(self, raw_accounts: Any) -> Any:
        return raw_accounts  # Plaid returns balances inline on each account

    def _fetch_transactions_raw(self, raw_accounts: Any) -> Dict[str, Any]:
        cursor = self.credentials.get("cursor")
        # transactions/sync is paginated (up to `count` transactions per page,
        # `has_more` signals another page at `next_cursor`) — drain every page
        # so a fresh connection's full backlog lands in one sync.
        merged: Dict[str, Any] = {"added": [], "modified": [], "removed": []}
        while True:
            if not self.is_live:
                data = self.sandbox.sync_transactions(cursor)
            else:
                data = self._plaid_call("transactions/sync", {
                    "access_token": self.credentials["access_token"],
                    "cursor": cursor or "",
                    "count": 500,
                })
            for key in merged:
                merged[key].extend(data.get(key) or [])
            cursor = data.get("next_cursor", cursor)
            if not data.get("has_more"):
                break
        # Advances the stored cursor so the *next* sync is incremental; the
        # engine persists this back to the encrypted connection after sync().
        self.credentials["cursor"] = cursor
        merged["next_cursor"] = cursor
        return merged

    # Link only *requests* investments where supported
    # (required_if_supported_products), so items at banks without the product
    # (e.g. Capital One) reject investments/holdings/get outright. Treat those
    # rejections — and "data not pulled yet" — as "no holdings" rather than
    # failing the whole sync.
    _NO_HOLDINGS_CODES = ("PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_READY",
                          "NO_INVESTMENT_ACCOUNTS", "NO_INVESTMENT_AUTH_ACCOUNTS")

    def _fetch_holdings_raw(self, raw_accounts: Any) -> Dict[str, Any]:
        if not self.is_live:
            return self.sandbox.get_holdings()
        try:
            return self._plaid_call("investments/holdings/get",
                                    {"access_token": self.credentials["access_token"]})
        except AuthenticationError as exc:
            if getattr(exc, "plaid_error_code", None) in self._NO_HOLDINGS_CODES:
                return {"holdings": [], "securities": []}
            raise

    # -- normalization ---------------------------------------------------------

    def _normalize_accounts(self, raw: Any) -> List[InstitutionAccount]:
        accounts = []
        for acct in (raw or {}).get("accounts", []):
            balances = acct.get("balances") or {}
            accounts.append(InstitutionAccount(
                external_id=acct["account_id"],
                name=acct.get("name") or "Account",
                account_type=self._map_account_type(acct.get("type"), acct.get("subtype")),
                currency=balances.get("iso_currency_code") or "USD",
                mask=acct.get("mask"),
            ))
        return accounts

    @staticmethod
    def _map_account_type(type_: Optional[str], subtype: Optional[str]) -> AccountType:
        if type_ == "depository":
            return AccountType.SAVINGS if subtype == "savings" else AccountType.CHECKING
        return _ACCOUNT_TYPE_MAP.get((type_, None), AccountType.OTHER)

    def _normalize_balances(self, raw_accounts: Any, raw: Any) -> List[Balance]:
        balances = []
        for acct in (raw or {}).get("accounts", []):
            bal = acct.get("balances") or {}
            current = bal.get("current")
            if current is None:
                continue  # investment accounts report value via holdings, not a cash balance
            balances.append(Balance(
                account_external_id=acct["account_id"],
                current=float(current),
                available=float(bal["available"]) if bal.get("available") is not None else None,
                currency=bal.get("iso_currency_code") or "USD",
                as_of=datetime.utcnow(),
            ))
        return balances

    def _normalize_transactions(self, raw_accounts: Any, raw: Any) -> List[Transaction]:
        transactions = []
        for txn in (raw or {}).get("added", []) + (raw or {}).get("modified", []):
            transactions.append(Transaction(
                account_external_id=txn["account_id"],
                external_id=txn["transaction_id"],
                date=date.fromisoformat(txn["date"]),
                description=txn.get("name") or "Transaction",
                # Plaid convention: positive amount = money out; canonical is the reverse.
                amount=round(-float(txn["amount"]), 2),
                currency=txn.get("iso_currency_code") or "USD",
                pending=bool(txn.get("pending", False)),
            ))
        return transactions

    def _normalize_holdings(self, raw_accounts: Any, raw: Any) -> List[Holding]:
        securities = {s["security_id"]: s for s in (raw or {}).get("securities", [])}
        holdings = []
        for h in (raw or {}).get("holdings", []):
            sec = securities.get(h["security_id"], {})
            symbol = sec.get("ticker_symbol") or h["security_id"]
            price = float(h.get("institution_price") or 0.0)
            quantity = float(h.get("quantity") or 0.0)
            market_value = h.get("institution_value")
            market_value = round(float(market_value), 2) if market_value is not None else round(quantity * price, 2)
            cost_basis = h.get("cost_basis")
            holdings.append(Holding(
                account_external_id=h["account_id"],
                symbol=symbol,
                name=sec.get("name") or symbol,
                quantity=quantity,
                current_price=price,
                market_value=market_value,
                asset_class=_SECURITY_TYPE_MAP.get(sec.get("type"), AssetClass.OTHER),
                avg_cost=(float(cost_basis) / quantity) if cost_basis and quantity else None,
                external_id=f"{h['account_id']}:{h['security_id']}",
            ))
        return holdings

    # -- live token lifecycle -----------------------------------------------

    def _refresh_token_live(self) -> Dict[str, Any]:
        return dict(self.credentials)  # Plaid access tokens don't rotate on a schedule

    def _validate_live(self) -> bool:
        try:
            self._plaid_call("accounts/get", {"access_token": self.credentials["access_token"]})
            return True
        except Exception:
            return False

    def _revoke_live(self) -> None:
        try:
            self._plaid_call("item/remove", {"access_token": self.credentials["access_token"]})
        except Exception:
            pass  # best-effort; local disconnect must still succeed

    # -- Plaid transport --------------------------------------------------------

    # Plaid retired the standalone "development" host in 2023 — it's now just
    # a request-limit tier inside production, not a separate hostname.
    _VALID_ENVS = ("sandbox", "production")

    @staticmethod
    def _env_setting(name: str) -> str:
        """Resolve a setting for the active PLAID_ENV.

        ``PLAID_SECRET_SANDBOX`` / ``PLAID_SECRET_PRODUCTION`` (etc.) take
        precedence over the un-suffixed variable, so both environments can
        live in .env side by side and ``PLAID_ENV`` alone picks between them.
        """
        env = os.environ.get("PLAID_ENV", "sandbox")
        return os.environ.get(f"{name}_{env.upper()}") or os.environ.get(name, "")

    @classmethod
    def is_live_configured(cls) -> bool:
        return bool(os.environ.get("PLAID_CLIENT_ID")) and bool(cls._env_setting("PLAID_SECRET"))

    @staticmethod
    def _api_base() -> str:
        env = os.environ.get("PLAID_ENV", "sandbox")
        if env not in PlaidAdapter._VALID_ENVS:
            raise ConfigurationError(
                f"Plaid: PLAID_ENV={env!r} is not valid — Plaid only serves "
                f"{PlaidAdapter._VALID_ENVS!r}. If you're trying to test against a "
                "real institution, use 'production' (Plaid's free Development-tier "
                "request limits apply automatically there).")
        return f"https://{env}.plaid.com"

    def _plaid_call(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to Plaid with client credentials; maps Plaid's body-level error
        codes (not just HTTP status) onto the shared sync error hierarchy."""
        body = {
            "client_id": os.environ.get("PLAID_CLIENT_ID", ""),
            "secret": self._env_setting("PLAID_SECRET"),
            **payload,
        }
        try:
            resp = requests.post(f"{self._api_base()}/{path}", json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Plaid: network failure — {exc}") from exc
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 500:
            raise ProviderOutageError(f"Plaid: provider error {resp.status_code}")
        error_code = data.get("error_code")
        if error_code == "ITEM_LOGIN_REQUIRED":
            raise TokenExpiredError(f"Plaid: item requires re-authentication ({error_code})")
        if error_code == "RATE_LIMIT_EXCEEDED":
            raise RateLimitError("Plaid: rate limited")
        if resp.status_code >= 400:
            exc = AuthenticationError(
                f"Plaid: request rejected ({resp.status_code}) "
                f"{data.get('error_message', resp.text[:200])}")
            exc.plaid_error_code = error_code
            raise exc
        return data
