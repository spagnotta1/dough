"""Connections — the brand-coloured institution controls.

Everything else on this page is design-system components already held to the
page sweep's baseline. What is specific to Connections is colour that comes
from *data*: each institution row paints its icon and its Connect button
with the institution's own brand colour. No theme token can know what that
will be — ``--accent-on`` is the ink for the *theme's* accent, and painting
it over Plaid's ``#000000`` produced a black label on a black button that
read as a disabled control. The ink is now derived from the brand colour
server-side (``dough/contrast.py``) and arrives inline with the background.

The second test covers how the page is actually reached. There is no
Connections link in the primary nav — it is opened from the profile menu,
which is a *soft* navigation, and this page is the one in the product most
sensitive to that: its Alpine component is defined in an inline script, so
anything that delays that script past Alpine's initialisation leaves every
control bound to ``connectionsApp()`` rendering blank. That is not
hypothetical; it is the bug this file was written for.
"""

from .conftest import visit, wait_for_layout

#: Both brand-coloured controls: the row's letter square and its button.
BRAND_CONTROLS = '.conn-offer__icon, .conn-offer__connect'


def _unreadable(page):
    """Brand controls whose ink does not clear WCAG AA on its own background.

    Measured through the app's own contrast routine, so this test and the
    runtime agree on what "readable" means.
    """
    return page.evaluate(r"""(sel) => {
      const out = [];
      document.querySelectorAll(sel).forEach(el => {
        const cs = getComputedStyle(el);
        const ink = cs.color.match(/\d+/g).slice(0, 3).map(Number);
        const bg = cs.backgroundColor.match(/\d+/g).slice(0, 3).map(Number);
        const ratio = CheckScheme.contrast(ink, bg);
        if (ratio < 4.5) {
          out.push(`${el.className}: rgb(${ink}) on rgb(${bg}) = ${ratio.toFixed(2)}:1`);
        }
      });
      return out;
    }""", BRAND_CONTROLS)


def test_brand_controls_are_readable_on_their_own_colour(signed_in):
    page = signed_in
    visit(page, '/connections')
    page.wait_for_selector('.conn-offer__connect')
    wait_for_layout(page)

    bad = _unreadable(page)
    assert not bad, 'brand controls with unreadable ink:\n  ' + '\n  '.join(bad)


def test_the_page_works_when_reached_by_soft_navigation(signed_in):
    """The profile-menu route into Connections, end to end.

    Three failures hide here, and only the third is visible to a screenshot:
    the inline script never ran, Alpine could not evaluate
    ``x-data="connectionsApp()"``, and every ``x-show`` label collapsed to
    ``display: none`` — a button with a background, a border and no text.
    """
    page = signed_in
    page.goto('/', wait_until='load')
    wait_for_layout(page)

    page.click('#profile-btn')
    page.click('.pm-item[href="/connections"]')
    page.wait_for_selector('.conn-offer__connect', timeout=10_000)
    wait_for_layout(page)

    assert page.evaluate(
        "!!document.getElementById('connections-page')._x_dataStack"), (
        'Alpine never initialised connectionsApp after a soft navigation — '
        'every control on the page is inert')

    button = page.locator('.conn-offer__connect').first
    assert button.inner_text().strip(), (
        'the Connect button rendered with no label: its x-show spans are all '
        'hidden, which is what an unevaluated x-data leaves behind')
    assert not _unreadable(page), 'brand ink is unreadable after a soft navigation'


def test_the_connect_button_is_wired_end_to_end(signed_in, page_health):
    """Clicking Connect runs the real handler rather than sitting there as
    inert markup. The network call is intercepted and refused, so nothing is
    ever connected — what is asserted is the round trip: Alpine bound the
    component, the click reached ``connect()``, and the failure surfaced
    through ``showToast``."""
    page = signed_in
    # The refusal below is the point of the test; the browser still logs the
    # 400 as a console error, which the health guard is told to expect.
    page_health.expect_server_error('/api/connections')

    visit(page, '/connections')
    page.wait_for_selector('.conn-offer__connect')

    page.route('**/api/connections', lambda route: route.fulfill(
        status=400,
        content_type='application/json',
        body='{"error": "refused by the test, on purpose"}'))

    button = page.locator('.conn-offer__connect').first
    assert button.is_enabled()
    button.click()
    page.locator('#toast-container .ds-toast',
                 has_text='refused by the test, on purpose').wait_for(timeout=5_000)
