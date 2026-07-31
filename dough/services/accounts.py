"""Accounts, wherever they come from, in one list.  [Phase 10]

Allowed:   models, dough.tenancy, dough.services.*, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`, `g`

A household's money sits in three kinds of place and, before this phase, each
was reachable only through a different endpoint with a different shape:

    FinancialAccount   synced from an institution   /api/accounts, /investments
    AccountBalance     typed in by hand             /api/log/balances
    Transaction.account_name  a CSV import's label  nowhere at all

A client asking "what accounts does this household have?" had to call two
endpoints, know that the third kind existed, and merge them itself. That is a
piece of domain knowledge living in the client, which is the thing a stable API
contract is supposed to take off the client's hands.

`overview()` is the merged answer. It does not replace the three underlying
tables or pretend they are the same thing — each entry says which `kind` it is,
because a synced balance and a hand-typed one have genuinely different
trustworthiness and a client showing them identically would be lying by
omission.

## Why the CSV accounts are included

They are the only record that a household has, say, a "360 Checking" at all when
nothing is linked and no balance was typed. Leaving them out would mean a
household that has only ever uploaded CSVs sees an empty accounts list while
looking at a page full of its own transactions. They carry no balance, and the
`kind` says so rather than reporting zero — which would be a number, and wrong.
"""

from __future__ import annotations

from sqlalchemy import func

__all__ = [
    'ACCOUNT_KINDS',
    'connections',
    'ledger_account_names',
    'manual_balances',
    'overview',
    'set_manual_balance',
    'synced_accounts',
]

#: Where an account entry came from. The client is told this rather than left to
#: infer it from which fields are populated.
ACCOUNT_KINDS = ('synced', 'manual', 'ledger')


def synced_accounts(active_only=True):
    """Accounts `finance_sync` maintains, ordered as the Investments page shows."""
    from models import FinancialAccount

    query = FinancialAccount.query
    if active_only:
        query = query.filter_by(is_active=True)
    return query.order_by(FinancialAccount.account_type,
                          FinancialAccount.name).all()


def connections():
    """Every linked institution, by display name."""
    from models import InstitutionConnection

    return (InstitutionConnection.query
            .order_by(InstitutionConnection.display_name).all())


def last_sync_at():
    """When any connection last synchronized, or None."""
    from models import InstitutionConnection, db

    return db.session.query(func.max(InstitutionConnection.last_sync_at)).scalar()


def manual_balances():
    """Hand-entered starting balances, by account type."""
    from models import AccountBalance

    return AccountBalance.query.order_by(AccountBalance.account_type).all()


def set_manual_balance(account_type, starting_balance):
    """Set a hand-entered balance, creating the row if needed.

    Returns `(balance, previous_value)`. The previous value is returned rather
    than logged here because the caller is the one that writes the audit record
    — this module has no request, no actor and no business deciding what is
    worth recording.
    """
    from models import AccountBalance, db

    balance = AccountBalance.query.filter_by(account_type=account_type).first()
    if balance is None:
        balance = AccountBalance(account_type=account_type)
        db.session.add(balance)
        previous = None
    else:
        previous = balance.starting_balance
    balance.starting_balance = starting_balance
    db.session.commit()
    return balance, previous


def ledger_account_names():
    """Every account name that appears on a transaction.

    `distinct()` on the column rather than a `GROUP BY` with a count: the
    question is which names exist, and the counts would be a different query
    that no caller has asked for.
    """
    from models import Transaction, db

    rows = (db.session.query(Transaction.account_name)
            .distinct().order_by(Transaction.account_name).all())
    return [row[0] for row in rows if row[0]]


def overview():
    """One list describing every account this household has, however it got here.

    Ordered synced, then manual, then ledger-only — most trustworthy first,
    which is also most useful first. A client rendering the list in order gets a
    sensible page without sorting it itself.
    """
    entries = []
    seen_names = set()

    for account in synced_accounts():
        seen_names.add(account.name)
        entries.append({
            'kind': 'synced',
            'id': account.id,
            'name': account.name,
            'account_type': account.account_type,
            'balance': float(account.balance),
            'available_balance': (float(account.available_balance)
                                  if account.available_balance is not None
                                  else None),
            'currency': account.currency,
            'mask': account.mask,
            'institution': (account.connection.institution
                            if account.connection else None),
            'connection_id': account.connection_id,
            'last_synced_at': (account.last_synced_at.isoformat() + 'Z'
                               if account.last_synced_at else None),
        })

    for balance in manual_balances():
        seen_names.add(balance.account_type)
        entries.append({
            'kind': 'manual',
            'id': None,
            'name': balance.account_type,
            'account_type': balance.account_type,
            'balance': float(balance.starting_balance),
            'available_balance': None,
            'currency': 'USD',
            'mask': None,
            'institution': None,
            'connection_id': None,
            'last_synced_at': (balance.last_updated.isoformat() + 'Z'
                               if balance.last_updated else None),
        })

    for name in ledger_account_names():
        if name in seen_names:
            continue
        entries.append({
            'kind': 'ledger',
            'id': None,
            'name': name,
            'account_type': 'other',
            # Null, not zero. This account has no known balance; reporting 0.00
            # would be a figure a client would happily add into a total.
            'balance': None,
            'available_balance': None,
            'currency': 'USD',
            'mask': None,
            'institution': None,
            'connection_id': None,
            'last_synced_at': None,
        })

    return entries
