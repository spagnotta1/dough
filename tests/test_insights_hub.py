"""`/insights` — the consolidated hub [Phase 11A].

Two things are worth testing about a page that mostly composes services: that
it renders every section without a template error (a Jinja typo in a `{% if %}`
branch is invisible until the branch is taken), and that the consolidation did
not quietly remove anything a user relied on.

The second is what most of this file is. Retiring a nav entry is a reversible
product decision; losing the anomaly review workflow, or leaving a bookmark
404ing, is not.
"""

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def app_no_auth(tmp_path):
    """The hub, reachable without signing in, so these stay page tests."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    from dough.tenancy import tenant_scope
    from models import db

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'hub.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AUTH_ENABLED': False,
    })
    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


def _months_back(n):
    """The 1st of the month `n` months before today.

    The fixture is built relative to the real clock rather than to fixed 2026
    dates, because the route under test takes no anchor — it is the production
    call — and a hardcoded ledger drifts out of its lookback window as the
    calendar moves. The first version of this file used August 2026 and the
    trends section rendered empty the moment "today" was earlier in that month
    than the transactions were dated.
    """
    today = date.today()
    year, month = today.year, today.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


@pytest.fixture()
def populated(app_no_auth):
    """Enough history that every section on the page has something to render.

    Groceries climb steadily so the trend engine has a direction to report;
    everything else is level so it does not have several.
    """
    from models import Transaction, db

    for months_ago in range(6, -1, -1):
        when = _months_back(months_ago)
        db.session.add_all([
            Transaction(account_name='checking', date=when,
                        description='Payroll ACME', amount=Decimal('5000'),
                        category='Income'),
            Transaction(account_name='checking', date=when,
                        description='Whole Foods',
                        amount=Decimal(str(-(300 + (6 - months_ago) * 60))),
                        category='Groceries'),
            Transaction(account_name='checking', date=when,
                        description='Netflix', amount=Decimal('-15.99'),
                        category='Streaming'),
        ])
    db.session.add(Transaction(
        account_name='checking', date=_months_back(0),
        description='Odd charge', amount=Decimal('-1200'),
        category='Shopping', anomaly_score=-1.0))
    db.session.commit()
    return app_no_auth


def _get(app, path='/insights'):
    return app.test_client().get(path)


# ── It renders ──────────────────────────────────────────────────────────────

def test_the_hub_renders_every_section(populated):
    body = _get(populated).get_data(as_text=True)

    assert 'Financial health' in body
    assert 'What I noticed' in body
    assert 'Unusual activity' in body
    assert 'Spending trends' in body


def test_the_hub_renders_on_a_ledger_with_nothing_interesting(app_no_auth):
    """The empty branches are the ones a template typo hides in."""
    from models import Transaction, db

    for month in range(2, 9):
        db.session.add(Transaction(
            account_name='checking', date=date(2026, month, 1),
            description='Payroll ACME', amount=Decimal('5000'),
            category='Income'))
    db.session.commit()

    response = _get(app_no_auth)
    assert response.status_code == 200
    assert 'Nothing stands out' in response.get_data(as_text=True)


def test_an_empty_ledger_is_sent_to_upload_rather_than_an_empty_page(app_no_auth):
    response = _get(app_no_auth)
    assert response.status_code == 302
    assert '/upload' in response.headers['Location']


# ── The anomaly pane is collapsed, not removed ──────────────────────────────

def test_the_anomaly_pane_is_collapsed_by_default(populated):
    """The point of the consolidation: present, and not the whole page."""
    body = _get(populated).get_data(as_text=True)

    assert '<details class="ins-details"' in body
    # No `open` attribute on that element unless it was asked for.
    start = body.index('<details class="ins-details"')
    assert 'open' not in body[start:start + 60]


def test_the_pane_can_be_opened_by_link(populated):
    """So an insight elsewhere can deep-link straight to the review list."""
    body = _get(populated, '/insights?open=unusual').get_data(as_text=True)
    start = body.index('<details class="ins-details"')
    assert 'open' in body[start:start + 60]


def test_flagged_transactions_are_reviewable_from_the_hub(populated):
    body = _get(populated).get_data(as_text=True)
    assert 'Odd charge' in body
    assert 'dismissAnomaly(' in body


# ── Nothing was lost ────────────────────────────────────────────────────────

def test_the_old_pages_are_still_served(populated):
    """Retiring a nav entry must not 404 a bookmark."""
    assert _get(populated, '/anomalies').status_code == 200
    assert _get(populated, '/recurring').status_code == 200


def test_the_hub_links_to_both_pages_it_replaced_in_the_nav(populated):
    body = _get(populated).get_data(as_text=True)
    assert '/anomalies' in body
    assert '/recurring' in body


def test_the_dismiss_endpoints_still_work(populated):
    from models import Transaction

    flagged = Transaction.query.filter(Transaction.anomaly_score == -1.0).first()
    response = populated.test_client().post(f'/anomalies/{flagged.id}/dismiss')

    assert response.status_code == 200
    assert response.get_json()['success'] is True


def test_the_primary_nav_carries_insights_and_not_the_two_it_absorbed(populated):
    """The consolidation freed a slot, and Phase 11B spent it on Goals.

    The count went 7 -> 6 when Anomalies and Recurring folded into Insights,
    then back to 7 when Goals arrived. That is the intended trade rather than a
    regression: the same width now carries two destinations people visit
    (Insights, Goals) instead of two single-table pages they rarely did, and
    the nav is still only revealed at 1024px where seven links fit — see the
    comment above `#primary-nav` in base.html.

    What must stay true is the substance below: the two absorbed pages are not
    in the primary nav, and are still reachable.
    """
    import re

    body = _get(populated).get_data(as_text=True)
    nav = re.search(r'<div id="primary-nav">(.*?)</div>', body, re.DOTALL).group(1)

    assert '/insights' in nav
    assert '/goals' in nav
    assert '/anomalies' not in nav
    assert '/recurring' not in nav
    # Counted on the anchors, not on the class: "Ask Dough" carries
    # `nav-link nav-link--accent`, so counting the class name over-reports.
    assert nav.count('<a href=') == 7


def test_the_absorbed_pages_stay_reachable_from_the_touch_menu(populated):
    """A phone has no primary nav, so the links cannot only live in it."""
    import re

    body = _get(populated).get_data(as_text=True)
    menu = re.search(r'<div id="mobile-menu"[^>]*>(.*?)</div>\s*</div>',
                     body, re.DOTALL).group(1)

    assert '/insights' in menu
    assert '/anomalies' in menu
    assert '/recurring' in menu


# ── Figures come from the services ──────────────────────────────────────────

def test_the_hub_runs_the_anomaly_detector_only_once(populated):
    """Regression: it ran three times — for the list, the counts, and the insights.

    Each run is a full pass over a year of transactions. Counted rather than
    timed, so the guarantee does not evaporate on a fast machine.
    """
    from dough.services import anomalies

    calls = []
    original = anomalies.detect

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    anomalies.detect = counting
    try:
        assert _get(populated).status_code == 200
    finally:
        anomalies.detect = original

    assert len(calls) == 1, f'detector ran {len(calls)} times for one page'


def test_the_score_on_the_page_is_the_score_the_service_computed(populated):
    """One derivation. A page that recomputed would be free to disagree."""
    from dough.services import health

    expected = health.score()['score']
    body = _get(populated).get_data(as_text=True)

    assert f'<span class="ins-score__value">{expected}</span>' in body
