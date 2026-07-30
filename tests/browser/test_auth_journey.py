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
