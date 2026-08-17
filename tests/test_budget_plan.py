"""The Budgets tab as a plan rather than a scoreboard.

`tests/test_api_v1.py` already owns the original contract — the upsert, the
banding, the month window and the 1st-of-the-month regression. This file covers
what the page gained afterwards, and each group here exists because the old
page could render something reassuring and untrue.

- **Projection.** Spend-to-date against a limit is a report on days already
  gone. The tests pin the clock rather than reading it, because "is this budget
  heading over?" is a question whose answer changes with the date and a test
  that runs on the 28th would pass for the wrong reason.
- **The anchor.** Limits summing to more than the household earns is the one
  arrangement a budget page must never render as calm green cards, and nothing
  on the page knew what anybody earned.
- **Unplanned spending.** The grid loops over budgets, so money outside them
  was invisible while the header read "$X of $Y budgeted" in the position where
  a reader takes it for total spending.
- **One answer to "am I over".** The dashboard used to compute its own, from
  its own window, in the same words. The last test in this file is the one that
  keeps the second implementation deleted.

Every clock-dependent case anchors on `ANCHOR` — the 10th of a 31-day month, so
that a third of the month has elapsed and a projection is a multiple of three
rather than a number nobody can check by eye.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from dough.services import budgets as budget_service
from models import Budget, Transaction, db

#: The 10th of August 2026 — 10 of 31 days, so `month_progress` is 32% and a
#: budget with $300 spent projects to about $940.
ANCHOR = datetime(2026, 8, 10, 12, 0, 0)


@pytest.fixture()
def post(app):
    def _post(when, description, amount, category='Groceries',
              account='Checking'):
        db.session.add(Transaction(
            account_name=account, date=when, description=description,
            amount=Decimal(str(amount)), category=category))
        db.session.commit()
    return _post


@pytest.fixture()
def budget():
    def _budget(category, limit, account='both'):
        db.session.add(Budget(category=category, account_name=account,
                              monthly_limit=Decimal(str(limit))))
        db.session.commit()
    return _budget


@pytest.fixture()
def household(post):
    """Six complete months, plus ten days of August.

    Groceries is steady, Dining climbs, and the household is paid on the 1st.
    Complete months run March–July so that `suggested_limits` has six months of
    evidence that do not include the partial one.

    Groceries is $418 rather than a round number on purpose: a fixture sitting
    exactly on the rounding step would let `_round_limit` return its input and
    still pass the test that exists to prove it rounds up.
    """
    for month in range(3, 9):
        post(date(2026, month, 1), 'Payroll ACME', 4000.00, 'Income')
        post(date(2026, month, 2), 'Landlord', -1500.00, 'Rent')
        post(date(2026, month, 3), 'Whole Foods', -418.00, 'Groceries')
        post(date(2026, month, 4), 'Olive Garden', -(120 + (month - 3) * 10),
             'Dining')


# ── Where a budget is heading ───────────────────────────────────────────────

def test_a_budget_under_its_limit_can_still_be_heading_over(app, post, budget):
    """The whole reason the page needed a tense.

    $300 of a $500 budget on the 10th is 60% used and reads as comfortable. At
    that pace it finishes the month around $940 — nearly twice the limit — and
    the only moment anybody can act on that is now.
    """
    budget('Dining', 500)
    post(date(2026, 8, 5), 'Restaurant', -300.00, category='Dining')

    row = budget_service.status(today=ANCHOR)['budgets'][0]

    assert row['state'] == 'ok', 'spent-to-date is genuinely under the limit'
    assert row['projected'] == pytest.approx(937.5)
    assert row['projected_state'] == 'danger'


def test_a_projection_on_the_first_of_the_month_does_not_divide_by_zero(app, post,
                                                                        budget):
    """`month_progress` is 3% on the 1st and rounds lower on a longer month.

    A 500 on the page somebody opened *because* it is a new month is the worst
    possible time for one, so `project` floors the elapsed fraction at 1%.
    """
    budget('Dining', 500)
    post(date(2026, 8, 1), 'Restaurant', -40.00, category='Dining')

    row = budget_service.status(
        today=datetime(2026, 8, 1, 0, 30))['budgets'][0]
    assert row['projected'] > 0
    assert row['confidence'] == 'low', 'day one is not a forecast'


def test_a_budget_with_no_spending_projects_to_nothing(app, budget):
    budget('Dining', 500)
    row = budget_service.status(today=ANCHOR)['budgets'][0]
    assert row['projected'] == 0.0
    assert row['projected_state'] == 'ok'


def test_the_page_and_the_copilot_project_the_same_number(app, post, budget):
    """The unification, asserted rather than intended.

    `_project_budgets` in `dough/ai/copilot.py` was the only implementation of
    this arithmetic and nothing reachable from a route called it. Now the page
    shows a projection and the briefing describes one; a card reading "on pace
    for $612" beside a briefing that says $580 is the same class of bug as two
    answers to "am I over budget".
    """
    from dough.ai.copilot import _project_budgets
    from dough.services import ai_context

    budget('Dining', 500)
    post(date(2026, 8, 5), 'Restaurant', -300.00, category='Dining')

    page_row = budget_service.status(today=ANCHOR)['budgets'][0]
    context = ai_context.build(['budgets'], anchor=date(2026, 8, 10))['budgets']
    briefing_row = _project_budgets(context)['budgets'][0]

    assert briefing_row['projected_month_end'] == page_row['projected']


# ── The income anchor ───────────────────────────────────────────────────────

def test_the_plan_is_measured_against_last_complete_months_take_home(app,
                                                                    household):
    """Not against income received so far.

    This household is paid on the 1st, so on the 10th both figures happen to
    agree — the assertion that matters is `basis_source`, because for anyone
    paid on the 28th "so far this month" is zero and a plan measured against it
    would report them as catastrophically over-committed once a month, every
    month.
    """
    plan = budget_service.plan(today=ANCHOR)

    assert plan['basis_source'] == 'last_month'
    assert plan['basis'] == 4000.00
    assert plan['take_home_prior'] == 4000.00


def test_a_household_with_no_prior_income_falls_back_to_this_month(app, post):
    post(date(2026, 8, 2), 'First paycheck', 1200.00, 'Income')

    plan = budget_service.plan(today=ANCHOR)
    assert plan['basis_source'] == 'this_month'
    assert plan['basis'] == 1200.00


def test_limits_that_promise_more_than_the_household_earns_say_so(app, household,
                                                                 budget):
    """The arrangement the old page rendered as four calm green cards."""
    budget('Rent', 1500)
    budget('Groceries', 2000)
    budget('Dining', 2000)

    plan = budget_service.plan(today=ANCHOR)

    assert plan['planned'] == 5500.00
    assert plan['over_committed'] is True
    assert plan['unallocated'] == -1500.00, 'reported as a shortfall, not clamped'


def test_unallocated_take_home_is_reported_when_there_is_room(app, household,
                                                             budget):
    budget('Rent', 1500)

    plan = budget_service.plan(today=ANCHOR)
    assert plan['over_committed'] is False
    assert plan['unallocated'] == 2500.00


def test_safe_to_spend_shows_the_arithmetic_it_rests_on(app, household, budget):
    """A bottom line without its basis is one the reader over-trusts or ignores.

    Every component is returned, signed, so the page can print the subtraction
    rather than a number nobody can check.
    """
    budget('Rent', 1500)
    plan = budget_service.plan(today=ANCHOR)
    safe = plan['safe_to_spend']

    labels = [c['label'] for c in safe['components']]
    assert labels[0] == 'Take-home'
    assert 'Bills still to come' in labels
    assert 'Goal contributions' in labels

    total = sum(c['amount'] * c['sign'] for c in safe['components'])
    assert safe['amount'] == pytest.approx(total, abs=0.01)


# ── Spending with no budget against it ──────────────────────────────────────

def test_spending_outside_every_budget_is_surfaced(app, household, budget):
    """The money the grid could not render, because the grid loops over budgets."""
    budget('Rent', 1500)

    rows = budget_service.unplanned(today=ANCHOR)
    categories = [r['category'] for r in rows]

    assert 'Rent' not in categories, 'it has a budget'
    assert 'Groceries' in categories and 'Dining' in categories
    assert rows[0]['spent'] >= rows[-1]['spent'], 'largest first'


def test_money_moved_between_your_own_accounts_is_never_offered_a_budget(app,
                                                                        post):
    """A transfer is not spending, and a ceiling on one is nonsense.

    Excluded by the household's own category wording via
    `recurring.is_excluded_category`, which is the same rule the recurring
    detector applies — so a household that files these under 'Transfers' and one
    that files them under 'Internal Transfer' both get the same answer.
    """
    post(date(2026, 8, 3), 'To savings', -900.00, 'Transfer')
    post(date(2026, 8, 3), 'Dividend', -500.00, 'Investments')
    post(date(2026, 8, 4), 'Corner shop', -120.00, 'Groceries')

    categories = [r['category'] for r in budget_service.unplanned(today=ANCHOR)]
    assert categories == ['Groceries']


def test_the_filing_gap_is_never_offered_a_budget(app, post):
    """`Uncategorized` is `Transaction.category`'s default, not a name anybody
    chose. A ceiling on it would be a budget for unsorted rows, and it moves the
    moment they are sorted — taking the limit's meaning with it."""
    post(date(2026, 8, 3), 'Unknown charge', -300.00, 'Uncategorized')
    post(date(2026, 8, 4), 'Corner shop', -120.00, 'Groceries')

    categories = [r['category'] for r in budget_service.unplanned(today=ANCHOR)]
    assert categories == ['Groceries']


def test_pocket_change_is_not_called_unplanned_spending(app, post):
    """A page that asks somebody to budget $4 is a page they close."""
    post(date(2026, 8, 3), 'Parking meter', -4.00, 'Parking')
    post(date(2026, 8, 4), 'Corner shop', -120.00, 'Groceries')

    categories = [r['category'] for r in budget_service.unplanned(today=ANCHOR)]
    assert categories == ['Groceries']


def test_every_unplanned_row_carries_a_limit_somebody_can_accept(app, household):
    """The gap between "you should budget this" and a budget existing is a form.

    A category with history is priced from its median; one seen for the first
    time still gets a number rather than an empty box.
    """
    rows = {r['category']: r for r in budget_service.unplanned(today=ANCHOR)}

    assert rows['Groceries']['suggested'] > 0
    assert rows['Groceries']['months_seen'] >= 2


# ── Suggestions drawn from the household's own months ───────────────────────

def test_a_suggested_limit_is_never_below_a_normal_month(app, household):
    """Rounded up, always.

    A ceiling below what this household spends every month is one they break in
    the first week, and the first broken budget is where people decide
    budgeting is not for them.
    """
    hint = budget_service.suggested_limits(today=ANCHOR)['Groceries']

    assert hint['median'] == 418.00
    assert hint['suggested'] == 420.00, 'rounded up to the next $10'
    assert hint['suggested'] >= hint['median']


def test_suggestions_ignore_the_month_in_progress(app, household, post):
    """A limit suggested on the 3rd from three days of data has no evidence.

    August is deliberately loaded with a figure nothing else in the history
    supports; if the partial month were included the median would move.
    """
    post(date(2026, 8, 9), 'Panic shop', -3000.00, 'Groceries')

    hint = budget_service.suggested_limits(today=ANCHOR)['Groceries']
    assert hint['median'] == 418.00
    assert hint['highest'] == 418.00


def test_one_month_of_a_category_is_an_anecdote_not_a_budget(app, post):
    """Budgeting a one-off sofa as a monthly ceiling teaches somebody the
    suggestions are not worth reading."""
    post(date(2026, 6, 4), 'Sofa', -1400.00, 'Furniture')
    post(date(2026, 6, 5), 'Whole Foods', -300.00, 'Groceries')
    post(date(2026, 7, 5), 'Whole Foods', -320.00, 'Groceries')

    suggestions = budget_service.suggested_limits(today=ANCHOR)
    assert 'Furniture' not in suggestions
    assert 'Groceries' in suggestions


def test_the_balanced_frame_needs_income_before_it_offers_a_shape(app, post):
    """50% of an unknown number is not advice."""
    post(date(2026, 6, 5), 'Whole Foods', -300.00, 'Groceries')
    post(date(2026, 7, 5), 'Whole Foods', -320.00, 'Groceries')

    frame = budget_service.balanced_frame(today=ANCHOR)
    assert frame['available'] is False
    assert 'income' in frame['reason']


def test_the_balanced_frame_prices_the_split_against_take_home(app, household):
    frame = budget_service.balanced_frame(today=ANCHOR)
    assert frame['available'] is True

    buckets = {b['bucket']: b for b in frame['buckets']}
    assert buckets['needs']['target'] == 2000.00, '50% of $4,000'
    assert buckets['savings']['target'] == 800.00

    # Rent is an obligation by the household's own wording; dining is not.
    assert 'Rent' in [c['category'] for c in buckets['needs']['categories']]
    assert 'Dining' in [c['category'] for c in buckets['wants']['categories']]


# ── The committed split ─────────────────────────────────────────────────────

def test_the_committed_split_is_absent_unless_a_caller_asks_for_it(app, budget):
    """Recurring detection walks the whole ledger, and `/api/v1/budgets` calls
    `status()` on every request. `None` is not `0.0`: the page renders the first
    as "not known" and would render the second as "no bills".
    """
    budget('Rent', 1500)

    unasked = budget_service.status(today=ANCHOR)['budgets'][0]
    assert unasked['committed'] is None and unasked['flexible'] is None

    asked = budget_service.status(today=ANCHOR, committed={'Rent': 1500.0}
                                  )['budgets'][0]
    assert asked['committed'] == 1500.0 and asked['flexible'] == 0.0


def test_committed_spending_never_exceeds_the_limit_it_sits_in(app, budget):
    """A $200 bill inside a $150 budget is a budgeting problem, not 133%
    committed — and a flexible remainder below zero would render as a bar
    pointing the wrong way."""
    budget('Phone', 150)

    row = budget_service.status(today=ANCHOR,
                                committed={'Phone': 200.0})['budgets'][0]
    assert row['committed'] == 150.0
    assert row['flexible'] == 0.0


# ── One answer to "am I over budget" ────────────────────────────────────────

def test_the_dashboard_reads_its_overruns_from_the_budget_service(app, post,
                                                                 budget):
    """The test that keeps the second implementation deleted.

    `dough/blueprints/core.py` used to band spend across whatever window the
    reader had selected, normalised into a monthly average. A household looking
    at "last 90 days" was told about a different set of budgets than the Budgets
    page showed, from different arithmetic, in the same words.
    """
    budget('Dining', 100)
    post(date(2026, 8, 5), 'Restaurant', -260.00, category='Dining')

    rows = budget_service.status(today=ANCHOR)['budgets']
    alerts = budget_service.alerts(today=ANCHOR, rows=rows)

    assert len(alerts) == 1
    assert alerts[0]['level'] == 'over'
    assert alerts[0]['pct'] == round(rows[0]['pct'])
    assert alerts[0]['limit'] == rows[0]['monthly_limit']


def test_a_budget_inside_its_limit_raises_no_alert(app, post, budget):
    budget('Dining', 1000)
    post(date(2026, 8, 5), 'Restaurant', -100.00, category='Dining')

    assert budget_service.alerts(today=ANCHOR) == []


# ── The page and the builder ────────────────────────────────────────────────

def test_the_page_renders_the_anchor_and_the_unplanned_money(client, household,
                                                             budget):
    budget('Rent', 1500)
    page = client.get('/budgets').get_data(as_text=True)

    assert 'Every dollar coming in' in page
    assert 'Safe to spend' in page
    assert 'Not yet planned' in page


def test_applying_a_proposed_plan_sets_every_ticked_category_at_once(client,
                                                                    household):
    """One decision about several categories, so one commit.

    Committing per row would leave a half-applied plan behind if the eighth row
    failed, and would make "undo that" mean something different depending on
    where it stopped.
    """
    client.post('/budgets', data={
        'action': 'apply',
        'pick': ['Groceries', 'Dining'],
        'limit_Groceries': '425.00',
        'limit_Dining': '160.00',
    }, follow_redirects=True)

    saved = {b.category: float(b.monthly_limit)
             for b in budget_service.list_budgets()}
    assert saved == {'Groceries': 425.00, 'Dining': 160.00}


def test_an_unticked_row_is_not_budgeted(client, household):
    """Clearing the checkbox is how somebody says "not this one", and a posted
    amount for a row they unticked must not override that."""
    client.post('/budgets', data={
        'action': 'apply',
        'pick': ['Groceries'],
        'limit_Groceries': '425.00',
        'limit_Dining': '160.00',
    }, follow_redirects=True)

    assert [b.category for b in budget_service.list_budgets()] == ['Groceries']


def test_applying_a_plan_over_an_existing_budget_moves_the_number(client,
                                                                 household,
                                                                 budget):
    budget('Groceries', 300)

    client.post('/budgets', data={'action': 'apply', 'pick': ['Groceries'],
                                  'limit_Groceries': '425.00'},
                follow_redirects=True)

    rows = budget_service.list_budgets()
    assert len(rows) == 1, 'an upsert, not a duplicate'
    assert float(rows[0].monthly_limit) == 425.00


def test_the_builder_never_proposes_a_category_that_is_already_budgeted(client,
                                                                       household,
                                                                       budget):
    """Every row in the builder ships ticked.

    Leaving a budgeted category in the list would mean one click on "Save this
    plan" replaced a limit somebody chose with Dough's suggestion for it — the
    page overwriting a decision while appearing to make a new one.
    """
    budget('Groceries', 300)
    page = client.get('/budgets').get_data(as_text=True)

    assert 'name="limit_Dining"' in page, 'Dining has no budget and is proposed'
    assert 'name="limit_Groceries"' not in page

    # And the limit that was set survives a page render.
    assert float(budget_service.list_budgets()[0].monthly_limit) == 300.00


def test_a_cleared_amount_does_not_discard_the_rows_beside_it(client, household):
    """One bad box must not throw away the eleven good ones somebody just
    reviewed."""
    client.post('/budgets', data={
        'action': 'apply',
        'pick': ['Groceries', 'Dining'],
        'limit_Groceries': '',
        'limit_Dining': '160.00',
    }, follow_redirects=True)

    assert [b.category for b in budget_service.list_budgets()] == ['Dining']


# ── The edges ───────────────────────────────────────────────────────────────

def test_a_household_with_no_ledger_at_all_renders(client, app):
    """Every figure on this page is a ratio of something, and a brand-new
    household has zeroes in all the denominators."""
    page = client.get('/budgets')
    assert page.status_code == 200

    plan = budget_service.plan(today=ANCHOR)
    assert plan['basis'] == 0.0 and plan['basis_source'] == 'none'
    assert plan['planned_pct'] is None and plan['unallocated'] is None
    assert plan['over_committed'] is False
    assert budget_service.suggested_limits(today=ANCHOR) == {}


def test_a_budget_of_zero_is_not_a_division(app, post, budget):
    """`monthly_limit` has a minimum of zero, not a minimum of one cent."""
    budget('Dining', 0)
    post(date(2026, 8, 5), 'Restaurant', -50.00, category='Dining')

    row = budget_service.status(today=ANCHOR)['budgets'][0]
    assert row['pct'] == 0.0
    assert row['projected_pct'] == 0.0
    assert budget_service.alerts(today=ANCHOR) == [], 'no limit, nothing to breach'


def test_income_with_no_budgets_still_reports_the_whole_take_home_as_free(app,
                                                                         household):
    plan = budget_service.plan(today=ANCHOR)
    assert plan['planned'] == 0.0
    assert plan['unallocated'] == plan['basis']
