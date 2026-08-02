"""Every page, at every viewport, held to the baseline all pages must meet.

Three things are asserted here, and they are the three that no amount of reading
the templates can establish:

  * the page renders — a real engine parsed it and produced a `<main>` with
    content in it;
  * nothing threw and nothing 5xx'd while it did (``page_health``, autouse in
    conftest.py);
  * the document does not scroll sideways at 1440, 768 or 375 CSS pixels.

The page list is **derived from the URL map**, not written out here. That is the
same discipline ``tests/test_route_guard.py`` uses, and for the same reason: a
list maintained by hand is a list that silently stops covering the page somebody
added last week. A new page joins this sweep by existing.

The corollary is that ``SKIP`` below is the only hand-maintained part, so every
entry in it carries the reason it is not a page.
"""

import pytest

from app import create_app

from .conftest import assert_no_horizontal_overflow, wait_for_layout

#: GET routes that answer with something other than a page a person looks at.
#: Each is excluded for a stated reason; anything not listed gets swept.
SKIP = {
    '/export':         'streams a CSV attachment, so there is no document to measure',
    '/clear_filters':  'a redirect that resets filter state, not a destination',
    # Phase 10.7. Same reason as /export and found the same way -- by this sweep
    # failing on it, which is the sweep working: a route added without thinking
    # about whether it is a page gets asked to prove it is one.
    '/settings/export': 'sends a JSON attachment, so there is no document to measure',
    # These two render fine, but not for somebody who is signed in: /setup
    # redirects to /login once an owner exists, and /login redirects to the
    # dashboard. tests/test_auth_journey.py covers both signed out.
    '/login':          'covered signed-out by test_auth_journey.py',
    '/setup':          'unreachable once an owner account exists',
    # Phase 10.5. The same shape as /login and /setup: these render for somebody
    # who is *not* signed in, which is the opposite of what this sweep visits.
    # /register redirects a signed-in visitor to the dashboard, and both pages
    # use the auth shell rather than base.html, so they have no <main> for this
    # file's assertions to find. tests/browser/test_identity_journey.py sweeps
    # them signed out, at the same three viewports.
    '/register':        'renders for somebody with no session; swept by test_identity_journey.py',
    '/forgot-password': 'renders for somebody with no session; swept by test_identity_journey.py',
}


def _page_paths():
    """Parameterless GET routes that serve HTML to a signed-in person.

    Built from a throwaway application rather than from the live server, because
    parametrization happens at collection time and the server does not exist
    yet. ``AUTH_ENABLED`` is on so that /household is present — it is registered
    conditionally (see dough/blueprints/__init__.py), and a sweep built from an
    auth-off app would quietly omit it.
    """
    app = create_app(test_config={'TESTING': True, 'AUTH_ENABLED': True})
    paths = set()
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if 'GET' not in rule.methods or rule.arguments:
            continue
        if path.startswith(('/api/', '/health/', '/static/')):
            continue
        if path in SKIP:
            continue
        paths.add(path)
    return sorted(paths)


PAGE_PATHS = _page_paths()

#: Desktop, tablet, phone. The phone width is an iPhone SE/13 mini, the
#: narrowest screen worth supporting; the tablet width is iPad portrait, which
#: is by a wide margin the most common tablet viewport there is.
VIEWPORTS = (
    ('desktop', 1440, 900),
    ('tablet',   768, 1024),
    ('phone',    375, 812),
)

# The tablet row was xfail(strict=True) when this file was written: every page
# scrolled sideways at 768px, because base.html revealed the seven-link desktop
# nav at Tailwind's `sm:` (640px) when that row needs about 880px. The touch
# nav handed over at 640px, so from 641px to ~880px the product had no
# navigation that fit — a band that contains every tablet in portrait.
#
# Fixed by moving the handover to 1023.98px, the exact complement of `lg:`.
# The marker is gone because all twelve turned XPASS the moment it was, which
# is what strict=True is for. test_ui_invariants.py's
# test_the_touch_breakpoint_agrees_across_files now holds the two files that
# have to carry that number to the same value.
VIEWPORT_PARAMS = [pytest.param(name, w, h, id=name) for name, w, h in VIEWPORTS]


def test_the_sweep_actually_covers_the_product():
    """A guard on the guard.

    Everything below is parametrized off ``_page_paths()``. If that function
    ever returns an empty list — a changed URL prefix, a blueprint that stopped
    registering — every test in this file would pass by having nothing to run,
    which is the most comfortable kind of wrong.
    """
    assert len(PAGE_PATHS) >= 10, f'the page sweep found only {PAGE_PATHS}'
    for expected in ('/', '/transactions', '/chat', '/rules', '/budgets'):
        assert expected in PAGE_PATHS, f'{expected} fell out of the sweep'


@pytest.mark.parametrize('path', PAGE_PATHS)
def test_page_renders(signed_in, path):
    """The page comes back, with content, for somebody who is signed in."""
    page = signed_in
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto(path, wait_until='load')

    assert '/login' not in page.url, f'{path} bounced to the login page'
    main = page.locator('main')
    assert main.count() == 1, f'{path} rendered {main.count()} <main> elements'
    assert main.first.inner_html().strip(), f'{path} rendered an empty <main>'


@pytest.mark.parametrize('path', PAGE_PATHS)
@pytest.mark.parametrize('name, width, height', VIEWPORT_PARAMS)
def test_page_does_not_scroll_sideways(signed_in, path, name, width, height):
    """Horizontal overflow, per page, per viewport.

    Parametrized on both axes rather than looped, so a failure names the exact
    page-and-width pair — and so a known failure can be marked at that
    granularity. That mattered: the tablet row was xfail for a while, and a loop
    would have forced the whole page to carry the marker, hiding any desktop or
    phone regression behind a tablet defect nobody had got to yet.
    """
    page = signed_in
    page.set_viewport_size({'width': width, 'height': height})
    page.goto(path, wait_until='load')
    # The helper waits for layout to settle before it measures; see
    # conftest._SETTLED for why a fixed sleep was not good enough.
    assert_no_horizontal_overflow(page, f' [{path} @ {name} {width}px]')


@pytest.mark.parametrize('path', PAGE_PATHS)
def test_page_has_a_title_of_its_own(signed_in, path):
    """A shared fallback title is what a page gets when its block is missing.

    Cheap to assert, and it catches a real class of template mistake: a new page
    that forgot ``{% block title %}`` looks completely correct until it is
    bookmarked or opened in a second tab.
    """
    page = signed_in
    page.goto(path, wait_until='load')
    title = page.title().strip()
    assert title, f'{path} has no <title>'
    assert title.lower() != 'dough', \
        f'{path} fell back to the base title — it is missing a title block'


# ── The navigation handover ─────────────────────────────────────────────────
#
# The sweep above would catch a regression here, but it would report it as
# "twelve pages are suddenly too wide", which sends the reader looking at twelve
# pages. These two say what actually broke.

@pytest.mark.parametrize('name, width, height', VIEWPORT_PARAMS)
def test_exactly_one_navigation_is_offered_at_each_width(signed_in, name, width, height):
    """The desktop row and the tab bar are alternatives, never both and never
    neither.

    "Never neither" is the one that had been violated: the desktop row appeared
    at 640px and the tab bar disappeared at the same width, but the row did not
    fit until ~880px. Everything in between had a navigation that ran off the
    side of the screen. "Never both" is the mistake the obvious fix makes —
    widening one media query and forgetting the other.
    """
    page = signed_in
    page.set_viewport_size({'width': width, 'height': height})
    page.goto('/', wait_until='load')
    wait_for_layout(page)

    desktop = page.locator('#primary-nav').is_visible()
    touch = page.locator('#tab-bar').is_visible()

    assert desktop != touch, (
        f'at {name} ({width}px) the desktop nav is '
        f'{"shown" if desktop else "hidden"} and the tab bar is '
        f'{"shown" if touch else "hidden"} — exactly one must be offered'
    )
    if desktop:
        # And it fits. This is the assertion the whole breakpoint move is about.
        nav = page.locator('#primary-nav').bounding_box()
        assert nav['x'] + nav['width'] <= width, \
            f'the desktop nav is shown at {width}px but runs to {nav["x"] + nav["width"]}px'


def test_no_destination_is_lost_when_the_desktop_nav_folds_away(signed_in):
    """Everything reachable at 1440px stays reachable at 768px.

    This is what makes the breakpoint move safe rather than merely tidy. Hiding
    the desktop row fixes the overflow no matter what; it is only *correct* if
    the tab bar and the "More" sheet between them still go everywhere the row
    went. Asserted against the rendered DOM rather than by reading base.html,
    because a link that is present but permanently hidden is not a destination.
    """
    page = signed_in

    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto('/', wait_until='load')
    desktop_links = set(page.locator('#primary-nav a').evaluate_all(
        'els => els.map(e => e.getAttribute("href"))'))
    assert len(desktop_links) >= 5, f'the desktop nav looks empty: {desktop_links}'

    page.set_viewport_size({'width': 768, 'height': 1024})
    page.goto('/', wait_until='load')
    wait_for_layout(page)
    touch_links = set(page.locator('#tab-bar a, #mobile-menu a').evaluate_all(
        'els => els.map(e => e.getAttribute("href"))'))

    missing = desktop_links - touch_links
    assert not missing, (
        f'these destinations exist at 1440px and nowhere at 768px: {sorted(missing)}'
    )
