"""Persistence of canonical sync payloads into SQLite.

The repository is the only component that writes provider-derived data to the
database. It consumes canonical models exclusively — it has no idea which
institution produced them — and guarantees idempotency: syncing the same
payload twice never creates duplicate accounts, holdings, or transactions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    AccountBalance,
    FinancialAccount,
    Holding as HoldingRow,
    InstitutionConnection,
    MarketPrice,
    PortfolioSnapshotRow,
    Transaction as TransactionRow,
    db,
)

from .canonical import AccountType, SyncPayload


@dataclass
class SaveResult:
    """Counts of what a payload write actually changed."""

    accounts_synced: int = 0
    balances_updated: int = 0
    holdings_synced: int = 0
    transactions_added: int = 0
    transactions_skipped: int = 0
    details: Dict[str, int] = field(default_factory=dict)


class SyncRepository:
    """Writes canonical payloads to SQLite; institution-agnostic by design."""

    def __init__(self, categorize: Optional[Callable[[str], str]] = None):
        if categorize is None:
            # Wire in the household's rules engine so synced transactions are
            # categorized exactly like CSV imports. Resolved per call rather
            # than held, for two reasons that now both matter:
            #
            #   1. A rule edited in the Rules page applies to the next sync
            #      without a restart -- the original reason, still true.
            #   2. [Phase 11A.1] The rules are per household. Capturing an
            #      engine at construction would pin this repository to whichever
            #      household happened to be in scope when it was built, and a
            #      sync would then categorize one family's transactions with
            #      another family's rules.
            def categorize(description):
                from dough.services.rules_service import as_engine
                return as_engine().get_category(description)
        #: callback mapping a transaction description to a category
        self._categorize = categorize

    # -- public API -----------------------------------------------------------

    def save_payload(self, connection: InstitutionConnection,
                     payload: SyncPayload) -> SaveResult:
        """Persist one adapter's canonical payload. Commits on success."""
        result = SaveResult()
        account_rows = self._upsert_accounts(connection, payload, result)
        self._apply_balances(payload, account_rows, result)
        self._sync_holdings(connection, payload, account_rows, result)
        self._import_transactions(payload, account_rows, result)
        self._update_market_prices(connection, payload)
        db.session.commit()
        # These read committed data across *all* connections:
        self.refresh_cash_account_totals()
        self.write_daily_snapshot()
        return result

    def refresh_cash_account_totals(self) -> None:
        """Mirror synced checking/savings totals into the legacy AccountBalance
        rows so the existing log page and net-worth code stay correct."""
        for account_type in ("checking", "savings"):
            total = (db.session.query(func.sum(FinancialAccount.balance))
                     .filter(FinancialAccount.account_type == account_type,
                             FinancialAccount.is_active.is_(True))
                     .scalar())
            if total is None:
                continue  # nothing synced for this type; leave manual value alone
            row = AccountBalance.query.filter_by(account_type=account_type).first()
            if row is None:
                row = AccountBalance(account_type=account_type, starting_balance=0)
                db.session.add(row)
            row.starting_balance = float(total)
        db.session.commit()

    def write_daily_snapshot(self) -> PortfolioSnapshotRow:
        """Upsert today's portfolio snapshot from current DB state."""
        totals = self.compute_totals()
        today = date.today()
        row = PortfolioSnapshotRow.query.filter_by(snapshot_date=today).first()
        if row is None:
            row = PortfolioSnapshotRow(snapshot_date=today)
            db.session.add(row)
        row.checking = totals["checking"]
        row.savings = totals["savings"]
        row.total_cash = totals["cash"]
        row.brokerage = totals["brokerage"]
        row.crypto = totals["crypto"]
        row.total_investments = totals["investments"]
        row.net_worth = totals["net_worth"]
        db.session.commit()
        return row

    @staticmethod
    def compute_totals() -> Dict[str, float]:
        """Current net-worth breakdown from synced accounts + holdings.

        Falls back to the manual AccountBalance rows for cash when an account
        type has never been synced, preserving pre-sync behavior.
        """
        def synced_cash(account_type: str) -> Optional[float]:
            total = (db.session.query(func.sum(FinancialAccount.balance))
                     .filter(FinancialAccount.account_type == account_type,
                             FinancialAccount.is_active.is_(True))
                     .scalar())
            return float(total) if total is not None else None

        def manual_cash(account_type: str) -> float:
            row = AccountBalance.query.filter_by(account_type=account_type).first()
            return float(row.starting_balance) if row else 0.0

        checking = synced_cash("checking")
        savings = synced_cash("savings")
        checking = checking if checking is not None else manual_cash("checking")
        savings = savings if savings is not None else manual_cash("savings")

        # Brokerage sweep / settlement cash counts toward cash as well. It is
        # reported as Cash-class holdings (money market funds, CUR:USD); the
        # account-level balance Plaid returns for an investment account is the
        # account's *total* value, so summing it here would double count the
        # holdings.
        brokerage_cash = float(db.session.query(func.sum(HoldingRow.current_value))
                               .filter(HoldingRow.asset_class == "Cash")
                               .scalar() or 0.0)

        crypto = float(db.session.query(func.sum(HoldingRow.current_value))
                       .filter(HoldingRow.asset_class == "Crypto").scalar() or 0.0)
        invested = float(db.session.query(func.sum(HoldingRow.current_value))
                         .filter(HoldingRow.asset_class.notin_(("Crypto", "Cash")))
                         .scalar() or 0.0)

        cash = round(checking + savings + brokerage_cash, 2)
        investments = round(invested + crypto, 2)
        return {
            "checking": round(checking, 2),
            "savings": round(savings, 2),
            "brokerage_cash": round(brokerage_cash, 2),
            "cash": cash,
            "brokerage": round(invested, 2),
            "crypto": round(crypto, 2),
            "investments": investments,
            "net_worth": round(cash + investments, 2),
        }

    # -- internals --------------------------------------------------------------

    def _upsert_accounts(self, connection: InstitutionConnection,
                         payload: SyncPayload,
                         result: SaveResult) -> Dict[str, FinancialAccount]:
        """Create/update FinancialAccount rows; returns external_id → row map."""
        now = datetime.utcnow()
        rows: Dict[str, FinancialAccount] = {}
        existing = {a.external_id: a for a in FinancialAccount.query
                    .filter_by(connection_id=connection.id).all()}
        seen = set()
        for acct in payload.accounts:
            row = existing.get(acct.external_id)
            if row is None:
                row = FinancialAccount(
                    connection_id=connection.id,
                    external_id=acct.external_id,
                )
                db.session.add(row)
            row.name = acct.name
            row.account_type = acct.account_type.value
            row.currency = acct.currency
            row.mask = acct.mask
            row.is_active = True
            row.last_synced_at = now
            rows[acct.external_id] = row
            seen.add(acct.external_id)
            result.accounts_synced += 1
        # Accounts that disappeared at the provider become inactive (never deleted).
        for external_id, row in existing.items():
            if external_id not in seen:
                row.is_active = False
        db.session.flush()
        return rows

    def _apply_balances(self, payload: SyncPayload,
                        account_rows: Dict[str, FinancialAccount],
                        result: SaveResult) -> None:
        for balance in payload.balances:
            row = account_rows.get(balance.account_external_id)
            if row is None:
                continue
            row.balance = balance.current
            row.available_balance = balance.available
            result.balances_updated += 1

    def _sync_holdings(self, connection: InstitutionConnection,
                       payload: SyncPayload,
                       account_rows: Dict[str, FinancialAccount],
                       result: SaveResult) -> None:
        """Upsert synced holdings per account; remove positions the provider
        no longer reports. Manual holdings (account_id NULL) are untouched."""
        now = datetime.utcnow()
        account_ids = [row.id for row in account_rows.values()]
        existing = {}
        if account_ids:
            for h in HoldingRow.query.filter(HoldingRow.account_id.in_(account_ids)).all():
                existing[(h.account_id, h.ticker)] = h
        seen = set()
        for holding in payload.holdings:
            account = account_rows.get(holding.account_external_id)
            if account is None:
                continue
            key = (account.id, holding.symbol.upper())
            row = existing.get(key)
            if row is None:
                row = HoldingRow(account_id=account.id, ticker=holding.symbol.upper())
                db.session.add(row)
            row.name = holding.name
            row.shares = holding.quantity
            row.current_value = holding.market_value
            row.asset_class = holding.asset_class.value
            row.account_name = f"{connection.display_name} · {account.name}"
            row.source = "sync"
            row.external_id = holding.external_id
            row.avg_cost = holding.avg_cost
            row.current_price = holding.current_price
            row.last_synced_at = now
            seen.add(key)
            result.holdings_synced += 1
        for key, row in existing.items():
            if key not in seen:
                db.session.delete(row)  # position closed at the provider
        db.session.flush()

    def _import_transactions(self, payload: SyncPayload,
                             account_rows: Dict[str, FinancialAccount],
                             result: SaveResult) -> None:
        """Insert new cash-account transactions; skip anything already imported.

        Dedupe is two-layered: the provider transaction ID (account_id +
        external_id) and the legacy content index (account_name + date +
        description + amount) that also guards CSV imports.
        """
        if not payload.transactions:
            return
        account_ids = [row.id for row in account_rows.values()]
        known_external = set()
        if account_ids:
            known_external = {
                (t.account_id, t.external_id)
                for t in db.session.query(TransactionRow.account_id, TransactionRow.external_id)
                .filter(TransactionRow.account_id.in_(account_ids),
                        TransactionRow.external_id.isnot(None)).all()
            }
        for txn in payload.transactions:
            account = account_rows.get(txn.account_external_id)
            if account is None:
                continue
            if account.account_type not in (AccountType.CHECKING.value, AccountType.SAVINGS.value):
                continue  # investment activity is not spending
            if (account.id, txn.external_id) in known_external:
                result.transactions_skipped += 1
                continue
            row = TransactionRow(
                # Title-cased to match the 'Checking'/'Savings' values CSV imports
                # and the Dashboard's own filter dropdown use (see upload.html /
                # dashboard.html) — otherwise synced and CSV history would sit in
                # separate, non-matching account_name buckets.
                account_name=account.account_type.capitalize(),
                date=txn.date,
                description=txn.description,
                amount=txn.amount,
                category=txn.category_hint or self._categorize(txn.description),
                source="sync",
                account_id=account.id,
                external_id=txn.external_id,
            )
            try:
                # Savepoint so a duplicate rolls back only this row, not the
                # accounts/holdings/transactions already staged in this sync.
                with db.session.begin_nested():
                    db.session.add(row)
            except IntegrityError:
                # Same content already imported (e.g. via CSV before connecting).
                result.transactions_skipped += 1
            else:
                result.transactions_added += 1

    def _update_market_prices(self, connection: InstitutionConnection,
                              payload: SyncPayload) -> None:
        now = datetime.utcnow()
        for holding in payload.holdings:
            if not holding.current_price:
                continue
            symbol = holding.symbol.upper()
            row = MarketPrice.query.filter_by(symbol=symbol).first()
            if row is None:
                row = MarketPrice(symbol=symbol, price=holding.current_price)
                db.session.add(row)
            row.name = holding.name
            row.price = holding.current_price
            row.currency = holding.currency
            row.asset_class = holding.asset_class.value
            row.source = connection.institution
            row.as_of = now
        db.session.flush()
