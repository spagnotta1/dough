"""Adapter registry and per-institution adapter pipelines (sandbox mode)."""

import pytest

from finance_sync.adapters import (
    ADAPTER_REGISTRY,
    available_institutions,
    get_adapter_class,
    register_adapter,
)
from finance_sync.adapters.base import FinancialInstitutionAdapter
from finance_sync.canonical import AccountType, AssetClass
from finance_sync.exceptions import UnsupportedInstitutionError

EXPECTED_INSTITUTIONS = {"coinbase", "plaid"}


def test_all_expected_adapters_registered():
    assert EXPECTED_INSTITUTIONS.issubset(ADAPTER_REGISTRY.keys())


def test_unknown_institution_raises():
    with pytest.raises(UnsupportedInstitutionError):
        get_adapter_class("bank_of_narnia")


def test_unoffered_adapter_is_hidden_but_still_resolvable():
    """Withdrawing an institution hides the card; it must not break sync.

    Existing connections are looked up by slug through get_adapter_class, so an
    adapter that leaves the catalog has to stay in the registry or every stored
    connection using it would start failing.
    """
    assert "coinbase" not in {c.institution for c in available_institutions()}
    assert get_adapter_class("coinbase").institution == "coinbase"


@pytest.mark.parametrize("institution", sorted(EXPECTED_INSTITUTIONS))
def test_connect_and_full_sync(institution):
    """Every adapter must complete the full pipeline and produce valid data."""
    adapter = get_adapter_class(institution)()
    credentials = adapter.connect()
    assert credentials["access_token"]
    assert credentials["mode"] == "sandbox"
    assert adapter.validate_connection()

    payload = adapter.sync()  # fetch + normalize + validate
    assert payload.institution == institution
    assert payload.accounts, f"{institution} produced no accounts"
    assert payload.balances, f"{institution} produced no balances"
    # Canonical types only — no raw provider data leaks out.
    for acct in payload.accounts:
        assert isinstance(acct.account_type, AccountType)
    for holding in payload.holdings:
        assert isinstance(holding.asset_class, AssetClass)
        assert holding.market_value >= 0


def test_get_positions_is_alias_for_get_holdings():
    adapter = get_adapter_class("plaid")()
    adapter.connect()
    holdings = adapter.get_holdings()
    positions = adapter.get_positions()
    assert [h.symbol for h in holdings] == [p.symbol for p in positions]
    assert len(holdings) > 0


def test_coinbase_provides_crypto_holdings():
    adapter = get_adapter_class("coinbase")()
    adapter.connect()
    payload = adapter.sync()
    symbols = {h.symbol for h in payload.holdings}
    assert "BTC" in symbols
    assert all(h.asset_class == AssetClass.CRYPTO for h in payload.holdings)
    # USD wallet is cash, never a position
    assert "USD" not in symbols


def test_token_refresh_renews_expired_token():
    from datetime import datetime, timedelta
    adapter = get_adapter_class("coinbase")()
    adapter.connect()
    adapter.credentials["expires_at"] = (
        datetime.utcnow() - timedelta(hours=1)).isoformat()
    old_token = adapter.credentials["access_token"]
    refreshed = adapter.refresh_access_token()
    assert refreshed["access_token"] != old_token


def test_disconnect_clears_credentials():
    adapter = get_adapter_class("coinbase")()
    adapter.connect()
    adapter.disconnect()
    assert not adapter.credentials
    assert not adapter.validate_connection()


def test_new_institution_requires_only_one_adapter_class():
    """Extensibility: registering one subclass makes it fully discoverable."""

    @register_adapter
    class SchwabAdapter(FinancialInstitutionAdapter):
        institution = "schwab_test"
        display_name = "Schwab (test)"
        supports_transactions = False
        supports_holdings = False

        def _fetch_accounts_raw(self):
            return {"accounts": []}

        def _fetch_balances_raw(self, raw_accounts):
            return None

        def _fetch_transactions_raw(self, raw_accounts):
            return None

        def _fetch_holdings_raw(self, raw_accounts):
            return None

        def _normalize_accounts(self, raw):
            return []

        def _normalize_balances(self, raw_accounts, raw):
            return []

        def _normalize_transactions(self, raw_accounts, raw):
            return []

        def _normalize_holdings(self, raw_accounts, raw):
            return []

    try:
        assert get_adapter_class("schwab_test") is SchwabAdapter
        assert any(c.institution == "schwab_test" for c in available_institutions())
        payload = SchwabAdapter().sync()
        assert payload.institution == "schwab_test"
    finally:
        ADAPTER_REGISTRY.pop("schwab_test", None)
