"""Category rules per household, and the delete operations that never worked.

## What was wrong

`category_rules.json` was a single file at the repo root, read and written by
every household in the installation. `rules.py`, `dough/services/
categorization.py` and the Rules page contained no mention of a household
between them, so this was not a leak through a missing query filter: **the rules
were never tenanted at all**, and the second household to sign in saw the first
household's rule set because only one existed.

That is a disclosure of personal financial data. A rule set names the merchants
somebody actually pays — the file this replaced contained
`/planet fitness|gym membership/` and `/DEPT EDUCATION STUDENT LN|IU BURSAR/`.

## And two delete bugs

"Delete category" posted `action=remove` with a category and no keyword, so the
route called `remove_rule(category, None)`, matched nothing, returned silently,
and then flashed "Rule removed". It appeared to work and never did. There was no
`remove_category` on the engine at all.

Removing a keyword then re-categorized by `description ILIKE '%keyword%'`, which
finds nothing for a `/regex/` rule and is too broad for a plain one — it blanked
rows that a *surviving* rule still claimed.
"""

from datetime import date
from decimal import Decimal

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app


@pytest.fixture()
def tenant_app(tmp_path):
    """An app with no ambient household, so isolation cannot pass by accident."""
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'rules.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def two_households(tenant_app):
    from dough.tenancy import unscoped
    from models import Household, db

    with unscoped():
        a = Household(name='A', plaid_user_id='rt-a')
        b = Household(name='B', plaid_user_id='rt-b')
        db.session.add_all([a, b])
        db.session.commit()
        return a.id, b.id


# ── The disclosure ──────────────────────────────────────────────────────────

def test_one_households_rules_are_invisible_to_another(two_households):
    """The bug, stated as directly as it can be stated."""
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    with tenant_scope(a_id):
        rules_service.replace_all({
            'Gym': ['/planet fitness|gym membership/'],
            'Student Loan': ['/DEPT EDUCATION STUDENT LN|IU BURSAR/'],
        })

    with tenant_scope(b_id):
        rules = rules_service.all_rules()

        # The *keywords* are the disclosure, not the category names: "Student
        # Loan" is also a built-in default, and a generic category name tells
        # nobody anything. "IU BURSAR" names a specific institution.
        flattened = str(rules)
        assert 'planet fitness' not in flattened
        assert 'gym membership' not in flattened
        assert 'IU BURSAR' not in flattened
        assert 'DEPT EDUCATION' not in flattened

        assert 'Gym' not in rules
        assert rules == {}


def test_a_new_household_gets_nothing_at_all(two_households):
    """What RankParsely should have seen, and now does.  [Phase 11A.2]

    This assertion used to be `== DEFAULT_RULES`, and passing it was the bug.
    The defaults were the developer's credit union, student-loan servicer,
    broker, card issuers, auto lender and employer, so a second account really
    did open the Rules page onto somebody else's banks — the tenancy was sound
    and the seed data was not. Nothing is seeded now; `/rules/ai-suggest` reads
    the household's own descriptions instead.
    """
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS'],
                                   'Gym': ['PLANET FITNESS']})

    with tenant_scope(b_id):
        assert rules_service.all_rules() == {}


def test_no_default_rule_names_a_real_institution(two_households):
    """The regression guard for the disclosure itself.

    Not "DEFAULT_RULES is empty" — that is today's implementation and a future
    contributor may have a good reason to ship a genuinely generic starter set.
    What must never come back is a *default* that names somebody's actual bank,
    lender or employer, because that is what shipped to every household for the
    life of the previous implementation.
    """
    from rules import DEFAULT_RULES

    leaked = ('FIRST TECH FCU', 'FIRSTMARK', 'VANGUARD', 'CAPITAL ONE',
              'CHASE', 'JPMORGAN', 'TEVA')
    flattened = str(DEFAULT_RULES).upper()
    for name in leaked:
        assert name not in flattened, (
            f'{name} is back in DEFAULT_RULES. Every household that opens the '
            f'Rules page would be seeded with it — see rules.py.')


def test_editing_one_households_rules_does_not_touch_the_other(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS']})
    with tenant_scope(b_id):
        rules_service.replace_all({'Coffee': ['PEETS']})

    with tenant_scope(a_id):
        rules_service.add_rule('Coffee', 'BLUE BOTTLE')
        rules_service.remove_rule('Coffee', 'STARBUCKS')

    with tenant_scope(b_id):
        assert rules_service.all_rules() == {'Coffee': ['PEETS']}


def test_categorization_uses_the_current_households_rules(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    with tenant_scope(a_id):
        rules_service.replace_all({'Gym': ['PLANET FITNESS']})
    with tenant_scope(b_id):
        rules_service.replace_all({'Fitness': ['PLANET FITNESS']})

    with tenant_scope(a_id):
        assert rules_service.as_engine().get_category('PLANET FITNESS 55') == 'Gym'
    with tenant_scope(b_id):
        assert rules_service.as_engine().get_category('PLANET FITNESS 55') == 'Fitness'


def test_both_households_may_hold_the_same_rule(two_households):
    """The unique index must be household-composite.

    Without `household_id` leading it, the second household to add STARBUCKS to
    Coffee gets an IntegrityError that reads like a duplicate-rule error.
    """
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    for household_id in (a_id, b_id):
        with tenant_scope(household_id):
            assert rules_service.add_rule('Coffee', 'STARBUCKS') is not None


def test_clearing_all_rules_leaves_the_household_empty(two_households):
    """Cleared stays cleared, and does not touch the other household.

    The property that makes "Clear all rules" honest. It holds because
    `DEFAULT_RULES` is empty rather than because anything records that the
    person meant it — which is why removing the seed also removed the need for
    a marker column.
    """
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, b_id = two_households

    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS'], 'Gym': ['YMCA']})
    with tenant_scope(b_id):
        rules_service.replace_all({'Fuel': ['SHELL']})

    with tenant_scope(a_id):
        # Two, not three: B's `Fuel` row is not this household's to count.
        assert rules_service.clear_all() == 2
        assert rules_service.all_rules() == {}
        # The read that used to re-seed. Twice, because the old bug needed a
        # second look at the page to show itself.
        assert rules_service.all_rules() == {}
        assert rules_service.clear_all() == 0

    with tenant_scope(b_id):
        assert rules_service.all_rules() == {'Fuel': ['SHELL']}


# ── Delete category: the button that lied ───────────────────────────────────

def test_remove_category_deletes_every_keyword(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({
            'Subscriptions': ['NETFLIX', 'SPOTIFY', 'HULU'],
            'Coffee': ['STARBUCKS'],
        })

        assert rules_service.remove_category('Subscriptions') == 3
        assert rules_service.all_rules() == {'Coffee': ['STARBUCKS']}


def test_removing_a_missing_category_reports_nothing_removed(two_households):
    """The honest return value the route needs to stop lying in its flash."""
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS']})
        assert rules_service.remove_category('Nonexistent') == 0


def test_remove_rule_reports_whether_it_removed_anything(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS', 'PEETS']})

        assert rules_service.remove_rule('Coffee', 'PEETS') is True
        assert rules_service.remove_rule('Coffee', 'PEETS') is False
        assert rules_service.remove_rule('Coffee', None) is False
        assert rules_service.all_rules() == {'Coffee': ['STARBUCKS']}


def test_removing_the_last_keyword_removes_the_category(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Coffee': ['STARBUCKS']})
        rules_service.remove_rule('Coffee', 'STARBUCKS')
        assert 'Coffee' not in rules_service.all_rules()


def test_renaming_merges_rather_than_colliding(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Food': ['WHOLE FOODS', 'TRADER JOE'],
                                   'Groceries': ['WHOLE FOODS', 'ALDI']})
        rules_service.rename_category('Food', 'Groceries')

        rules = rules_service.all_rules()
        assert 'Food' not in rules
        assert sorted(rules['Groceries']) == ['ALDI', 'TRADER JOE', 'WHOLE FOODS']


# ── Priority ────────────────────────────────────────────────────────────────

def test_lower_position_wins(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Shopping': ['AMAZON'],
                                   'Subscriptions': ['AMAZON PRIME']})
        assert rules_service.as_engine().get_category('AMAZON PRIME') == 'Shopping'

        rules_service.reorder(['Subscriptions', 'Shopping'])
        assert rules_service.as_engine().get_category('AMAZON PRIME') == 'Subscriptions'


def test_add_rule_first_wins_over_everything(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({'Shopping': ['AMAZON']})
        rules_service.add_rule('Subscriptions', 'AMAZON PRIME', first=True)
        assert rules_service.as_engine().get_category('AMAZON PRIME') == 'Subscriptions'


def test_a_duplicate_rule_is_rejected_not_duplicated(two_households):
    from dough.services import rules_service
    from dough.tenancy import tenant_scope

    a_id, _ = two_households
    with tenant_scope(a_id):
        rules_service.replace_all({})
        assert rules_service.add_rule('Coffee', 'STARBUCKS') is not None
        assert rules_service.add_rule('Coffee', 'STARBUCKS') is None
        assert rules_service.all_rules() == {'Coffee': ['STARBUCKS']}


# ── The page ────────────────────────────────────────────────────────────────

@pytest.fixture()
def page(tmp_path):
    """A signed-in-equivalent app with a ledger, for exercising the route."""
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'page.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AUTH_ENABLED': False,
    })
    from dough.services import rules_service
    from dough.tenancy import tenant_scope
    from models import Transaction, db

    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            for description, category in (
                    ('STARBUCKS STORE 8891', 'Coffee'),
                    ('NETFLIX.COM', 'Subscriptions'),
                    ('SPOTIFY USA', 'Subscriptions')):
                db.session.add(Transaction(
                    account_name='checking', date=date(2026, 8, 1),
                    description=description, amount=Decimal('-10.00'),
                    category=category))
            db.session.commit()
            rules_service.replace_all({
                'Coffee': ['STARBUCKS'],
                'Subscriptions': ['NETFLIX', 'SPOTIFY'],
            })
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


def test_the_delete_category_button_actually_deletes(page):
    """The bug you reported: it posted the wrong action and claimed success."""
    from dough.services import rules_service

    response = page.test_client().post('/rules', data={
        'action': 'remove_category', 'category': 'Subscriptions'},
        follow_redirects=True)

    assert response.status_code == 200
    assert 'Subscriptions' not in rules_service.all_rules()


def test_clear_all_button_empties_the_rules_and_uncategorizes(page):
    """The button, end to end.  [Phase 11A.2]"""
    from dough.services import rules_service
    from models import Transaction

    response = page.test_client().post('/rules', data={'action': 'clear_all'},
                                       follow_redirects=True)

    assert response.status_code == 200
    assert rules_service.all_rules() == {}
    assert {t.category for t in Transaction.query.all()} == {'Uncategorized'}


def test_a_cleared_page_does_not_re_seed_on_the_next_view(page):
    """The regression this replaced the seeding with.

    Under `DEFAULT_RULES`, clearing every rule and reloading the page put the
    defaults straight back — so "clear all" could not have been implemented
    without a marker column recording that the household meant it. With nothing
    to seed, the second view is as empty as the first.
    """
    from dough.services import rules_service

    client = page.test_client()
    client.post('/rules', data={'action': 'clear_all'}, follow_redirects=True)

    body = client.get('/rules').get_data(as_text=True)

    assert rules_service.all_rules() == {}
    assert 'No rules yet' in body
    # The specific thing that used to come back.
    assert 'FIRSTMARK' not in body
    assert 'TEVA' not in body


def test_the_rules_page_never_renders_a_seeded_institution(page):
    """Belt and braces on the disclosure, at the surface a person actually sees."""
    from dough.services import rules_service

    rules_service.clear_all()
    body = page.test_client().get('/rules').get_data(as_text=True).upper()

    for name in ('FIRST TECH FCU', 'FIRSTMARK', 'VANGUARD BUY', 'CAPITAL ONE',
                 'CHASE CREDIT CRD', 'JPMORGAN', 'TEVA PHARMA'):
        assert name not in body


def test_deleting_a_category_recategorizes_its_transactions(page):
    from models import Transaction

    page.test_client().post('/rules', data={
        'action': 'remove_category', 'category': 'Subscriptions'},
        follow_redirects=True)

    netflix = Transaction.query.filter_by(description='NETFLIX.COM').one()
    assert netflix.category == 'Uncategorized'


def test_deleting_a_keyword_leaves_rows_a_surviving_rule_still_claims(page):
    """The over-broad `ILIKE` bug.

    Removing SPOTIFY must not disturb the NETFLIX row, which the surviving
    NETFLIX rule still categorizes as Subscriptions.
    """
    from models import Transaction

    page.test_client().post('/rules', data={
        'action': 'remove', 'category': 'Subscriptions', 'keyword': 'SPOTIFY'},
        follow_redirects=True)

    assert Transaction.query.filter_by(
        description='NETFLIX.COM').one().category == 'Subscriptions'
    assert Transaction.query.filter_by(
        description='SPOTIFY USA').one().category == 'Uncategorized'


def test_removing_a_regex_rule_recategorizes_what_it_had_claimed(page):
    """The `ILIKE '%/regex/%'` bug: it matched nothing, so nothing was fixed."""
    from dough.services import rules_service
    from models import Transaction

    rules_service.replace_all({'Streaming': ['/netflix|spotify/']})
    page.test_client().post('/rules', data={
        'action': 'add', 'category': 'Placeholder', 'keyword': 'ZZZNOMATCH'},
        follow_redirects=True)
    assert Transaction.query.filter_by(
        description='NETFLIX.COM').one().category == 'Streaming'

    page.test_client().post('/rules', data={
        'action': 'remove', 'category': 'Streaming',
        'keyword': '/netflix|spotify/'}, follow_redirects=True)

    assert Transaction.query.filter_by(
        description='NETFLIX.COM').one().category == 'Uncategorized'


def test_deleting_something_absent_does_not_claim_success(page):
    response = page.test_client().post('/rules', data={
        'action': 'remove_category', 'category': 'Nonexistent'},
        follow_redirects=True)

    body = response.get_data(as_text=True)
    assert 'no Nonexistent rule to delete' in body


def test_the_page_lists_only_this_households_rules(page):
    body = page.test_client().get('/rules').get_data(as_text=True)
    assert 'STARBUCKS' in body
    assert 'PLANET FITNESS' not in body


# ── The seed rows the code fix left behind  [2026-08-03] ────────────────────

def test_match_counts_measures_rules_not_labels(two_households):
    """The Rules page's Transactions column used to describe the wrong thing.

    Reported from the running app: a household's page listed
    `Student Loan — First Tech FCU, FIRSTMARK — 56` for a rule matching none of
    its transactions. The number was real; it just answered a different
    question. `Transaction.category == 'Student Loan'` counts rows *wearing*
    that label, and a label outlives the rule that wrote it — categories are
    re-derived only on a rule edit or at import, never on a page view.

    So a rule set that had stopped matching anything still presented healthy
    numbers, which is exactly the evidence a person uses to conclude their
    rules are fine. Counting matches instead means the column can only say
    something true about the rules beside it, and a stale label reads 0.
    """
    from datetime import date
    from decimal import Decimal

    from dough.services import rules_service
    from dough.tenancy import tenant_scope
    from models import Transaction, db

    a_id, _ = two_households

    with tenant_scope(a_id):
        # Labelled `Student Loan` by some earlier categorization, but nothing
        # in the description any Student Loan rule could match.
        for i in range(3):
            db.session.add(Transaction(
                account_name='Checking', date=date(2026, 8, 1),
                description=f'WHOLE FOODS MARKET #{i}', amount=Decimal('-20.00'),
                category='Student Loan'))
        db.session.commit()

        rules_service.replace_all({'Student Loan': ['FIRSTMARK'],
                                   'Groceries': ['WHOLE FOODS']})

        counts = rules_service.match_counts()

        # The label count is 3 and would have been reported as 3.
        assert Transaction.query.filter(
            Transaction.category == 'Student Loan').count() == 3
        # What the rules actually claim.
        assert counts.get('Student Loan', 0) == 0
        assert counts['Groceries'] == 3


def test_match_counts_respects_priority_and_does_not_double_count(two_households):
    """A transaction claimed by two rules counts once, for the winner.

    The column sums to the number of categorized transactions rather than to
    the number of (rule, transaction) hits, which is what makes it readable as
    "how much of my ledger does this category own".
    """
    from datetime import date
    from decimal import Decimal

    from dough.services import rules_service
    from dough.tenancy import tenant_scope
    from models import Transaction, db

    a_id, _ = two_households

    with tenant_scope(a_id):
        db.session.add(Transaction(
            account_name='Checking', date=date(2026, 8, 1),
            description='AMAZON GROCERY', amount=Decimal('-31.00'),
            category='Uncategorized'))
        db.session.commit()

        # Both match; Groceries is written first, so it wins on position.
        rules_service.replace_all({'Groceries': ['GROCERY'],
                                   'Shopping': ['AMAZON']})

        counts = rules_service.match_counts()
        assert counts == {'Groceries': 1}
        assert sum(counts.values()) == 1


def test_the_unseed_migration_clears_a_household_that_only_ever_got_the_seed():
    """The seven `DEFAULT_RULES` rows, removed from households that only hold them.

    Phase 11A.2 emptied `DEFAULT_RULES` and deleted `seed_defaults()`, which
    stops *new* households receiving the developer's credit union, student-loan
    servicer, broker, card issuers, auto lender and employer. It did not touch
    the rows already written, so every household seeded before that commit kept
    its copy — which is what put `First Tech FCU` and `VANGUARD BUY` on a real
    user's Rules page for merchants they have never transacted with.

    `docs/rule-engine.md` states the reason the code fix was not sufficient on
    its own: "No amount of correct filtering fixes seed data that should never
    have been written."
    """
    from migrations.versions import (
        __name__ as _versions,  # noqa: F401  (ensures the package imports)
    )
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations', 'versions', '20260803_10_unseed_default_rules.py')
    spec = importlib.util.spec_from_file_location('unseed_mig', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    seeded = module.SEEDED_PAIRS

    # The set is the historical constant, recovered from `26720e6~1`. Asserted
    # rather than trusted: a migration that deletes by literal is only correct
    # if the literal is right, and nothing else in the tree still holds it.
    assert ('Student Loan', 'First Tech FCU') in seeded
    assert ('Investments', 'VANGUARD BUY') in seeded
    assert ('Income', 'TEVA PHARMA') in seeded
    assert len(seeded) == 7

    # `(id, household_id, category, keyword)` — the shape `upgrade()` selects.
    rows = [
        # Household 1: the full seed and nothing else. This is the household in
        # the bug report, and every row of it goes.
        (1, 1, 'Student Loan', 'First Tech FCU'),
        (2, 1, 'Student Loan', 'FIRSTMARK'),
        (3, 1, 'Investments', 'VANGUARD BUY'),
        (4, 1, 'Credit Card', 'CAPITAL ONE'),
        (5, 1, 'Credit Card', 'CHASE CREDIT CRD'),
        (6, 1, 'Auto Loan', 'JPMorgan Chase'),
        (7, 1, 'Income', 'TEVA PHARMA'),

        # Household 2: seeded rules *plus* rules of its own. Untouched — the
        # conservative direction, because deleting by pair alone would destroy
        # real hand-written rules in a household that genuinely banks with
        # First Tech FCU. See the migration's docstring.
        (8, 2, 'Student Loan', 'First Tech FCU'),
        (9, 2, 'Groceries', 'WHOLE FOODS'),

        # Household 3: never seeded. Nothing to do.
        (10, 3, 'Dining', 'CHIPOTLE'),

        # Household 4: a partial seed — it opened the page once and got some of
        # it. Still entirely somebody else's data, so it still goes.
        (11, 4, 'Investments', 'VANGUARD BUY'),
    ]

    assert sorted(module.rows_to_delete(rows)) == [1, 2, 3, 4, 5, 6, 7, 11]
