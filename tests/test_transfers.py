"""Transfer netting: money moved between the household's own accounts.

The bug being pinned here is arithmetic, not plumbing. Every income total in
the application already excluded the `Transfer` category — `analytics`,
`core.dashboard`, `finance_context` — and **nothing ever wrote that category**,
because rules come from `CategoryRule` rows, a household starts with none, and
the AI suggester was told to skip transfers. So $2,000 swept from checking to
savings arrived as `+2000 Uncategorized` and was counted as income.

`test_a_sweep_is_not_income` is the whole report, end to end. The rest are the
edges that decide whether the pairing can be trusted to run automatically.
"""

from datetime import date

import pytest

from dough.services import analytics, transfers
from models import CategoryRule, Transaction, db


def _txn(account, day, description, amount, category='Uncategorized'):
    row = Transaction(account_name=account, date=date(2026, 3, day),
                      description=description, amount=amount,
                      category=category)
    db.session.add(row)
    return row


@pytest.fixture()
def sweep(app):
    """One $2,000 checking → savings transfer, posted a day apart."""
    out = _txn('Checking', 10, 'ONLINE TRANSFER TO SAVINGS 8842', -2000)
    into = _txn('Savings', 11, 'DEPOSIT FROM CHECKING', 2000)
    db.session.commit()
    return out, into


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_a_sweep_is_not_income(app, sweep):
    """Moving your own money between your own accounts is not earnings.

    The failing number in the report: `income` counted the arriving half.
    """
    _txn('Checking', 5, 'ACME PAYROLL', 3000.00)
    db.session.commit()

    transfers.net_out_transfers()
    summary = analytics.period_summary(
        analytics.Window(date(2026, 3, 1), date(2026, 3, 31), 'March'))

    assert summary['income'] == 3000.00, 'the sweep was counted as income'
    assert summary['spending'] == 0.0, 'the sweep was counted as spending'


def test_both_halves_are_labelled(app, sweep):
    out, into = sweep
    assert transfers.net_out_transfers() == 2
    assert out.category == 'Transfer'
    assert into.category == 'Transfer'


def test_running_it_twice_changes_nothing(app, sweep):
    transfers.net_out_transfers()
    assert transfers.net_out_transfers() == 0


# ---------------------------------------------------------------------------
# What must NOT be netted out
# ---------------------------------------------------------------------------

def test_a_refund_in_the_same_account_is_not_a_transfer(app):
    """Different accounts is the load-bearing condition.

    A merchant crediting back exactly what it charged looks identical in every
    other respect, and it is real money returning to the household.
    """
    _txn('Checking', 10, 'BIG BOX STORE', -149.99)
    _txn('Checking', 12, 'BIG BOX STORE REFUND', 149.99)
    db.session.commit()

    assert transfers.net_out_transfers() == 0


def test_unrelated_amounts_do_not_pair(app):
    _txn('Checking', 10, 'ONLINE TRANSFER', -500.00)
    _txn('Savings', 11, 'DEPOSIT', 500.01)
    db.session.commit()

    assert transfers.net_out_transfers() == 0


def test_a_gap_too_wide_does_not_pair(app):
    """Without transfer language, three days is the limit.

    A month between an outgoing and an incoming of the same size is a
    coincidence, and treating it as a transfer would delete a real income row
    from every total in the app.
    """
    _txn('Checking', 1, 'CONSULTING FEE PAID', -1200.00)
    _txn('Savings', 25, 'CLIENT PAYMENT', 1200.00)
    db.session.commit()

    assert transfers.net_out_transfers() == 0


def test_transfer_language_buys_a_longer_window(app):
    """An ACH between institutions routinely takes most of a week."""
    _txn('Checking', 1, 'ONLINE TRANSFER TO SAVINGS', -1200.00)
    _txn('Savings', 7, 'INCOMING TRANSFER', 1200.00)
    db.session.commit()

    assert transfers.net_out_transfers() == 2


def test_small_amounts_are_left_alone(app):
    """Two accounts producing an unrelated equal-and-opposite $3 is plausible.

    Mislabelling it removes a real purchase from spending, silently.
    """
    _txn('Checking', 10, 'CORNER STORE', -3.00)
    _txn('Savings', 11, 'INTEREST', 3.00)
    db.session.commit()

    assert transfers.net_out_transfers() == 0


# ---------------------------------------------------------------------------
# Pairing, repeated
# ---------------------------------------------------------------------------

def test_a_monthly_sweep_pairs_month_by_month(app):
    """Three identical $500 sweeps are three pairs, not nine.

    Each row is used at most once and the closest date wins, so February's
    debit cannot pair with March's credit and strand February's credit.
    """
    for month in (1, 2, 3):
        db.session.add(Transaction(
            account_name='Checking', date=date(2026, month, 1),
            description='ONLINE TRANSFER TO SAVINGS', amount=-500,
            category='Uncategorized'))
        db.session.add(Transaction(
            account_name='Savings', date=date(2026, month, 2),
            description='TRANSFER DEPOSIT', amount=500,
            category='Uncategorized'))
    db.session.commit()

    pairs = transfers.find_transfer_pairs()
    assert len(pairs) == 3
    for debit, credit in pairs:
        assert debit.date.month == credit.date.month


# ---------------------------------------------------------------------------
# The pass runs where categorization runs
# ---------------------------------------------------------------------------

def test_editing_a_rule_re_nets_the_transfers(app, client, sweep):
    """`_recategorize` resets every row from the rules, then re-nets.

    Without the second pass, adding any unrelated rule would silently undo the
    netting and put the sweep back into income.
    """
    transfers.net_out_transfers()

    client.post('/rules', data={'action': 'add', 'category': 'Groceries',
                                'keyword': 'WHOLE FOODS'},
                follow_redirects=True)

    out, into = sweep
    assert out.category == 'Transfer'
    assert into.category == 'Transfer'


def test_a_rule_does_not_win_over_the_arithmetic(app, sweep):
    """A rule matched a description; the pairing matched the money.

    A household with an `Income` rule on `DEPOSIT` would otherwise have the
    arriving half of every sweep booked as earnings — the original bug, wearing
    a label instead of no label.
    """
    db.session.add(CategoryRule(category='Income', keyword='DEPOSIT',
                                position=0))
    db.session.commit()

    from dough.services import rules_service
    engine = rules_service.as_engine()
    for row in Transaction.query.all():
        row.category = engine.get_category(row.description)
    assert sweep[1].category == 'Income'

    transfers.net_out_transfers()
    assert sweep[1].category == 'Transfer'
