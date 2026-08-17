"""The rail's two moving parts: "See more", and the collapse.

Both are state a user sets once and expects to find again, and both are held in
localStorage — which is the part worth testing in a browser rather than against
the template. What the markup says is that a `<details>` and a class exist; what
these assert is that a click changes the layout, that the change is still there
after a reload, and that a collapsed rail is still navigable rather than a
column of unlabelled glyphs.
"""

from .conftest import wait_for_layout


DESKTOP = {'width': 1440, 'height': 900}


def _rail_width(page):
    return page.locator('#app-rail').bounding_box()['width']


def test_collapsing_gives_the_width_back_to_the_page(signed_in):
    """The point of the control: the content gets the pixels the rail had.

    Asserted on <main>'s box rather than on the rail's, because a rail that
    narrows while the page keeps its old offset is the failure that looks
    right in a screenshot of the rail alone — a 10rem gutter of nothing.
    """
    page = signed_in
    page.set_viewport_size(DESKTOP)
    page.goto('/', wait_until='load')
    wait_for_layout(page)

    wide_rail = _rail_width(page)
    wide_main = page.locator('main').bounding_box()['width']

    page.click('#rail-toggle')
    wait_for_layout(page)

    assert _rail_width(page) < wide_rail / 2, 'the rail did not narrow'
    assert page.locator('main').bounding_box()['width'] > wide_main, \
        'the rail collapsed but the page kept its old left offset'

    page.click('#rail-toggle')
    wait_for_layout(page)
    assert abs(_rail_width(page) - wide_rail) < 1, 'expanding did not restore the width'


def test_the_choice_survives_a_reload(signed_in):
    """A width set once stays set — it is a preference, not a gesture."""
    page = signed_in
    page.set_viewport_size(DESKTOP)
    page.goto('/', wait_until='load')
    page.click('#rail-toggle')

    page.goto('/transactions', wait_until='load')
    wait_for_layout(page)
    assert page.evaluate(
        "document.documentElement.classList.contains('rail-collapsed')"), \
        'the collapsed rail came back expanded on the next page'

    # And back, so the fixture's page does not carry the state into whatever
    # test runs next — the browser context is shared for the whole session.
    page.click('#rail-toggle')


def test_a_collapsed_row_still_says_what_it_is(signed_in):
    """The label is the only thing naming a destination, and it is hidden.

    So it has to be somewhere a pointer can find it. Without this the rail
    collapses into eleven outline glyphs and a mascot.
    """
    page = signed_in
    page.set_viewport_size(DESKTOP)
    page.goto('/', wait_until='load')
    page.click('#rail-toggle')

    titles = page.locator('#primary-nav a').evaluate_all(
        'els => els.map(e => e.getAttribute("title"))')
    assert all(titles), f'a collapsed rail row has no tooltip: {titles}'
    assert 'Dashboard' in titles

    page.click('#rail-toggle')
    titles = page.locator('#primary-nav a').evaluate_all(
        'els => els.map(e => e.getAttribute("title"))')
    assert not any(titles), \
        f'the tooltips outlived the collapse and now shadow visible labels: {titles}'


def test_see_more_opens_and_is_remembered(signed_in):
    """The disclosure, and the reason it is not merely a `<details>`.

    SPA navigation leaves it alone because it lives outside <main>; a hard load
    would rebuild it closed. Remembering it makes the two agree.
    """
    page = signed_in
    page.set_viewport_size(DESKTOP)
    page.goto('/', wait_until='load')

    upload = page.locator('#rail-more a[href="/upload"]')
    assert not upload.is_visible(), 'the second tier is showing before it is opened'

    page.click('#rail-more > summary')
    assert upload.is_visible(), 'clicking "See more" revealed nothing'

    page.goto('/budgets', wait_until='load')
    wait_for_layout(page)
    assert page.locator('#rail-more a[href="/upload"]').is_visible(), \
        '"See more" closed itself on the next page load'

    page.click('#rail-more > summary')   # leave it as it was found


def test_the_open_group_marks_the_page_you_are_on(signed_in):
    """A collapsed disclosure holding the current page is a rail claiming you
    are nowhere — so landing inside it opens it and lights the row."""
    page = signed_in
    page.set_viewport_size(DESKTOP)
    page.goto('/sync-history', wait_until='load')
    wait_for_layout(page)

    row = page.locator('#rail-more a[href="/sync-history"]')
    assert row.is_visible(), 'the group stayed shut on a page inside it'
    assert 'active' in (row.get_attribute('class') or ''), \
        'the current page is not marked in the second tier'

    page.click('#rail-more > summary')
