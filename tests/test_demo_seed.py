"""The demo household is generated, complete, and points away from real data.

Three things are worth testing here and they fail in different ways:

1. **The guard.** `seed_demo_household` deletes everything it is pointed at.
   The test that it refuses a non-demo household is the most important one in
   this file, and it is written first for that reason.
2. **Every page renders with content.** The demo exists to be *looked at*, so
   the check is a real GET of every route in the navigation — not an assertion
   about row counts, which would pass just as happily for data that renders as
   a page full of empty states.
3. **The figures agree with each other.** Net worth is reachable three ways in
   this application (account balances, holdings, and the snapshot series) and a
   demo where they disagree is worse than no demo.
"""

from datetime import date

import pytest

from dough.services import demo_seed
from dough.tenancy import unscoped
from models import (AppUser, Budget, FinancialAccount, Goal, Holding,
                    PortfolioSnapshotRow, Transaction, db)

#: Every destination in the left rail and the overflow menu that renders
#: household data. The demo is only finished when all of them have something
#: to show.
#:
#: `/settings` and `/household` are deliberately absent: both blueprints are
#: registered only when AUTH_ENABLED (see the comment in templates/base.html),
#: so they 404 under the suite's auth-off config. They are also account
#: administration rather than financial data, so the seeder has nothing to put
#: on them.
NAV_ROUTES = ['/', '/transactions', '/insights', '/budgets', '/goals',
              '/investments', '/recurring', '/anomalies', '/rules',
              '/connections', '/sync-history', '/upload']


@pytest.fixture()
def demo_household(app):
    """The default household, owned by a demo account so the guard admits it."""
    household_id = app.config['DEFAULT_HOUSEHOLD_ID']
    with unscoped():
        db.session.add(AppUser(username='RankParsley', password_hash='x',
                               household_id=household_id, role='owner'))
        db.session.commit()
    return household_id


@pytest.fixture()
def seeded(app, demo_household):
    demo_seed.seed_demo_household(demo_household)
    return demo_household


# -- The guard --------------------------------------------------------------

def test_refuses_a_household_owned_by_a_real_account(app):
    """The one that matters: a real username means nothing is deleted."""
    household_id = app.config['DEFAULT_HOUSEHOLD_ID']
    with unscoped():
        db.session.add(AppUser(username='spagnotta11', password_hash='x',
                               household_id=household_id, role='owner'))
        db.session.commit()
    db.session.add(Transaction(account_name='Checking', date=date(2026, 1, 5),
                               description='REAL ROW', amount=-10,
                               category='Food'))
    db.session.commit()

    with pytest.raises(demo_seed.NotADemoHousehold):
        demo_seed.seed_demo_household(household_id)

    # The refusal happens before any delete, which is the whole point.
    assert Transaction.query.filter_by(description='REAL ROW').count() == 1


def test_refuses_a_household_with_no_users_at_all(app):
    """An empty household cannot be confirmed as the demo one, so it is not."""
    with pytest.raises(demo_seed.NotADemoHousehold):
        demo_seed.seed_demo_household(app.config['DEFAULT_HOUSEHOLD_ID'])


def test_refuses_a_household_that_does_not_exist(app):
    with pytest.raises(demo_seed.NotADemoHousehold):
        demo_seed.seed_demo_household(99999)


def test_a_real_member_disqualifies_a_household_the_demo_account_is_also_in(app):
    """Every user must be a demo account, not merely one of them."""
    household_id = app.config['DEFAULT_HOUSEHOLD_ID']
    with unscoped():
        db.session.add(AppUser(username='RankParsley', password_hash='x',
                               household_id=household_id, role='owner'))
        db.session.add(AppUser(username='a-real-person', password_hash='x',
                               household_id=household_id, role='member'))
        db.session.commit()
    with pytest.raises(demo_seed.NotADemoHousehold):
        demo_seed.seed_demo_household(household_id)


# -- Every page renders with something on it --------------------------------

@pytest.mark.parametrize('route', NAV_ROUTES)
def test_every_navigation_route_renders(client, seeded, route):
    response = client.get(route)
    assert response.status_code == 200, f'{route} returned {response.status_code}'


@pytest.mark.parametrize('route,needle', [
    ('/transactions', b'SUNRISE MORTGAGE SERVICING'),
    ('/budgets', b'Groceries'),
    ('/goals', b'Emergency fund'),
    ('/investments', b'VTI'),
    ('/recurring', b'NETFLIX.COM'),
    ('/rules', b'whole foods'),
    ('/connections', b'Vanguard'),
])
def test_pages_show_generated_content(client, seeded, route, needle):
    """Not just a 200 - the generated rows are actually on the page.

    A page whose query returned nothing still renders, and still returns 200,
    and is exactly the failure this file exists to catch.
    """
    assert needle in client.get(route).data


# -- The data has the shape the derived views need --------------------------

def test_history_is_long_enough_for_the_twelve_month_baselines(seeded):
    dates = [t.date for t in Transaction.query.all()]
    span_days = (max(dates) - min(dates)).days
    assert span_days > 660, 'less than ~22 months of history'


def test_recurring_detection_finds_the_bills_and_the_subscriptions(seeded):
    from dough.services import recurring_service
    detected = recurring_service.detect_recurring_summary()
    bills = {item['description'] for item in detected['bills']}
    subs = {item['description'] for item in detected['subscriptions']}
    assert 'SUNRISE MORTGAGE SERVICING' in bills
    assert 'NELNET STUDENT LOAN SVC' in bills
    assert 'NETFLIX.COM' in subs
    assert 'SPOTIFY USA' in subs


def test_the_planted_anomalies_are_all_discovered(seeded):
    """Each of the three is planted deliberately; see `_plant_anomalies`."""
    from dough.services import anomalies
    found = anomalies.detect(limit=200)
    summaries = ' | '.join(f['summary'] for f in found)
    kinds = {f['kind'] for f in found}

    assert 'BEST BUY' in summaries and 'large_purchase' in kinds
    assert 'duplicate' in kinds
    assert any(f['kind'] == 'subscription_hike' and 'BEACON INTERNET' in f['summary']
               for f in found), 'the planted bill increase was not detected'


def test_the_anomaly_review_queue_is_not_empty(client, seeded):
    """`/anomalies` lists stored IsolationForest flags, not live findings.

    Two different things wear the word "anomaly" in this application, and only
    one of them is computed on the fly. The review queue reads
    `Transaction.anomaly_score == -1.0`, so it stays empty — while still
    answering 200 — unless the seeder writes that score.
    """
    flagged = Transaction.query.filter(Transaction.anomaly_score == -1.0).all()
    assert len(flagged) >= 4

    body = client.get('/anomalies').data
    assert b'BEST BUY #1142' in body


def test_the_annual_bonus_is_paid_once_a_year(seeded):
    """March usually has two paydays before the 15th, and the bonus is one.

    Paid twice, it puts an extra $3,400 into a single month, which distorts the
    income trend, the health score's stability factor and the missing-income
    baseline all at once — and looks like a data-entry mistake on the ledger.
    """
    bonuses = Transaction.query.filter_by(
        description='NORTHWIND LABS BONUS').all()
    years = [t.date.year for t in bonuses]
    assert len(years) == len(set(years)), f'bonus paid twice in {years}'


def test_trends_and_health_have_enough_to_measure(seeded):
    from dough.services import health, trends
    assert len(trends.category_trends()) >= 8
    assert trends.merchant_trends()
    # A score at all means income and outgo were both measurable over 6 months.
    assert health.score()['score'] is not None


# -- The figures agree ------------------------------------------------------

def test_account_balances_equal_the_sum_of_their_holdings(seeded):
    for account in FinancialAccount.query.filter(
            FinancialAccount.account_type.in_(('brokerage', 'crypto'))).all():
        held = sum(float(h.current_value) for h in
                   Holding.query.filter_by(account_id=account.id).all())
        assert round(float(account.balance), 2) == round(held, 2), account.name


def test_the_newest_snapshot_matches_todays_net_worth(seeded):
    """The chart's last point and the tiles beside it are one number."""
    from dough.services import networth
    latest = (PortfolioSnapshotRow.query
              .order_by(PortfolioSnapshotRow.snapshot_date.desc()).first())
    assert round(float(latest.net_worth), 2) == \
        round(networth.compute_net_worth()['net_worth'], 2)


def test_goal_contributions_sum_to_the_amount_saved(seeded):
    for goal in Goal.query.all():
        contributed = sum(float(c.amount) for c in goal.contributions)
        assert round(contributed, 2) == round(float(goal.saved_amount), 2), goal.name


def test_two_years_of_daily_snapshots(seeded):
    assert PortfolioSnapshotRow.query.count() == demo_seed.SNAPSHOT_DAYS


# -- Re-running -------------------------------------------------------------

def test_reseeding_replaces_rather_than_appends(seeded):
    """The demo is meant to be reset; a seeder that appended would double it."""
    before = (Transaction.query.count(), Budget.query.count(),
              Holding.query.count(), PortfolioSnapshotRow.query.count())
    demo_seed.seed_demo_household(seeded)
    after = (Transaction.query.count(), Budget.query.count(),
             Holding.query.count(), PortfolioSnapshotRow.query.count())
    assert before == after


@pytest.mark.parametrize('today', [
    date(2026, 8, 1),    # the 1st: the month's own bills are barely due yet
    date(2026, 1, 31),   # a 31-day month, so day-of-month clamping is exercised
    date(2028, 2, 29),   # a leap day
    date(2026, 12, 31),  # a year boundary
])
def test_nothing_is_dated_in_the_future(app, demo_household, today):
    """Every date is derived from `today`, so the bugs hide on awkward days.

    A row dated after the anchor is not cosmetic: a future transaction skews
    every window the analytics package computes, and a future goal contribution
    reads as momentum that has not happened.
    """
    from models import GoalContribution

    # Six months rather than the full 24: this test is about how dates are
    # derived from the anchor, and every date rule runs per month, so a shorter
    # window exercises all of them for a quarter of the generation cost.
    demo_seed.seed_demo_household(demo_household, today=today,
                                  history_months=6)

    assert max(t.date for t in Transaction.query.all()) <= today
    assert max(c.occurred_on for c in GoalContribution.query.all()) <= today
    assert max(s.snapshot_date
               for s in PortfolioSnapshotRow.query.all()) <= today


def test_the_same_seed_on_the_same_day_gives_the_same_household(seeded):
    """Reproducibility, so a demo bug can be reproduced and screenshots match."""
    first = sorted((t.date, t.description, float(t.amount))
                   for t in Transaction.query.all())
    demo_seed.seed_demo_household(seeded)
    second = sorted((t.date, t.description, float(t.amount))
                    for t in Transaction.query.all())
    assert first == second
