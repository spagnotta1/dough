"""Signing in, getting it wrong, and signing out — in a real browser.

``tests/test_auth.py`` already asserts every one of these outcomes against the
test client, and does it faster. What it cannot assert is that the form a person
is shown can produce those outcomes: that the fields are reachable, that the
submit button submits, that the CSRF token the server now requires is actually
carried by the markup, and that the error a rejected attempt produces is
rendered somewhere a human would look.

That gap is not hypothetical. ``CSRF_ENABLED`` is off for the rest of the suite,
so until this file existed nothing exercised the login form with the protection
that production runs with.
"""

from .conftest import PASSWORD, USERNAME, assert_no_horizontal_overflow, visit


def test_the_login_page_renders_a_usable_form(page):
    visit(page, '/login')
    assert page.locator('input[name="username"]').is_visible()
    assert page.locator('input[name="password"]').is_visible()
    assert page.locator('button[type="submit"], input[type="submit"]').is_visible()


def test_a_correct_password_signs_you_in(page):
    visit(page, '/login')
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url(lambda url: '/login' not in url, timeout=10_000)
    # Landing anywhere but /login is necessary but not sufficient: assert the
    # session actually took by fetching a page that requires one.
    visit(page, '/transactions')
    assert '/login' not in page.url


def test_a_wrong_password_says_so_on_the_page(page):
    """The message has to be *visible*, not merely present in the response body.

    A flash rendered into a container the design system hides is the failure
    mode this catches, and it is invisible to a test that asserts on
    ``resp.data``.
    """
    visit(page, '/login')
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', 'not-the-password')
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state('load')

    assert '/login' in page.url, 'a rejected sign-in must not navigate away'
    error = page.get_by_text('Invalid username or password')
    assert error.count() >= 1, 'no rejection message rendered'
    assert error.first.is_visible(), 'the rejection message is in the DOM but not on screen'


def test_a_rejected_sign_in_does_not_reveal_which_half_was_wrong(page):
    """A browser-level check on a security property, because this is the surface
    the property exists to protect: what a person can read off the screen.
    """
    visit(page, '/login')
    page.fill('input[name="username"]', 'nobody-by-that-name')
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state('load')

    body = page.locator('body').inner_text().lower()
    for leak in ('no such user', 'unknown user', 'user not found', 'wrong password'):
        assert leak not in body, f'the login page distinguishes the two failures: {leak!r}'


def test_signing_out_ends_the_session(signed_in):
    """Sign-out lives inside the profile menu, so this exercises the menu too.

    Reaching the button the way a person reaches it is the point. A test that
    submitted the form directly would keep passing if the menu stopped opening,
    which would leave no way to sign out at all.
    """
    page = signed_in
    visit(page, '/')

    trigger = page.locator('#profile-btn')
    assert trigger.get_attribute('aria-expanded') == 'false'
    trigger.click()
    assert trigger.get_attribute('aria-expanded') == 'true', \
        'the profile menu trigger did not update aria-expanded'

    logout = page.locator('form[action*="logout"] button')
    logout.wait_for(state='visible', timeout=5_000)
    logout.click()
    page.wait_for_load_state('load')

    # The real assertion: the session is gone, not merely that a redirect
    # happened. Ask for a protected page and expect to be turned away.
    page.goto('/transactions', wait_until='load')
    assert '/login' in page.url, 'a protected page was served after signing out'


def test_the_login_page_fits_a_phone(page):
    page.set_viewport_size({'width': 375, 'height': 812})
    visit(page, '/login', note=' at 375px')
    assert_no_horizontal_overflow(page, ' at 375px')


# ── The way back out ────────────────────────────────────────────────────────
#
# `tests/test_auth_navigation.py` asserts both links exist, point at `/`, and
# are named. What it cannot assert is that they are on screen, that they can be
# reached with a keyboard, that the focus ring is drawn, and that clicking the
# dog actually navigates — the mascot is an SVG injected by a deferred script
# into a `<span>`, and "the anchor is in the markup" says nothing about whether
# the thing a person aims at ends up inside it.

def test_clicking_the_branding_goes_to_the_public_page(page):
    visit(page, '/login')
    page.click('.auth-brand')
    page.wait_for_url(lambda url: '/login' not in url, timeout=10_000)
    assert page.locator('.lp-hero').is_visible(), (
        'the logo led somewhere, but not to the landing page')


def test_clicking_the_mascot_itself_goes_home(page):
    """The dog is 104px of the page and the wordmark is one small line.

    Aiming at the mascot is what a person actually does, and an anchor that
    wraps only the text would pass the test above while ignoring every click
    that lands on the picture.
    """
    visit(page, '/login')
    page.click('.auth-brand [data-dough]')
    page.wait_for_url(lambda url: '/login' not in url, timeout=10_000)
    assert page.locator('.lp-hero').is_visible()


def test_back_to_home_leaves_the_login_page(page):
    visit(page, '/login')
    back = page.locator('.auth-back__link')
    assert back.is_visible(), 'the way home is in the DOM but not on screen'
    back.click()
    page.wait_for_url(lambda url: '/login' not in url, timeout=10_000)
    assert page.locator('.lp-hero').is_visible()


def test_the_way_home_is_reachable_and_visible_from_the_keyboard(page):
    """Focus lands in the username field on load, so the branding is one
    Shift+Tab away — and it has to *show* that it has focus.

    Driven from the keyboard rather than with `.focus()`: `:focus-visible` is
    defined in terms of how the element was focused, so a scripted focus can
    pass while the ring a keyboard user depends on is never drawn.
    """
    visit(page, '/login')
    page.focus('input[name="username"]')
    page.keyboard.press('Shift+Tab')

    focused = page.evaluate('document.activeElement.className')
    assert 'auth-brand' in focused, (
        f'Shift+Tab from the username field reached {focused!r}, not the branding')
    ring = page.evaluate(
        "getComputedStyle(document.activeElement).boxShadow")
    assert ring and ring != 'none', 'the branding takes focus without showing it'


def test_back_to_home_is_the_last_stop_before_leaving_the_form(page):
    """Tabbing forward from the button reaches it, in document order, without
    a keyboard trap in between."""
    visit(page, '/login')
    page.focus('button[type="submit"]')
    for _ in range(6):
        page.keyboard.press('Tab')
        if 'auth-back__link' in page.evaluate('document.activeElement.className'):
            return
    raise AssertionError('tabbing forward from Sign in never reached the way home')


def test_the_way_home_is_reachable_on_a_phone(page):
    """Visible *and in the viewport* at 375px, without scrolling.

    A link below the fold on the page somebody was bounced to is a link that
    does not exist. The check is the element's own box against the viewport
    height rather than `is_visible()`, which is true of anything rendered.
    """
    page.set_viewport_size({'width': 375, 'height': 812})
    visit(page, '/login', note=' at 375px')

    box = page.locator('.auth-back__link').bounding_box()
    assert box, 'no way home on a phone'
    assert box['y'] + box['height'] <= 812, (
        f"the way home sits at {box['y'] + box['height']:.0f}px on an 812px "
        f'screen, below the fold')
    # Tappable: .78rem of text is ~15px tall on its own, so the target is
    # entirely the padding's doing, and this is what proves it survived. 24px
    # is the floor WCAG 2.5.8 sets.
    assert box['height'] >= 24, f"a {box['height']:.0f}px tap target"


def test_a_sign_out_message_does_not_bury_the_form_on_a_phone(signed_in):
    """The banner renders inside the card, above "Welcome back".

    On a 375px screen that is the arrangement most likely to go wrong: a
    message tall enough to push the fields down, or one overlapping them, turns
    an explanation into an obstruction. Both halves are asserted — the message
    is readable, and everything it is about is still where it was.

    The logout form is submitted directly rather than through the profile menu,
    which `test_signing_out_ends_the_session` already exercises. The subject
    here is the page that comes *after* it.
    """
    page = signed_in
    page.set_viewport_size({'width': 375, 'height': 812})
    visit(page, '/', note=' at 375px')
    page.evaluate("document.querySelector('form[action*=\"logout\"]').submit()")
    page.wait_for_url(lambda url: '/login' in url, timeout=10_000)

    assert_no_horizontal_overflow(page, ' on the signed-out login page at 375px')
    banner = page.locator('.auth-flash')
    field = page.locator('input[name="username"]')
    submit = page.locator('button[type="submit"]')
    for name, locator in (('the sign-out message', banner),
                          ('the username field', field),
                          ('the Sign in button', submit),
                          ('the way home', page.locator('.auth-back__link'))):
        assert locator.is_visible(), f'{name} is not on screen after signing out'

    message, username = banner.bounding_box(), field.bounding_box()
    assert message['y'] + message['height'] <= username['y'], (
        'the sign-out message overlaps the username field')
    assert username['y'] + username['height'] <= 812, (
        'the sign-out message pushed the form below the fold')
