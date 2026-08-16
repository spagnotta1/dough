"""The dashboard's compact filter bar.

The bar replaced a panel that held six preset buttons, two date fields, an
account select, Apply, Reset all and Done, permanently open above the numbers.
What is asserted here is not the layout — that is `tests/browser/` — but the
two properties the refactor had to keep and the one it added:

  * every window and account the panel could ask for still selects exactly the
    same rows, because the route did not change;
  * the controls and the applied-filter chips are two views of **one** form,
    so they cannot disagree about what is on;
  * a default is not a filter. Month-to-date and "All accounts" are the
    absence of a choice, and a chip offering to remove the state the page is
    already in is a control with nothing to do.

The arithmetic behind the preset buttons lives in `static/js/dashboard.js` and
is deliberately not duplicated here: this file asserts what a *window* does,
and `tests/browser/test_dashboard_filters.py` asserts that clicking "Last
Month" produces the window this file describes. Splitting it that way is what
keeps the two from drifting while letting each be checked where it lives.
"""

import html as html_lib
import json
import re
from datetime import date, timedelta

import pytest

from models import Transaction, db

#: One applied-filter chip: its key, its value, and where its ✕ leads.
CHIP = re.compile(
    r'<span class="dash-chip">\s*'
    r'<span class="dash-chip__key">([^<]+)</span>\s*'
    r'<span class="dash-chip__val"[^>]*>([^<]+)</span>\s*'
    r'<a class="dash-chip__x"[^>]*\shref="([^"]+)"', re.S)


def _chips(page):
    """`[(key, value), ...]` for the chips on a rendered dashboard."""
    return [(key, value) for key, value, _ in CHIP.findall(page)]


def _remove(page, key, value):
    """The URL behind one chip's ✕, ready to hand back to the client."""
    for chip_key, chip_value, href in CHIP.findall(page):
        if (chip_key, chip_value) == (key, value):
            return html_lib.unescape(href)
    raise AssertionError(f'no {key} chip reading {value!r} in: {_chips(page)}')


def _spent(page):
    """`{category: dollars out}` as the page computed it.

    Asserted on rather than the rendered figures because `|money` rounds to
    whole dollars: two windows that differ by ninety cents both render "$613",
    and a filter test that cannot tell them apart is not testing the filter.
    This payload is also what every chart on the page is drawn from, so it is
    the closest thing to "what the dashboard is showing".
    """
    blob = re.search(r'<script id="dashData" type="application/json">\s*(.*?)\s*</script>',
                     page, re.S)
    assert blob, 'the dashboard rendered no data payload'
    stats = json.loads(blob.group(1))['categoryStats']
    return {category: round(figures['outbound'], 2)
            for category, figures in stats.items() if figures['outbound']}


def _seed(rows):
    for account, when, amount, category, description in rows:
        db.session.add(Transaction(account_name=account, date=when,
                                   amount=amount, category=category,
                                   description=description))
    db.session.commit()


def _dash(client, query=''):
    return client.get('/?' + query if query else '/').get_data(as_text=True)


# ── Windows ─────────────────────────────────────────────────────────────────
#
# Named for the preset that produces each one. The dates are computed rather
# than written down so the file does not expire: a fixed "2026-03-01" passes
# only while somebody's clock agrees with it, and the dashboard's own default
# window is relative to today.

TODAY = date.today()


def _month_start(when):
    return when.replace(day=1)


def _last_month():
    end = _month_start(TODAY) - timedelta(days=1)
    return _month_start(end), end


WINDOWS = {
    'this_month': (_month_start(TODAY), TODAY),
    'last_month': _last_month(),
    'ytd': (date(TODAY.year, 1, 1), TODAY),
    'last_year': (date(TODAY.year - 1, 1, 1), date(TODAY.year - 1, 12, 31)),
}


def _q(window, **extra):
    start, end = window
    query = f'start_date={start:%Y-%m-%d}&end_date={end:%Y-%m-%d}'
    for key, value in extra.items():
        query += f'&{key}={value}'
    return query


@pytest.mark.parametrize('preset', sorted(WINDOWS))
def test_every_preset_window_selects_only_its_own_rows(client, preset):
    """One transaction inside the window, one outside, for each of the four
    windows that can be stated without repeating the JS's month arithmetic.

    The 3- and 6-month presets are the two that cannot: `new Date(y, m - 3, d)`
    rolls a 31st into the following month, and a Python restatement of that
    would be a second implementation to keep in step rather than a test. They
    are covered end-to-end in the browser suite instead.
    """
    start, end = WINDOWS[preset]
    inside = start + (end - start) // 2

    _seed([('Checking', inside, -11.11, 'Inside', 'INSIDE THE WINDOW'),
           ('Checking', start - timedelta(days=1), -22.22, 'Before', 'THE DAY BEFORE'),
           ('Checking', end + timedelta(days=1), -33.33, 'After', 'THE DAY AFTER')])

    assert _spent(_dash(client, _q(WINDOWS[preset]))) == {'Inside': 11.11}


def test_a_custom_window_is_inclusive_of_both_ends(client):
    """The boundary the route carries a comment about: `Transaction.date` is a
    Date column, and a window compared against datetimes silently lost its
    first day."""
    start = TODAY - timedelta(days=10)
    end = TODAY - timedelta(days=5)

    _seed([('Checking', start, -1.01, 'FirstDay', 'ON THE START DATE'),
           ('Checking', end, -2.02, 'LastDay', 'ON THE END DATE'),
           ('Checking', start - timedelta(days=1), -3.03, 'Before', 'THE DAY BEFORE'),
           ('Checking', end + timedelta(days=1), -4.04, 'After', 'THE DAY AFTER')])

    assert _spent(_dash(client, _q((start, end)))) == {'FirstDay': 1.01, 'LastDay': 2.02}


# ── Accounts ────────────────────────────────────────────────────────────────

def test_the_account_options_are_the_ledger_s_own_accounts(client):
    """The control offered a hardcoded Checking and Savings for as long as it
    existed — two names a household may not have, and no name for the ones it
    does. A Visa was in every chart with no way to filter to it."""
    _seed([('Visa', TODAY, -10.00, 'Travel', 'DELTA'),
           ('360 Checking', TODAY, -20.00, 'Food', 'DELI')])

    page = _dash(client)

    assert 'value="Visa"' in page
    assert 'value="360 Checking"' in page
    assert 'value="Savings"' not in page, \
        'the account list is still being written into the template'


def test_an_account_filter_narrows_the_page_and_says_so(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES'),
           ('Visa', TODAY, -612.80, 'Travel', 'DELTA AIR LINES')])

    page = _dash(client, _q(WINDOWS['this_month'], account='Visa'))

    assert _spent(page) == {'Travel': 612.80}
    assert ('Account', 'Visa') in _chips(page)


def test_all_accounts_is_a_default_rather_than_an_applied_filter(client):
    """Including when it is asked for by name. "This Month" and "All accounts"
    are what the page opens on, so neither leaves a chip offering to remove
    the state the reader is already in."""
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES')])

    page = _dash(client, _q(WINDOWS['this_month'], account='both'))

    assert _spent(page) == {'Groceries': 84.21}
    assert not _chips(page), f'the default state shows chips: {_chips(page)}'
    assert 'Clear all' not in page


def test_changing_the_account_after_the_date_keeps_the_date(client):
    """The two controls submit one form, so neither can drop the other's
    value — this is the property the old panel's three buttons made easy to
    get wrong and the reason the bar keeps a single source of truth."""
    older = _month_start(TODAY) - timedelta(days=10)
    _seed([('Checking', older, -50.37, 'Groceries', 'INSIDE THE OLD WINDOW'),
           ('Visa', older, -75.53, 'Travel', 'VISA IN THE OLD WINDOW'),
           ('Visa', TODAY, -99.13, 'Travel', 'VISA TODAY')])

    window = (older - timedelta(days=1), older + timedelta(days=1))
    page = _dash(client, _q(window, account='Visa'))

    assert _spent(page) == {'Travel': 75.53}, \
        'the account and the window did not both survive the same submission'


# ── Combined ────────────────────────────────────────────────────────────────

def test_a_date_an_account_and_a_category_compose(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES'),
           ('Checking', TODAY, -15.99, 'Subscriptions', 'NETFLIX'),
           ('Visa', TODAY, -141.07, 'Groceries', 'WHOLE FOODS')])

    page = _dash(client, _q(WINDOWS['this_month'], account='Checking',
                            category='Groceries'))

    assert _spent(page) == {'Groceries': 84.21}
    assert ('Account', 'Checking') in _chips(page)
    assert ('Category', 'Groceries') in _chips(page)


# ── Chips ───────────────────────────────────────────────────────────────────

def test_a_chosen_window_becomes_a_chip_and_the_default_does_not(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES')])

    assert not [c for c in _chips(_dash(client)) if c[0] == 'Dates']

    chosen = _dash(client, _q(WINDOWS['last_year']))
    assert [c for c in _chips(chosen) if c[0] == 'Dates'], \
        'a window somebody picked is not shown as applied'


def test_removing_one_chip_leaves_the_others_alone(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES'),
           ('Visa', TODAY, -141.07, 'Groceries', 'WHOLE FOODS'),
           ('Visa', TODAY, -612.80, 'Travel', 'DELTA AIR LINES')])

    page = _dash(client, _q(WINDOWS['ytd'], account='Visa', category='Groceries'))
    assert ('Account', 'Visa') in _chips(page)
    assert ('Category', 'Groceries') in _chips(page)

    after = client.get(_remove(page, 'Category', 'Groceries')).get_data(as_text=True)

    assert ('Category', 'Groceries') not in _chips(after)
    assert ('Account', 'Visa') in _chips(after), 'dropping a category dropped the account'
    assert _spent(after) == {'Groceries': 141.07, 'Travel': 612.80}, \
        'removing one filter did not leave exactly the others in place'


def test_removing_the_date_chip_returns_to_month_to_date(client):
    """Not "omit the dates": they are sticky in the session, so a link that
    simply leaves them off restores the very window it is removing."""
    old = _month_start(TODAY) - timedelta(days=40)
    _seed([('Checking', old, -50.37, 'LongAgo', 'LONG AGO'),
           ('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES')])

    page = _dash(client, _q((old - timedelta(days=1), old + timedelta(days=1))))
    dates = [c for c in _chips(page) if c[0] == 'Dates']
    assert dates, 'a hand-picked window is not shown as applied'

    after = client.get(_remove(page, *dates[0])).get_data(as_text=True)

    assert _spent(after) == {'Groceries': 84.21}
    assert not [c for c in _chips(after) if c[0] == 'Dates'], \
        'the default window came back as an applied filter'


def test_removing_the_account_chip_keeps_the_window(client):
    old_window = (_month_start(TODAY) - timedelta(days=40),
                  _month_start(TODAY) - timedelta(days=30))
    inside = old_window[0] + timedelta(days=2)
    _seed([('Checking', inside, -50.37, 'Groceries', 'CHECKING INSIDE'),
           ('Visa', inside, -75.53, 'Travel', 'VISA INSIDE'),
           ('Visa', TODAY, -99.13, 'Travel', 'VISA TODAY')])

    page = _dash(client, _q(old_window, account='Visa'))
    after = client.get(_remove(page, 'Account', 'Visa')).get_data(as_text=True)

    assert _spent(after) == {'Groceries': 50.37, 'Travel': 75.53}, \
        'the account came off but the window did not stay'


def test_clear_all_appears_only_when_something_is_applied(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES')])

    assert 'Clear all' not in _dash(client)
    assert 'Clear all' in _dash(client, _q(WINDOWS['this_month'], account='Checking'))


def test_clear_all_restores_the_default_state(client):
    old = _month_start(TODAY) - timedelta(days=40)
    _seed([('Checking', old, -50.37, 'LongAgo', 'LONG AGO'),
           ('Visa', TODAY, -84.21, 'Groceries', 'WHOLE FOODS')])

    _dash(client, _q((old - timedelta(days=1), old + timedelta(days=1)),
                     account='Checking', category='LongAgo'))

    client.get('/clear_filters?next=/', follow_redirects=True)
    page = _dash(client)

    assert not _chips(page), f'a filter survived Clear all: {_chips(page)}'
    assert _spent(page) == {'Groceries': 84.21}, 'the default window did not come back'


# ── The markup the behaviour rests on ───────────────────────────────────────

def test_the_permanent_filter_panel_is_gone(client):
    """Its controls are the bar's now; none of it may be left behind, because
    a second copy of `name="account"` in the same form is a second answer."""
    page = _dash(client)

    for gone in ('id="dashFilterPanel"', 'id="dashFilterToggle"',
                 'id="filterApply"', 'id="filterClose"', 'id="catRow"',
                 'Reset all', '>Done<'):
        assert gone not in page, f'the old filter panel still renders {gone}'


def test_the_bar_holds_one_form_and_the_hooks_its_script_needs(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES')])
    page = _dash(client)

    for hook in ('id="dashFilters"', 'id="dashFilterForm"', 'id="dateMenu"',
                 'id="accountMenu"', 'id="moreMenu"', 'id="customRange"',
                 'id="start_date"', 'id="end_date"', 'data-date-label',
                 'data-preset="this_month"', 'id="dashPeriodLabel"'):
        assert hook in page, f'the filter bar lost {hook}'

    assert page.count('id="dashFilterForm"') == 1
    # Every control that carries filter state is inside that one form, which
    # is what lets a preset apply without discarding the account beside it.
    form = page.split('id="dashFilterForm"')[1].split('</form>')[0]
    for field in ('name="start_date"', 'name="end_date"', 'name="account"',
                  'name="category"'):
        assert field in form, f'{field} is outside the filter form'


def test_the_category_boxes_agree_with_the_applied_categories(client):
    _seed([('Checking', TODAY, -84.21, 'Groceries', 'TRADER JOES'),
           ('Checking', TODAY, -15.99, 'Subscriptions', 'NETFLIX')])

    page = _dash(client, _q(WINDOWS['this_month'], category='Groceries'))

    assert re.search(r'name="category" value="Groceries"\s+checked', page)
    assert not re.search(r'name="category" value="Subscriptions"\s+checked', page)
