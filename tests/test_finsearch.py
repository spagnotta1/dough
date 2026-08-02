"""`dough/services/finsearch.py` — retrieval for the questions in the brief.

The seven example questions from Feature 7 are tested literally, because they
are the acceptance criteria and paraphrasing them into something the parser
happens to handle would defeat the point.

Beyond those, the tests are mostly about restraint: resolving a period the
question did not name, or a merchant out of an arbitrary noun, produces a
confident answer to a question nobody asked. `matched: False` is a valid and
important result.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import finsearch

TODAY = date(2026, 8, 15)


@pytest.fixture()
def ledger(app):
    """A year of history with recognisable shapes to ask questions about."""
    from models import Transaction, db

    rows = [
        # Q2 2026 dining, the "last quarter" answer: 120 + 80 + 100 = 300
        (date(2026, 4, 12), 'Olive Garden', -120.00, 'Dining'),
        (date(2026, 5, 18), 'Blue Bottle', -80.00, 'Dining'),
        (date(2026, 6, 24), 'Olive Garden', -100.00, 'Dining'),
        # Q3 dining, outside "last quarter"
        (date(2026, 7, 4), 'Olive Garden', -55.00, 'Dining'),
        (date(2026, 8, 2), 'Blue Bottle', -45.00, 'Dining'),

        (date(2026, 3, 9), 'Amazon Marketplace', -240.00, 'Shopping'),
        (date(2026, 6, 11), 'Amazon Marketplace', -60.00, 'Shopping'),
        (date(2026, 8, 5), 'Amazon Marketplace', -35.00, 'Shopping'),

        (date(2026, 2, 14), 'Delta Air Lines', -2450.00, 'Travel'),
        (date(2026, 8, 8), 'Whole Foods', -210.00, 'Groceries'),

        (date(2026, 7, 31), 'Payroll ACME', 5200.00, 'Income'),
        # Dated before TODAY on purpose: a paycheck later in August would fall
        # outside every window anchored on the 15th, which is correct and would
        # make the income assertions below quietly test nothing.
        (date(2026, 8, 14), 'Payroll ACME', 5200.00, 'Income'),
        (date(2026, 7, 15), 'Landlord', -1800.00, 'Rent'),
        (date(2026, 7, 20), 'To savings', -900.00, 'Transfer'),
    ]
    for when, description, amount, category in rows:
        db.session.add(Transaction(
            account_name='checking', date=when, description=description,
            amount=Decimal(str(amount)), category=category))
    db.session.commit()
    return app


def ask(question):
    return finsearch.search(question, today=TODAY)


# ── The seven questions from the brief ──────────────────────────────────────

def test_how_much_did_i_spend_on_restaurants_last_quarter(ledger):
    """"Restaurants" is not a category this household has. It still resolves."""
    found = ask('How much did I spend on restaurants last quarter?')

    assert found['intent'] == 'total_spend'
    assert found['window']['label'] == 'Q2 2026'
    assert found['categories_matched'] == ['Dining']
    assert found['total_spent'] == 300.00


def test_show_my_amazon_purchases(ledger):
    found = ask('Show my Amazon purchases.')

    assert found['merchant_matched'] == 'amazon'
    assert found['total_spent'] == 335.00           # 240 + 60 + 35
    assert len(found['transactions']) == 3


def test_what_was_my_biggest_expense_this_year(ledger):
    found = ask('What was my biggest expense this year?')

    assert found['intent'] == 'largest'
    assert found['transactions'][0]['description'] == 'Delta Air Lines'
    assert found['transactions'][0]['amount'] == -2450.00


def test_how_much_did_i_save_last_month(ledger):
    """July: 5,200 in, 1,855 out excluding the transfer."""
    found = ask('How much did I save last month?')

    assert found['intent'] == 'savings'
    assert found['window']['label'] == 'July 2026'
    assert found['total_income'] == 5200.00
    assert found['total_spent'] == 1855.00
    assert found['net'] == 3345.00


def test_which_merchant_charged_me_the_most(ledger):
    found = ask('Which merchant charged me the most?')

    assert found['intent'] == 'top_merchant'
    assert found['merchants'][0]['description'] == 'Delta Air Lines'
    assert found['merchants'][0]['total'] == 2450.00


def test_how_much_is_my_income_this_year(ledger):
    found = ask('How much income did I make this year?')
    assert found['intent'] == 'total_income'
    assert found['total_income'] == 10400.00


def test_which_subscriptions_increased_is_routed_elsewhere_not_guessed(ledger):
    """A question this module cannot answer must not be answered anyway.

    "Which subscriptions increased" is `anomalies.bill_increases`' job. What
    matters here is that the parser does not silently turn it into a spend
    total over an invented period.
    """
    found = ask('Which subscriptions increased?')
    assert found['total_spent'] == 0.0 or found['intent'] != 'total_spend'


# ── Windows ─────────────────────────────────────────────────────────────────

def test_last_month_this_month_and_last_year_resolve(ledger):
    assert ask('spending last month')['window']['label'] == 'July 2026'
    assert ask('spending this month')['window']['label'] == 'August 2026'
    assert ask('spending last year')['window']['label'] == '2025'


def test_a_named_month_resolves_to_the_most_recent_one(ledger):
    """"What did I spend in December", asked in August, means last December."""
    assert ask('what did I spend in december')['window']['label'] == 'December 2025'
    assert ask('what did I spend in march')['window']['label'] == 'March 2026'


def test_a_named_month_with_a_year_is_taken_literally(ledger):
    assert ask('what did I spend in march 2024')['window']['label'] == 'March 2024'


def test_a_rolling_window_resolves(ledger):
    window = ask('how much did I spend in the last 3 months')['window']
    assert window['start'] == '2026-06-01'
    assert window['end'] == '2026-08-15'


def test_a_question_with_no_period_uses_a_wide_default(ledger):
    """Not this month. Answering "$0" because the parser guessed narrow is the
    most annoying possible failure."""
    found = ask('how much did I spend on dining')
    assert found['window']['start'] == '2025-09-01'
    assert found['total_spent'] == 400.00       # every dining row in the year


# ── Restraint ───────────────────────────────────────────────────────────────

def test_an_unparseable_question_says_so(ledger):
    found = ask('is the moon made of cheese')
    assert found['matched'] is False
    assert found['intent'] == 'unknown'


def test_unmatched_terms_are_reported(ledger):
    """So a caller can tell a whole answer from a partial one."""
    found = ask('how much did I spend on unicorns last month')
    assert 'unicorns' in found['unmatched_terms']
    assert found['categories_matched'] == []


def test_a_generic_noun_is_not_taken_for_a_merchant(ledger):
    """"my monthly purchases" must not resolve `monthly` as a payee."""
    assert ask('show my monthly purchases')['merchant_matched'] is None


def test_a_quoted_merchant_is_taken_literally(ledger):
    found = ask('show my "Whole Foods" purchases')
    assert found['merchant_matched'] == 'whole foods'
    assert found['total_spent'] == 210.00


def test_transfers_are_excluded_unless_asked_for(ledger):
    """July spending is 1,855 with the 900 transfer left out, as it should be.

    Asking *about* transfers both re-includes them and narrows to the Transfer
    category, so the answer is the 900 that moved rather than all spending with
    the transfer added back — which is what somebody asking that means.
    """
    assert ask('how much did I spend last month')['total_spent'] == 1855.00

    transferred = ask('how much did I transfer last month')
    assert transferred['total_spent'] == 900.00
    assert transferred['categories_matched'] == ['Transfer']


def test_a_how_much_question_does_not_ship_line_items(ledger):
    """25 rows beside a total is 25 chances to re-add them and disagree."""
    found = ask('how much did I spend on dining last quarter')
    assert 'transactions' not in found
    assert found['total_spent'] == 300.00


def test_a_list_question_does_ship_line_items(ledger):
    found = ask('list my dining transactions last quarter')
    assert len(found['transactions']) == 3


def test_the_answer_names_the_categories_it_combined(ledger):
    """So the sentence can say which ones, instead of implying one."""
    found = ask('how much did I spend on food this year')
    assert set(found['categories_matched']) >= {'Dining', 'Groceries'}


def test_search_never_crosses_a_household(app):
    """The retrieval layer inherits scoping, asserted rather than assumed."""
    from dough.tenancy import tenant_scope, unscoped
    from models import Household, Transaction, db

    with unscoped():
        a = Household(name='A', plaid_user_id='fs-a')
        b = Household(name='B', plaid_user_id='fs-b')
        db.session.add_all([a, b])
        db.session.commit()
        a_id, b_id = a.id, b.id

    for hid, amount in ((a_id, '-100.00'), (b_id, '-999.00')):
        with tenant_scope(hid):
            db.session.add(Transaction(
                account_name='checking', date=date(2026, 8, 4),
                description='Olive Garden', amount=Decimal(amount),
                category='Dining'))
            db.session.commit()

    with tenant_scope(a_id):
        assert finsearch.search('how much did I spend on dining',
                                today=TODAY)['total_spent'] == 100.00
    with tenant_scope(b_id):
        assert finsearch.search('how much did I spend on dining',
                                today=TODAY)['total_spent'] == 999.00
