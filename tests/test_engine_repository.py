"""SyncEngine orchestration, retries, dedupe, and net-worth persistence."""

from datetime import date

import pytest

from finance_sync.adapters import ADAPTER_REGISTRY, register_adapter
from finance_sync.adapters.base import FinancialInstitutionAdapter
from finance_sync.canonical import (
    AccountType,
    AssetClass,
    Balance,
    Holding as CanonicalHolding,
    InstitutionAccount,
    Transaction as CanonicalTransaction,
)
from finance_sync.engine import SyncEngine
from finance_sync.exceptions import NetworkError
from finance_sync.service import ConnectionService
from models import (
    AccountBalance,
    FinancialAccount,
    Holding,
    PortfolioSnapshotRow,
    SyncErrorLog,
    SyncRun,
    Transaction,
    db,
)


def _engine():
    return SyncEngine(backoff_base_seconds=0)


def _connect(institution):
    return ConnectionService().connect(institution)


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------

def test_sync_all_persists_everything(app):
    for institution in ("coinbase", "plaid"):
        _connect(institution)
    result = _engine().sync_all(trigger="manual")
    assert result.status == "success"
    assert len(result.results) == 2

    # Accounts, balances, holdings, transactions all landed
    assert FinancialAccount.query.count() >= 6
    assert Holding.query.filter_by(source="sync").count() > 0
    assert Transaction.query.filter_by(source="sync").count() > 0
    # Sync history recorded per connection
    assert SyncRun.query.filter_by(status="success").count() == 2
    # Daily snapshot written
    assert PortfolioSnapshotRow.query.count() == 1
    # Legacy AccountBalance mirrors synced cash so old pages keep working
    checking = AccountBalance.query.filter_by(account_type="checking").first()
    assert checking is not None and checking.starting_balance > 0


def test_second_sync_creates_no_duplicates(app):
    for institution in ("coinbase", "plaid"):
        _connect(institution)
    engine = _engine()
    engine.sync_all()
    txn_count = Transaction.query.count()
    holding_count = Holding.query.count()
    account_count = FinancialAccount.query.count()

    second = engine.sync_all()
    assert second.status == "success"
    assert Transaction.query.count() == txn_count, "duplicate transactions imported"
    assert Holding.query.count() == holding_count, "duplicate holdings created"
    assert FinancialAccount.query.count() == account_count, "duplicate accounts created"
    # The engine reported the duplicates as skipped, not added
    last_run = SyncRun.query.filter_by(institution="plaid").order_by(
        SyncRun.id.desc()).first()
    assert last_run.transactions_added == 0
    assert last_run.transactions_skipped > 0


def test_synced_transactions_are_categorized_by_rules(app):
    """A sync must categorise through the household's rules.

    [Phase 11A.1] The rule is now created by the test. It used to rely on
    whatever happened to be in `category_rules.json` — a file at the repo root
    shared by the whole installation *and* by the test suite, so this passed
    because the developer's personal rules were visible from here. That is the
    pollution SEC-0025 describes, and a test that depends on it is asserting
    something about the developer's finances rather than about the sync.

    `ACME PAYROLL DIRECT DEP` is emitted deterministically by the sandbox for
    every checking account, so the rule below is guaranteed a match.
    """
    from dough.services import rules_service

    rules_service.add_rule("Income", "ACME PAYROLL")

    _connect("plaid")
    _engine().sync_all()

    categorized = Transaction.query.filter(Transaction.source == "sync",
                                           Transaction.category != "Uncategorized").count()
    assert categorized > 0, "category rules did not run on synced transactions"
    assert Transaction.query.filter(
        Transaction.description.like("ACME PAYROLL%"),
        Transaction.category == "Income").count() > 0, (
        "the household's own rule did not apply to synced transactions")


def test_net_worth_uses_synced_balances(app):
    from finance_sync.repository import SyncRepository
    # Manual baseline
    db.session.add(AccountBalance(account_type="checking", starting_balance=111.0))
    db.session.commit()
    totals = SyncRepository.compute_totals()
    assert totals["checking"] == 111.0

    _connect("plaid")
    _engine().sync_all()
    totals = SyncRepository.compute_totals()
    synced_checking = (db.session.query(FinancialAccount)
                       .filter_by(account_type="checking").first())
    assert totals["checking"] == float(synced_checking.balance)
    assert totals["net_worth"] == pytest.approx(
        totals["cash"] + totals["investments"], abs=0.01)


def test_brokerage_account_value_not_double_counted_as_cash(app):
    """Plaid reports an investment account's balance as its *total* value;
    only Cash-class holdings (sweep/money market) may count toward cash."""
    from finance_sync.repository import SyncRepository
    db.session.add(FinancialAccount(
        connection_id=1, external_id="brok-1", name="Brokerage",
        account_type=AccountType.BROKERAGE.value, balance=10_500, is_active=True))
    db.session.add(Holding(ticker="VTI", name="Total Market", shares=50,
                           current_value=10_000, asset_class="Stock"))
    db.session.add(Holding(ticker="CUR:USD", name="US Dollar", shares=500,
                           current_value=500, asset_class="Cash"))
    db.session.commit()

    totals = SyncRepository.compute_totals()
    assert totals["brokerage_cash"] == 500.0
    assert totals["cash"] == 500.0          # no checking/savings synced or manual
    assert totals["investments"] == 10_000.0
    assert totals["net_worth"] == 10_500.0  # account value counted exactly once


def test_crypto_split_out_in_totals(app):
    from finance_sync.repository import SyncRepository
    _connect("coinbase")
    _engine().sync_all()
    totals = SyncRepository.compute_totals()
    assert totals["crypto"] > 0
    assert totals["investments"] >= totals["crypto"]


def test_manual_holdings_survive_sync(app):
    db.session.add(Holding(ticker="MANUAL", name="Manually tracked", shares=1,
                           current_value=500, asset_class="Stock"))
    db.session.commit()
    _connect("plaid")
    _engine().sync_all()
    manual = Holding.query.filter_by(ticker="MANUAL").first()
    assert manual is not None and manual.source == "manual"


def test_disconnect_removes_synced_data_keeps_transactions(app):
    connection = _connect("plaid")
    _engine().sync_all()
    txn_count = Transaction.query.count()
    assert txn_count > 0
    ConnectionService().disconnect(connection.id)
    assert FinancialAccount.query.count() == 0
    assert Transaction.query.count() == txn_count  # history preserved


# ---------------------------------------------------------------------------
# Failure handling / retries
# ---------------------------------------------------------------------------

class _FlakyAdapter(FinancialInstitutionAdapter):
    """Fails with a transient error a configurable number of times."""

    institution = "flaky_test"
    display_name = "Flaky Test Bank"
    supports_transactions = True
    supports_holdings = False
    failures_remaining = 0  # class-level so the engine's fresh instances share it

    def _fetch_accounts_raw(self):
        if type(self).failures_remaining > 0:
            type(self).failures_remaining -= 1
            raise NetworkError("simulated network failure")
        return {"accounts": [{"id": "flaky-1", "name": "Flaky Checking",
                              "balance": 100.0}]}

    def _fetch_balances_raw(self, raw_accounts):
        return raw_accounts

    def _fetch_transactions_raw(self, raw_accounts):
        return None

    def _fetch_holdings_raw(self, raw_accounts):
        return None

    def _normalize_accounts(self, raw):
        return [InstitutionAccount(external_id=a["id"], name=a["name"],
                                   account_type=AccountType.CHECKING)
                for a in (raw or {}).get("accounts", [])]

    def _normalize_balances(self, raw_accounts, raw):
        return [Balance(account_external_id=a["id"], current=a["balance"])
                for a in (raw or {}).get("accounts", [])]

    def _normalize_transactions(self, raw_accounts, raw):
        return []

    def _normalize_holdings(self, raw_accounts, raw):
        return []


@pytest.fixture()
def flaky_adapter():
    register_adapter(_FlakyAdapter)
    yield _FlakyAdapter
    ADAPTER_REGISTRY.pop("flaky_test", None)


def test_transient_failure_is_retried_and_succeeds(app, flaky_adapter):
    flaky_adapter.failures_remaining = 2  # fails twice, succeeds on 3rd attempt
    _connect("flaky_test")
    result = _engine().sync_all()
    assert result.status == "success"
    # Both failed attempts were logged
    assert SyncErrorLog.query.filter_by(error_type="network").count() == 2


def test_persistent_failure_marks_connection_error(app, flaky_adapter):
    flaky_adapter.failures_remaining = 99
    connection = _connect("flaky_test")
    result = _engine().sync_all()
    assert result.status == "error"
    db.session.refresh(connection)
    assert connection.status == "error"
    assert connection.last_error
    run = SyncRun.query.order_by(SyncRun.id.desc()).first()
    assert run.status == "error"
    assert run.error_message


def test_one_bad_institution_does_not_block_others(app, flaky_adapter):
    flaky_adapter.failures_remaining = 99
    _connect("flaky_test")
    _connect("coinbase")
    result = _engine().sync_all()
    assert result.status == "partial"
    by_institution = {r.institution: r.status for r in result.results}
    assert by_institution["coinbase"] == "success"
    assert by_institution["flaky_test"] == "error"


def test_tokens_are_stored_encrypted(app):
    connection = _connect("coinbase")
    assert "access_token" not in (connection.auth_blob or "")
    assert "sbx-" not in (connection.auth_blob or "")
    from finance_sync.crypto import TokenCipher
    decrypted = TokenCipher().decrypt(connection.auth_blob)
    assert decrypted["access_token"].startswith("sbx-coinbase-")
