"""Coinbase adapter — crypto wallets as holdings, USD wallet as cash."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

from ..canonical import (
    AccountType,
    AssetClass,
    Balance,
    Holding,
    InstitutionAccount,
    Transaction,
)
from . import register_adapter
from .base import FinancialInstitutionAdapter

_API_BASE = "https://api.coinbase.com"


@register_adapter
class CoinbaseAdapter(FinancialInstitutionAdapter):
    """Syncs Coinbase wallets into crypto holdings and a USD cash balance.

    Live mode uses Coinbase's OAuth2 v2 API and requires
    ``COINBASE_CLIENT_ID`` / ``COINBASE_CLIENT_SECRET``; without them the
    deterministic sandbox backend is used.
    """

    institution = "coinbase"
    display_name = "Coinbase"
    auth_type = "oauth2"
    required_env = ("COINBASE_CLIENT_ID", "COINBASE_CLIENT_SECRET")
    supports_transactions = False  # buys/sells are portfolio activity, not spending
    supports_holdings = True
    accent_color = "#0052ff"
    offered = False  # withdrawn from the connect catalog for now; see base class

    # -- raw fetching ---------------------------------------------------------

    def _fetch_accounts_raw(self) -> Dict[str, Any]:
        if not self.is_live:
            return self.sandbox.get_accounts()
        resp = self._request("GET", f"{_API_BASE}/v2/accounts", headers=self._auth_headers())
        return resp.json()

    def _fetch_balances_raw(self, raw_accounts: Any) -> Any:
        # Coinbase returns native (USD) balances inline on the accounts resource.
        return raw_accounts

    def _fetch_transactions_raw(self, raw_accounts: Any) -> Any:
        return None

    def _fetch_holdings_raw(self, raw_accounts: Any) -> Any:
        return raw_accounts  # wallets *are* the positions

    # -- normalization ---------------------------------------------------------

    @staticmethod
    def _wallets(raw: Any) -> List[Dict[str, Any]]:
        return (raw or {}).get("data", [])

    def _normalize_accounts(self, raw: Any) -> List[InstitutionAccount]:
        accounts = []
        for wallet in self._wallets(raw):
            code = wallet["currency"]["code"]
            accounts.append(InstitutionAccount(
                external_id=str(wallet["id"]),
                name=wallet.get("name") or f"{code} Wallet",
                account_type=AccountType.SAVINGS if code == "USD" else AccountType.CRYPTO,
                currency="USD",
            ))
        return accounts

    def _normalize_balances(self, raw_accounts: Any, raw: Any) -> List[Balance]:
        balances = []
        for wallet in self._wallets(raw):
            usd_value = float(wallet["native_balance"]["amount"])
            balances.append(Balance(
                account_external_id=str(wallet["id"]),
                current=round(usd_value, 2),
                available=round(usd_value, 2),
                currency="USD",
                as_of=datetime.utcnow(),
            ))
        return balances

    def _normalize_holdings(self, raw_accounts: Any, raw: Any) -> List[Holding]:
        holdings = []
        for wallet in self._wallets(raw):
            code = wallet["currency"]["code"]
            if code == "USD":
                continue  # USD wallet is cash, not a position
            quantity = float(wallet["balance"]["amount"])
            usd_value = float(wallet["native_balance"]["amount"])
            if quantity <= 0:
                continue
            holdings.append(Holding(
                account_external_id=str(wallet["id"]),
                symbol=code,
                name=wallet["currency"].get("name", code),
                quantity=quantity,
                current_price=round(usd_value / quantity, 2),
                market_value=round(usd_value, 2),
                asset_class=AssetClass.CRYPTO,
                external_id=str(wallet["id"]),
            ))
        return holdings

    def _normalize_transactions(self, raw_accounts: Any, raw: Any) -> List[Transaction]:
        return []

    # -- live OAuth2 -------------------------------------------------------------

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        client_id = os.environ.get("COINBASE_CLIENT_ID", "")
        return ("https://www.coinbase.com/oauth/authorize?response_type=code"
                f"&client_id={client_id}&redirect_uri={redirect_uri}"
                f"&state={state}&scope=wallet:accounts:read")

    def _exchange_code_live(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        resp = self._request("POST", f"{_API_BASE}/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": os.environ["COINBASE_CLIENT_ID"],
            "client_secret": os.environ["COINBASE_CLIENT_SECRET"],
        })
        return self._token_response_to_credentials(resp.json())

    def _refresh_token_live(self) -> Dict[str, Any]:
        resp = self._request("POST", f"{_API_BASE}/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": self.credentials.get("refresh_token", ""),
            "client_id": os.environ["COINBASE_CLIENT_ID"],
            "client_secret": os.environ["COINBASE_CLIENT_SECRET"],
        })
        return self._token_response_to_credentials(resp.json())

    def _validate_live(self) -> bool:
        try:
            self._request("GET", f"{_API_BASE}/v2/user", headers=self._auth_headers())
            return True
        except Exception:
            return False

    @staticmethod
    def _token_response_to_credentials(token: Dict[str, Any]) -> Dict[str, Any]:
        expires_in = int(token.get("expires_in", 7200))
        return {
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token", ""),
            "expires_at": (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
        }
