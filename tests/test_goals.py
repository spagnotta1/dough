"""`dough/services/goals.py` — Feature 10, and the only Phase 11 table.

Progress and remaining are subtraction and get light coverage. Momentum and
projection get the weight, because they are the two figures that can be
confidently wrong: a lifetime average keeps projecting a completion date for a
goal nobody has funded in six months, and a projection from two deposits reads
as a plan.

The negatives matter as much as the positives here — `None` for "no rate to
project from" rather than a date eleven years out, and no completion date at all
from a single contribution.
"""

from datetime import date
from decimal import Decimal

import pytest

from dough.services import goals

TODAY = date(2026, 8, 15)


@pytest.fixture()
def goal(app):
    return goals.create_goal(name='Emergency fund', target_amount=6000,
                             kind='emergency_fund', monthly_target=500)


@pytest.fixture()
def contribute():
    """Post a contribution `months_ago` before TODAY."""
    def _contribute(goal_id, amount, months_ago=0, day=10):
        year, month = TODAY.year, TODAY.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        return goals.contribute(goal_id, amount,
                                occurred_on=date(year, month, day))
    return _contribute


# ── Creating and editing ────────────────────────────────────────────────────

def test_a_goal_starts_empty(app, goal):
    described = goals.describe(goal, today=TODAY)

    assert described['target_amount'] == 6000.0
    assert described['saved_amount'] == 0.0
    assert described['remaining'] == 6000.0
    assert described['progress_pct'] == 0.0
    assert described['is_complete'] is False


def test_a_goal_needs_a_name_and_a_positive_target(app):
    with pytest.raises(ValueError, match='needs a name'):
        goals.create_goal(name='  ', target_amount=100)
    with pytest.raises(ValueError, match='more than zero'):
        goals.create_goal(name='Nope', target_amount=0)
    with pytest.raises(ValueError, match='not a number'):
        goals.create_goal(name='Nope', target_amount='soon')


def test_two_goals_cannot_share_a_name(app, goal):
    with pytest.raises(ValueError, match='already have a goal'):
        goals.create_goal(name='emergency FUND', target_amount=100)


def test_an_unknown_kind_degrades_to_custom(app):
    created = goals.create_goal(name='Boat', target_amount=100, kind='yacht')
    assert created.kind == 'custom'


def test_editing_never_moves_the_saved_amount(app, goal, contribute):
    """Progress moves only through contribute(), so history stays complete."""
    contribute(goal.id, 400, months_ago=1)
    goals.update_goal(goal.id, name='Rainy day', target_amount=8000)

    described = goals.describe(goal, today=TODAY)
    assert described['name'] == 'Rainy day'
    assert described['saved_amount'] == 400.0


# ── Contributions ───────────────────────────────────────────────────────────

def test_contributing_moves_progress(app, goal, contribute):
    contribute(goal.id, 1500, months_ago=1)

    described = goals.describe(goal, today=TODAY)
    assert described['saved_amount'] == 1500.0
    assert described['remaining'] == 4500.0
    assert described['progress_pct'] == 25.0


def test_a_withdrawal_is_recorded_rather_than_refused(app, goal, contribute):
    """Money comes back out of a holiday fund; refusing it loses the record."""
    contribute(goal.id, 1000, months_ago=2)
    contribute(goal.id, -300, months_ago=1)

    assert goals.describe(goal, today=TODAY)['saved_amount'] == 700.0
    assert len(goals.contributions(goal.id)) == 2


def test_saved_never_goes_negative(app, goal, contribute):
    """A negative progress bar renders as nonsense rather than as a slip."""
    contribute(goal.id, 100, months_ago=1)
    contribute(goal.id, -500, months_ago=0)
    assert goals.describe(goal, today=TODAY)['saved_amount'] == 0.0


def test_a_zero_contribution_is_refused(app, goal):
    with pytest.raises(ValueError, match='needs an amount'):
        goals.contribute(goal.id, 0)


def test_reaching_the_target_marks_the_goal_achieved(app, goal, contribute):
    contribute(goal.id, 6000, months_ago=1)

    described = goals.describe(goal, today=TODAY)
    assert described['is_complete'] is True
    assert described['status'] == 'achieved'
    assert described['progress_pct'] == 100.0


def test_overfunding_caps_the_bar_but_not_the_figure(app, goal, contribute):
    contribute(goal.id, 9000, months_ago=1)

    described = goals.describe(goal, today=TODAY)
    assert described['progress_pct'] == 100.0
    assert described['saved_amount'] == 9000.0


def test_raising_the_target_reopens_an_achieved_goal(app, goal, contribute):
    """Otherwise it quietly stops tracking a goal the user just extended."""
    contribute(goal.id, 6000, months_ago=1)
    goals.update_goal(goal.id, target_amount=10000)

    described = goals.describe(goal, today=TODAY)
    assert described['status'] == 'active'
    assert described['is_complete'] is False


# ── Momentum ────────────────────────────────────────────────────────────────

def test_momentum_is_the_recent_rate(app, goal, contribute):
    for months_ago in (0, 1, 2):
        contribute(goal.id, 300, months_ago=months_ago)

    found = goals.momentum(goal, today=TODAY)
    assert found['contributed'] == 900.0
    assert found['per_month'] == 300.0
    assert found['months_with_a_contribution'] == 3
    assert found['stalled'] is False


def test_an_abandoned_goal_has_no_momentum_despite_a_healthy_total(app, goal,
                                                                   contribute):
    """The reason momentum is not the lifetime average.

    Funded hard six months ago and untouched since: the average still looks
    fine and would keep projecting a completion date that is not coming.
    """
    for months_ago in (6, 7, 8):
        contribute(goal.id, 1000, months_ago=months_ago)

    found = goals.momentum(goal, today=TODAY)
    assert found['stalled'] is True
    assert found['per_month'] == 0.0
    assert goals.describe(goal, today=TODAY)['saved_amount'] == 3000.0


def test_momentum_records_the_last_contribution(app, goal, contribute):
    contribute(goal.id, 200, months_ago=1, day=7)
    assert goals.momentum(goal, today=TODAY)['last_contribution'] == '2026-07-07'


# ── Projection ──────────────────────────────────────────────────────────────

def test_a_steady_goal_projects_a_completion_date(app, goal, contribute):
    for months_ago in (0, 1, 2):
        contribute(goal.id, 500, months_ago=months_ago)

    described = goals.describe(goal, today=TODAY)
    forecast = described['projection']

    assert forecast['date'] is not None
    assert forecast['months'] == 9.0            # 4,500 left at 500/month
    assert forecast['confidence'] == 'high'
    assert '$500 a month' in forecast['basis']


def test_a_stalled_goal_projects_nothing(app, goal, contribute):
    """"At this rate, never" is the true statement. Not a date in 2041."""
    contribute(goal.id, 1000, months_ago=8)

    forecast = goals.describe(goal, today=TODAY)['projection']
    assert forecast['date'] is None
    assert forecast['months'] is None
    assert forecast['confidence'] == 'none'
    assert 'no contributions' in forecast['basis']


def test_one_contribution_is_not_enough_to_project(app, goal, contribute):
    """Two points make a line and not a habit."""
    contribute(goal.id, 500, months_ago=0)

    forecast = goals.describe(goal, today=TODAY)['projection']
    assert forecast['date'] is None
    assert 'not enough contribution history' in forecast['basis']


def test_a_glacial_pace_reports_beyond_the_horizon_not_a_date(app, contribute):
    """A date eleven years out is arithmetic, not information."""
    slow = goals.create_goal(name='House', target_amount=500000)
    for months_ago in (0, 1, 2):
        contribute(slow.id, 100, months_ago=months_ago)

    forecast = goals.describe(slow, today=TODAY)['projection']
    assert forecast['date'] is None
    assert forecast['months'] > goals.MAX_PROJECTION_MONTHS
    assert 'years' in forecast['basis']


def test_an_achieved_goal_projects_complete(app, goal, contribute):
    contribute(goal.id, 6000, months_ago=1)
    forecast = goals.describe(goal, today=TODAY)['projection']
    assert forecast['complete'] is True
    assert forecast['confidence'] == 'certain'


def test_patchy_contributions_lower_the_confidence(app, goal, contribute):
    contribute(goal.id, 500, months_ago=0)
    contribute(goal.id, 500, months_ago=2)          # nothing last month

    forecast = goals.describe(goal, today=TODAY)['projection']
    assert forecast['confidence'] == 'low'


# ── Pace ────────────────────────────────────────────────────────────────────

def test_pace_compares_against_the_households_own_plan(app, goal, contribute):
    """Turns "you have saved $900" into "you are $200 a month behind"."""
    for months_ago in (0, 1, 2):
        contribute(goal.id, 300, months_ago=months_ago)

    pace = goals.describe(goal, today=TODAY)['pace']['vs_plan']
    assert pace['planned'] == 500.0
    assert pace['actual'] == 300.0
    assert pace['difference'] == -200.0
    assert pace['on_plan'] is False


def test_pace_reports_what_a_target_date_requires(app, contribute):
    dated = goals.create_goal(name='Wedding', target_amount=12000,
                              target_date=date(2027, 8, 15))
    for months_ago in (0, 1, 2):
        contribute(dated.id, 200, months_ago=months_ago)

    pace = goals.describe(dated, today=TODAY)['pace']
    assert pace['vs_target_date']['days_left'] == 365
    assert pace['required_per_month'] > 200
    assert pace['vs_target_date']['on_track'] is False


def test_a_passed_deadline_is_overdue_and_has_no_required_rate(app, contribute):
    """No rate reaches a deadline already behind you."""
    late = goals.create_goal(name='Late', target_amount=1000,
                             target_date=date(2026, 1, 1))
    contribute(late.id, 100, months_ago=1)

    pace = goals.describe(late, today=TODAY)['pace']
    assert pace['vs_target_date']['overdue'] is True
    assert pace['vs_target_date']['on_track'] is None
    assert pace['required_per_month'] is None


def test_a_goal_without_a_date_or_plan_has_no_pace_comparisons(app):
    bare = goals.create_goal(name='Someday', target_amount=1000)
    pace = goals.describe(bare, today=TODAY)['pace']

    assert pace['vs_plan'] is None
    assert pace['vs_target_date'] is None


# ── Listing and summary ─────────────────────────────────────────────────────

def test_dated_goals_are_listed_before_undated_ones(app):
    goals.create_goal(name='Someday', target_amount=1000)
    goals.create_goal(name='June', target_amount=1000,
                      target_date=date(2027, 6, 1))
    goals.create_goal(name='March', target_amount=1000,
                      target_date=date(2027, 3, 1))

    listed = [g['name'] for g in goals.list_goals(today=TODAY)]
    assert listed == ['March', 'June', 'Someday']


def test_the_summary_totals_the_household(app, goal, contribute):
    contribute(goal.id, 1000, months_ago=1)
    goals.create_goal(name='Holiday', target_amount=2000, monthly_target=100)

    found = goals.summary(today=TODAY)
    assert found['count'] == 2
    assert found['total_target'] == 8000.0
    assert found['total_saved'] == 1000.0
    assert found['total_remaining'] == 7000.0
    assert found['monthly_commitment'] == 600.0


def test_the_summary_names_stalled_and_behind_plan_goals(app, goal, contribute):
    contribute(goal.id, 100, months_ago=9)          # long abandoned

    found = goals.summary(today=TODAY)
    assert 'Emergency fund' in found['stalled']
    assert 'Emergency fund' in found['behind_plan']


def test_achieved_goals_drop_out_of_the_active_list(app, goal, contribute):
    contribute(goal.id, 6000, months_ago=1)
    assert goals.list_goals(status='active', today=TODAY) == []
    assert len(goals.list_goals(status=None, today=TODAY)) == 1


def test_deleting_a_goal_takes_its_contributions(app, goal, contribute):
    from models import GoalContribution

    contribute(goal.id, 500, months_ago=1)
    goals.delete_goal(goal.id)

    assert GoalContribution.query.count() == 0
    assert goals.list_goals(status=None, today=TODAY) == []


# ── Tenancy ─────────────────────────────────────────────────────────────────

def test_goals_never_cross_a_household(tmp_path):
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'goals.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False})

    from dough.tenancy import tenant_scope, unscoped
    from models import Household, db

    with application.app_context():
        with unscoped():
            a = Household(name='A', plaid_user_id='g-a')
            b = Household(name='B', plaid_user_id='g-b')
            db.session.add_all([a, b])
            db.session.commit()
            a_id, b_id = a.id, b.id

        with tenant_scope(a_id):
            created = goals.create_goal(name='Emergency fund',
                                        target_amount=6000)
            goals.contribute(created.id, 1000, occurred_on=TODAY)

        with tenant_scope(b_id):
            assert goals.list_goals(status=None, today=TODAY) == []
            # The same name must be available -- the unique index leads with
            # the household.
            mine = goals.create_goal(name='Emergency fund', target_amount=999)
            assert goals.describe(mine, today=TODAY)['saved_amount'] == 0.0

        with tenant_scope(a_id):
            listed = goals.list_goals(status=None, today=TODAY)
            assert len(listed) == 1
            assert listed[0]['saved_amount'] == 1000.0

        db.session.remove()
    scheduler_module._scheduler = None


def test_another_households_goal_cannot_be_reached_by_id(tmp_path):
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'goals2.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False})

    from dough.tenancy import tenant_scope, unscoped
    from models import Household, db

    with application.app_context():
        with unscoped():
            a = Household(name='A', plaid_user_id='gx-a')
            b = Household(name='B', plaid_user_id='gx-b')
            db.session.add_all([a, b])
            db.session.commit()
            a_id, b_id = a.id, b.id

        with tenant_scope(a_id):
            victim = goals.create_goal(name='House', target_amount=50000).id

        with tenant_scope(b_id):
            for attempt in (lambda: goals.contribute(victim, 100),
                            lambda: goals.update_goal(victim, name='Mine'),
                            lambda: goals.delete_goal(victim)):
                with pytest.raises(ValueError, match='does not exist'):
                    attempt()

        with tenant_scope(a_id):
            assert goals.describe(
                __import__('models').Goal.query.get(victim),
                today=TODAY)['name'] == 'House'

        db.session.remove()
    scheduler_module._scheduler = None


# ── The page ────────────────────────────────────────────────────────────────

@pytest.fixture()
def page(tmp_path):
    """A signed-in-equivalent app for exercising the routes."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'goalpage.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
        'AUTH_ENABLED': False})

    from dough.tenancy import tenant_scope
    from models import db

    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


def test_the_page_renders_with_no_goals(page):
    body = page.test_client().get('/goals').get_data(as_text=True)
    assert 'No goals yet' in body
    assert 'Add a goal' in body


def test_a_goal_can_be_added_from_the_page(page):
    response = page.test_client().post('/goals/new', data={
        'name': 'Holiday', 'target_amount': '2000', 'kind': 'vacation'},
        follow_redirects=True)

    assert response.status_code == 200
    assert 'Holiday' in response.get_data(as_text=True)
    assert goals.list_goals(today=TODAY)[0]['name'] == 'Holiday'


def test_a_bad_goal_shows_the_reason_rather_than_a_500(page):
    response = page.test_client().post('/goals/new', data={
        'name': '', 'target_amount': '2000'}, follow_redirects=True)

    assert response.status_code == 200
    assert 'needs a name' in response.get_data(as_text=True)


def test_a_duplicate_name_is_reported_to_the_user(page):
    client = page.test_client()
    client.post('/goals/new', data={'name': 'Holiday', 'target_amount': '2000'},
                follow_redirects=True)
    response = client.post('/goals/new',
                           data={'name': 'Holiday', 'target_amount': '50'},
                           follow_redirects=True)

    assert 'already have a goal' in response.get_data(as_text=True)


def test_contributing_from_the_page_moves_progress(page):
    created = goals.create_goal(name='Holiday', target_amount=2000)
    page.test_client().post(f'/goals/{created.id}/contribute',
                            data={'amount': '250'}, follow_redirects=True)

    assert goals.describe(created, today=TODAY)['saved_amount'] == 250.0


def test_deleting_from_the_page_removes_the_goal(page):
    created = goals.create_goal(name='Holiday', target_amount=2000)
    page.test_client().post(f'/goals/{created.id}/delete', follow_redirects=True)

    assert goals.list_goals(status=None, today=TODAY) == []


def test_the_page_shows_the_projection_basis_not_a_bare_date(page):
    """A date with no stated basis reads as a promise."""
    created = goals.create_goal(name='Holiday', target_amount=2000)
    for months_ago in (0, 1, 2):
        year, month = TODAY.year, TODAY.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        goals.contribute(created.id, 200, occurred_on=date(year, month, 10))

    body = page.test_client().get('/goals').get_data(as_text=True)
    assert 'Projected finish' in body
    assert 'a month over the last' in body or 'confidence' in body


def test_goals_is_in_the_primary_nav(page):
    import re
    body = page.test_client().get('/goals').get_data(as_text=True)
    nav = re.search(r'<div id="primary-nav">(.*?)</div>', body, re.DOTALL).group(1)
    assert '/goals' in nav
