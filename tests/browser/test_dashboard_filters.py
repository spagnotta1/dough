"""The dashboard's filter bar, in a real engine.

`tests/test_dashboard_filters.py` proves the windows select the right rows.
None of that says whether the popover opens, whether Escape closes it, whether
a preset applies without a second click, or whether the bar stays inside a
phone — and those are the properties the refactor was actually for.

Two of them can only be checked here. The preset arithmetic lives in
`static/js/dashboard.js`, so "Last Month" meaning the whole of last month is a
statement about code no Python test executes; and "one Apply, one request" is a
claim about network traffic, which is a thing you have to watch to know.
"""

from datetime import date

import pytest

from .conftest import assert_no_horizontal_overflow, visit, wait_for_layout

PHONE = {'width': 390, 'height': 844}


def _dash(page):
    visit(page, '/')
    wait_for_layout(page)


def _open(page, trigger):
    page.click(trigger)
    page.wait_for_timeout(120)


def _label(page):
    """What the date pill currently reads."""
    return page.locator('#dateTrigger .dash-trigger__value').inner_text().strip()


def _params(page):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(page.url).query)


# ── What replaced the panel ─────────────────────────────────────────────────

def test_the_permanent_panel_is_gone_and_the_bar_is_short(signed_in):
    """The panel was ~180px of controls above the numbers, open by default and
    remembered across visits. The bar has to be a fraction of that or the
    refactor bought nothing."""
    page = signed_in
    _dash(page)

    assert page.locator('#dashFilterPanel').count() == 0
    assert page.locator('#dashFilters').is_visible()

    bar = page.locator('#dashFilterForm').bounding_box()
    assert bar['height'] < 60, f'the filter bar is {bar["height"]}px tall'

    # And nothing is disclosed until it is asked for.
    for menu in ('#dateMenu', '#accountMenu', '#moreMenu'):
        assert page.locator(menu).get_attribute('open') is None, \
            f'{menu} is open on arrival'


def test_the_bar_says_what_is_on_screen_without_being_opened(signed_in):
    page = signed_in
    _dash(page)

    assert _label(page) == 'This Month'
    assert page.locator('#accountTrigger .dash-trigger__value').inner_text().strip() \
        == 'All accounts'
    # The exact window, for the case a preset name cannot describe it.
    assert page.locator('#dashPeriodLabel').inner_text().strip()


# ── Applying ────────────────────────────────────────────────────────────────

def test_a_preset_applies_on_one_click_and_renames_the_control(signed_in):
    """No Apply, no Done. The old panel needed both, and it stayed open
    through them."""
    page = signed_in
    _dash(page)

    _open(page, '#dateTrigger')
    page.click('[data-preset="last_month"]')
    page.wait_for_function("() => location.search.includes('start_date=')",
                           timeout=5_000)

    params = _params(page)
    start = date.fromisoformat(params['start_date'][0])
    end = date.fromisoformat(params['end_date'][0])
    today = date.today()

    assert start.day == 1, 'Last Month did not start on the 1st'
    assert (start.year, start.month) == (
        (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12))
    assert end.replace(day=1) == start, 'Last Month ran past the end of the month'
    assert end.day >= 28, 'Last Month stopped before the end of the month'

    page.wait_for_timeout(200)
    assert _label(page) == 'Last Month'
    assert page.locator('.dash-chip', has_text='Last Month').count() == 1


def test_choosing_an_account_applies_without_an_apply_button(signed_in):
    page = signed_in
    _dash(page)

    _open(page, '#accountTrigger')
    page.check('#accountMenu input[value="Visa"]')
    page.wait_for_function("() => location.search.includes('account=Visa')",
                           timeout=5_000)
    page.wait_for_timeout(200)

    assert page.locator('#accountTrigger .dash-trigger__value').inner_text().strip() \
        == 'Visa'
    assert page.locator('.dash-chip', has_text='Visa').count() == 1


def test_the_advanced_panel_costs_one_request_however_many_boxes_are_ticked(signed_in):
    """The reason it has an Apply at all: a popover that refreshed the
    dashboard on every checkbox would refresh it three times to build a
    three-part filter."""
    page = signed_in
    _dash(page)

    loads = []
    page.on('request', lambda r: loads.append(r.url)
            if 'category=' in r.url else None)

    _open(page, '#moreTrigger')
    page.check('#moreMenu input[value="Groceries"]')
    page.check('#moreMenu input[value="Travel"]')
    page.wait_for_timeout(200)
    assert loads == [], f'ticking a box refreshed the dashboard: {loads}'

    page.click('#moreMenu button[type="submit"]')
    page.wait_for_function("() => location.search.includes('category=')",
                           timeout=5_000)
    page.wait_for_timeout(300)

    assert len(loads) == 1, f'Apply issued {len(loads)} requests: {loads}'
    assert page.locator('.dash-chip', has_text='Groceries').count() == 1
    assert page.locator('.dash-chip', has_text='Travel').count() == 1


# ── Removing ────────────────────────────────────────────────────────────────

def test_a_chip_removes_only_its_own_filter(signed_in):
    page = signed_in
    visit(page, '/?account=Visa&category=Travel')
    wait_for_layout(page)

    assert page.locator('.dash-chip', has_text='Visa').count() == 1
    page.locator('.dash-chip', has_text='Travel').locator('.dash-chip__x').click()
    page.wait_for_function("() => !location.search.includes('category=Travel')",
                           timeout=5_000)
    page.wait_for_timeout(200)

    assert page.locator('.dash-chip', has_text='Travel').count() == 0
    assert page.locator('.dash-chip', has_text='Visa').count() == 1, \
        'removing the category took the account with it'


def test_clear_all_appears_with_a_filter_and_restores_the_default(signed_in):
    page = signed_in
    _dash(page)
    assert page.locator('#dashClearAll').count() == 0

    visit(page, '/?account=Visa')
    assert page.locator('#dashClearAll').is_visible()

    page.click('#dashClearAll')
    page.wait_for_url(lambda url: '/clear_filters' not in url, timeout=10_000)
    wait_for_layout(page)

    assert page.locator('.dash-chip').count() == 0
    assert page.locator('#accountTrigger .dash-trigger__value').inner_text().strip() \
        == 'All accounts'


# ── Keyboard and dismissal ──────────────────────────────────────────────────

def test_a_menu_opens_from_the_keyboard(signed_in):
    page = signed_in
    _dash(page)

    page.focus('#dateTrigger')
    page.keyboard.press('Enter')
    page.wait_for_timeout(150)

    assert page.locator('#dateMenu').get_attribute('open') is not None
    assert page.locator('[data-preset="ytd"]').is_visible()


def test_escape_closes_an_open_menu_and_gives_focus_back(signed_in):
    page = signed_in
    _dash(page)

    _open(page, '#accountTrigger')
    assert page.locator('#accountMenu').get_attribute('open') is not None

    page.keyboard.press('Escape')
    page.wait_for_timeout(150)

    assert page.locator('#accountMenu').get_attribute('open') is None
    assert page.evaluate("document.activeElement.id") == 'accountTrigger', \
        'Escape closed the menu and dropped focus on the floor'


def test_clicking_the_page_closes_an_open_menu(signed_in):
    page = signed_in
    _dash(page)

    _open(page, '#dateTrigger')
    page.click('.dash-header__title')
    page.wait_for_timeout(150)

    assert page.locator('#dateMenu').get_attribute('open') is None


def test_only_one_menu_is_open_at_a_time(signed_in):
    """Two popovers overlapping each other is the failure mode of a bar built
    out of three independent disclosures."""
    page = signed_in
    _dash(page)

    _open(page, '#dateTrigger')
    _open(page, '#accountTrigger')

    assert page.locator('#dashFilters .dash-menu[open]').count() == 1


def test_the_calendar_stays_behind_the_custom_option(signed_in):
    """Two date inputs permanently on screen is what the bar exists to
    stop — but they still have to be reachable, and they still have to be
    submitted, which is why they are disclosed rather than conditional."""
    page = signed_in
    _dash(page)

    assert not page.locator('#start_date').is_visible()

    _open(page, '#dateTrigger')
    assert not page.locator('#start_date').is_visible(), \
        'the calendar is on screen before anybody asked for a custom range'

    page.click('#customRange summary')
    page.wait_for_timeout(150)
    assert page.locator('#start_date').is_visible()


def test_a_custom_range_applies_from_its_own_apply(signed_in):
    page = signed_in
    _dash(page)

    _open(page, '#dateTrigger')
    page.click('#customRange summary')
    page.fill('#start_date', '2026-03-01')
    page.fill('#end_date', '2026-03-31')
    page.click('#customRange button[type="submit"]')
    page.wait_for_function("() => location.search.includes('start_date=2026-03-01')",
                           timeout=5_000)
    page.wait_for_timeout(200)

    assert page.locator('.dash-chip__key', has_text='Dates').count() == 1
    # A window with no preset name keeps the server's own rendering of it.
    assert _label(page) not in ('This Month', 'Last Month', 'YTD')


# ── Responsive ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('viewport', [PHONE, {'width': 768, 'height': 1024}],
                         ids=['phone', 'tablet'])
def test_the_bar_fits_without_scrolling_the_page_sideways(signed_in, viewport):
    page = signed_in
    page.set_viewport_size(viewport)
    visit(page, '/?account=Visa&category=Travel&category=Groceries',
          f' at {viewport["width"]}px')

    _open(page, '#moreTrigger')
    assert_no_horizontal_overflow(page, f' with a filter menu open at {viewport["width"]}px')


def test_the_controls_wrap_rather_than_overflow_on_a_phone(signed_in):
    page = signed_in
    page.set_viewport_size(PHONE)
    _dash(page)

    bar = page.locator('#dashFilterForm').bounding_box()
    date_pill = page.locator('#dateTrigger').bounding_box()
    account_pill = page.locator('#accountTrigger').bounding_box()

    assert date_pill['y'] < account_pill['y'], \
        'the pills did not wrap onto separate rows on a phone'
    assert bar['height'] < 160, f'the wrapped bar is {bar["height"]}px tall'
