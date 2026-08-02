"""`dough/services/analytics.py` — the aggregation layer Phase 11 rests on.

Three kinds of check live here, and the split is deliberate:

1. **Arithmetic**, against a ledger small enough to add up by hand. Every
   expected figure below is written as a sum of the literal rows in `ledger`,
   not as a call to the function under test, so a change in the function cannot
   quietly redefine what is correct.
2. **The structural promises the module docstring makes** — one query per
   summary, and no household crossing. Those are the two properties the rest of
   Phase 11 inherits by calling this module instead of writing its own SQL, so
   they are asserted rather than assumed.
3. **The edges that produce a fabricated number**: no baseline, no income, a
   month with no rows. Each of these has a "reasonable" wrong answer — 0%, 100%,
   a closed gap — and the point of the assertions is that the module returns
   `None` or a real zero instead.
"""

from datetime import date
from decimal import Decimal

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.services import analytics


# ── Fixtures ────────────────────────────────────────────────────────────────

#: A hand-checkable ledger. Two months, two accounts, one transfer pair.
#:
#: February totals, excluding transfers:  spending 430.00, income 3000.00
#: March totals,    excluding transfers:  spending 615.00, income 3200.00
LEDGER = [
    # (date, description, amount, category, account)
    (date(2026, 2, 3),  'Whole Foods',      -120.00, 'Groceries', 'checking'),
    (date(2026, 2, 9),  'Blue Bottle',       -30.00, 'Dining',    'checking'),
    (date(2026, 2, 14), 'Olive Garden',      -80.00, 'Dining',    'checking'),
    (date(2026, 2, 20), 'Shell',            -200.00, 'Gas',       'checking'),
    (date(2026, 2, 28), 'Paycheck',         3000.00, 'Income',    'checking'),
    (date(2026, 2, 28), 'To savings',       -500.00, 'Transfer',  'checking'),
    (date(2026, 2, 28), 'From checking',     500.00, 'Transfer',  'savings'),

    (date(2026, 3, 2),  'Whole Foods',      -150.00, 'Groceries', 'checking'),
    (date(2026, 3, 5),  'Blue Bottle',       -45.00, 'Dining',    'checking'),
    (date(2026, 3, 11), 'Delta Air Lines',  -320.00, 'Travel',    'checking'),
    (date(2026, 3, 18), 'Shell',            -100.00, 'Gas',       'checking'),
    (date(2026, 3, 31), 'Paycheck',         3200.00, 'Income',    'checking'),
]

FEB = analytics.resolve_window('month', date(2026, 2, 15))
MAR = analytics.resolve_window('month', date(2026, 3, 15))


@pytest.fixture()
def ledger(app):
    """The rows above, committed into the ambient test household."""
    from models import Transaction, db

    for when, description, amount, category, account in LEDGER:
        db.session.add(Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category))
    db.session.commit()
    return app


# ── Windows ─────────────────────────────────────────────────────────────────

def test_month_window_covers_the_whole_month():
    window = analytics.resolve_window('month', date(2026, 2, 15))
    assert (window.start, window.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert window.label == 'February 2026'


def test_february_in_a_leap_year_ends_on_the_29th():
    """The one month whose length is not a constant."""
    window = analytics.resolve_window('month', date(2028, 2, 1))
    assert window.end == date(2028, 2, 29)


def test_quarter_and_year_windows():
    q = analytics.resolve_window('quarter', date(2026, 5, 20))
    assert (q.start, q.end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert q.label == 'Q2 2026'

    y = analytics.resolve_window('year', date(2026, 7, 4))
    assert (y.start, y.end) == (date(2026, 1, 1), date(2026, 12, 31))
    assert y.label == '2026'


def test_unknown_period_kind_raises_rather_than_defaulting():
    """A typo must not silently return this month -- see `resolve_window`."""
    with pytest.raises(ValueError):
        analytics.resolve_window('fortnight')


def test_preceding_month_is_the_calendar_month_not_thirty_days():
    """March's predecessor is February, all 28 days of it.

    The "30 days before" reading would start on 29 January, pulling a January
    paycheck into a February comparison.
    """
    previous = analytics.preceding_window(MAR)
    assert (previous.start, previous.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert previous.label == 'February 2026'


def test_preceding_year_and_quarter():
    assert analytics.preceding_window(
        analytics.resolve_window('year', date(2026, 6, 1))).label == '2025'
    assert analytics.preceding_window(
        analytics.resolve_window('quarter', date(2026, 4, 1))).label == 'Q1 2026'


def test_preceding_custom_window_matches_its_length():
    window = analytics.custom_window(date(2026, 3, 10), date(2026, 3, 19))
    previous = analytics.preceding_window(window)
    assert previous.days == window.days == 10
    assert (previous.start, previous.end) == (date(2026, 2, 28), date(2026, 3, 9))


def test_reversed_custom_window_is_swapped_not_rejected():
    window = analytics.custom_window(date(2026, 3, 31), date(2026, 3, 1))
    assert (window.start, window.end) == (date(2026, 3, 1), date(2026, 3, 31))


def test_month_keys_include_empty_months():
    keys = analytics.month_keys_between(date(2025, 11, 4), date(2026, 2, 2))
    assert keys == ['2025-11', '2025-12', '2026-01', '2026-02']


# ── Period summary ──────────────────────────────────────────────────────────

def test_period_summary_totals_match_the_ledger(ledger):
    """Every figure here is the literal rows above, added up by hand."""
    summary = analytics.period_summary(FEB)

    assert summary['spending'] == 430.00      # 120 + 30 + 80 + 200
    assert summary['income'] == 3000.00
    assert summary['net'] == 2570.00
    assert summary['by_category'] == {
        'Gas': 200.00, 'Groceries': 120.00, 'Dining': 110.00}


def test_transfers_are_excluded_by_default_and_included_on_request(ledger):
    """The 500 moved to savings is not spending, unless you ask about it."""
    assert analytics.period_summary(FEB)['spending'] == 430.00
    assert analytics.period_summary(
        FEB, include_transfers=True)['spending'] == 930.00


def test_by_category_is_ordered_largest_first(ledger):
    assert list(analytics.period_summary(FEB)['by_category']) == [
        'Gas', 'Groceries', 'Dining']


def test_summary_can_be_scoped_to_one_account(ledger):
    """The savings account saw only the incoming transfer."""
    savings = analytics.period_summary(FEB, account='savings',
                                       include_transfers=True)
    assert savings['income'] == 500.00
    assert savings['spending'] == 0.0


def test_savings_rate_is_none_without_income(ledger):
    """Not 0%, and not 100%. There is no rate to report.

    A rate of 0 reads as "you saved nothing", which is a claim about the
    household rather than about the data.
    """
    empty = analytics.custom_window(date(2026, 1, 1), date(2026, 1, 31))
    assert analytics.period_summary(empty)['savings_rate'] is None


def test_savings_rate_is_computed_when_there_is_income(ledger):
    # (3000 - 430) / 3000 = 85.7%
    assert analytics.period_summary(FEB)['savings_rate'] == 85.7


def test_period_summary_issues_one_query(ledger):
    """The docstring's central promise, asserted rather than assumed.

    Counted, not timed: a wall-clock threshold passes on a fast machine no
    matter how many round trips it took.
    """
    from sqlalchemy import event

    from models import db

    seen = []
    engine = db.session.get_bind()

    def record(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith('SELECT'):
            seen.append(statement)

    event.listen(engine, 'before_cursor_execute', record)
    try:
        analytics.period_summary(MAR)
    finally:
        event.remove(engine, 'before_cursor_execute', record)

    assert len(seen) == 1, f'expected one SELECT, got {len(seen)}:\n' + '\n'.join(seen)


# ── Rollups ─────────────────────────────────────────────────────────────────

def test_merchant_totals_rank_by_spend(ledger):
    merchants = analytics.merchant_totals(MAR, limit=3)
    assert [m['description'] for m in merchants] == [
        'Delta Air Lines', 'Whole Foods', 'Shell']
    assert merchants[0]['total'] == 320.00
    assert merchants[0]['transactions'] == 1


def test_merchant_totals_aggregate_repeat_charges(ledger):
    """Two Blue Bottle visits across both months are one row over both."""
    window = analytics.custom_window(date(2026, 2, 1), date(2026, 3, 31))
    blue = next(m for m in analytics.merchant_totals(window)
                if m['description'] == 'Blue Bottle')
    assert blue['total'] == 75.00        # 30 + 45
    assert blue['transactions'] == 2
    assert blue['last_seen'] == '2026-03-05'


def test_largest_purchases_are_rows_not_categories(ledger):
    biggest = analytics.largest_purchases(MAR, limit=2)
    assert biggest[0]['description'] == 'Delta Air Lines'
    assert biggest[0]['amount'] == 320.00       # positive, as displayed
    assert biggest[1]['description'] == 'Whole Foods'


def test_monthly_series_keeps_empty_months_as_zero(ledger):
    """January has no rows at all and must still appear.

    A closed gap turns "you spent nothing in January" into "January did not
    happen", which is the trend line silently lying about its own axis.
    """
    series = analytics.monthly_series(date(2026, 1, 1), date(2026, 3, 31))
    assert list(series) == ['2026-01', '2026-02', '2026-03']
    assert series['2026-01'] == {'spending': 0.0, 'income': 0.0, 'net': 0.0}
    assert series['2026-03']['spending'] == 615.00


def test_monthly_category_series_splits_by_month(ledger):
    series = analytics.monthly_category_series(date(2026, 2, 1), date(2026, 3, 31))
    assert series['2026-02']['Dining'] == 110.00
    assert series['2026-03']['Travel'] == 320.00
    assert 'Travel' not in series['2026-02']


def test_coverage_reports_the_real_range(ledger):
    found = analytics.coverage()
    assert found['first_transaction'] == '2026-02-03'
    assert found['last_transaction'] == '2026-03-31'
    assert found['total_transactions'] == len(LEDGER)
    assert found['accounts'] == ['checking', 'savings']
    assert found['months_of_history'] == 2


def test_coverage_on_an_empty_ledger_says_so(app):
    found = analytics.coverage()
    assert found['first_transaction'] is None
    assert found['total_transactions'] == 0
    assert found['months_of_history'] == 0


# ── pct_change ──────────────────────────────────────────────────────────────

def test_pct_change_reports_none_without_a_baseline():
    """"Up 100%" and "nothing to compare against" are different statements."""
    assert analytics.pct_change(500.0, 0.0) is None
    assert analytics.pct_change(500.0, 400.0) == 25.0
    assert analytics.pct_change(300.0, 400.0) == -25.0


# ── Tenancy ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tenant_app(tmp_path):
    """An app with no ambient household -- see tests/test_tenancy_boundary.py.

    The shared `app` fixture binds one for the whole test, which would make the
    isolation assertion below pass for the wrong reason.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


def test_analytics_never_crosses_a_household(tenant_app):
    """Two households, deliberately identical figures, read back separately.

    The amounts differ by household so that a leak shows up as a wrong *total*
    rather than as a duplicate row -- if both sides held the same number, a
    query returning both would still sum to something plausible.
    """
    from dough.tenancy import tenant_scope, unscoped
    from models import Household, Transaction, db

    with unscoped():
        a = Household(name='A', plaid_user_id='an-a')
        b = Household(name='B', plaid_user_id='an-b')
        db.session.add_all([a, b])
        db.session.commit()
        a_id, b_id = a.id, b.id

    for hid, amount in ((a_id, '-100.00'), (b_id, '-999.00')):
        with tenant_scope(hid):
            db.session.add(Transaction(
                account_name='checking', date=date(2026, 2, 10),
                description='Groceries', amount=Decimal(amount),
                category='Groceries'))
            db.session.commit()

    with tenant_scope(a_id):
        assert analytics.period_summary(FEB)['spending'] == 100.00
        assert analytics.coverage()['total_transactions'] == 1
        assert analytics.merchant_totals(FEB)[0]['total'] == 100.00

    with tenant_scope(b_id):
        assert analytics.period_summary(FEB)['spending'] == 999.00
        assert analytics.coverage()['total_transactions'] == 1
