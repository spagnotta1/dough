"""Registering, recovering and managing an account — in a real browser.
[Phase 10.5]

`tests/test_identity.py` asserts every one of these outcomes against the test
client, and does it faster. What it cannot assert is that the forms a person is
shown can produce those outcomes: that the fields are reachable, that the submit
button submits, that the CSRF token the server requires is actually carried by
the markup, that the "shown once" token is on screen rather than merely in the
response body, and that none of it scrolls sideways on a phone.

That gap is not hypothetical here. `CSRF_ENABLED` is off for the rest of the
suite (see `config.TestingConfig`), so a `{{ csrf_field() }}` missing from one of
the four new forms would pass every test outside this directory — and would fail
for every real user with a 403 they cannot act on.

## Why this file signs *out* first

Everything in `test_pages.py` runs signed in. These pages are the opposite: the
landing page, `/register` and `/forgot-password` are reached by somebody with no
session, and visiting them with one renders a different page or a redirect. So
the sweep here is its own, and it is why those two paths are in that file's SKIP
list rather than being visited twice with the wrong session state.

The seeded browser database has `ALLOW_REGISTRATION` at its default (off), so the
registration *form* is exercised against a second application built by this
module. The closed state is asserted against the shared server, because that is
the state a default deployment is actually in.
"""

import re

import pytest

from .conftest import USERNAME, assert_no_horizontal_overflow, visit

#: The same three widths `test_pages.py` sweeps: desktop, iPad portrait, and an
#: iPhone SE/13 mini — the narrowest screen worth supporting.
VIEWPORTS = [
    pytest.param('desktop', 1440, 900, id='desktop'),
    pytest.param('tablet', 768, 1024, id='tablet'),
    pytest.param('phone', 375, 812, id='phone'),
]

#: Reached with no session. Visiting these signed in renders something else
#: entirely, which is why they are not in `test_pages.py`'s sweep.
SIGNED_OUT_PATHS = ['/', '/register', '/forgot-password']


def _sign_out(page):
    """Leave whatever session this browser context had.

    Clearing cookies rather than posting to `/logout`: this runs before tests
    that must be anonymous, and `/logout` on a context that never signed in is
    a 403 that leaves the test believing it is signed out when it is not.
    """
    page.context.clear_cookies()


# ---------------------------------------------------------------------------
# The signed-out sweep
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', SIGNED_OUT_PATHS)
@pytest.mark.parametrize('name, width, height', VIEWPORTS)
def test_a_signed_out_page_does_not_scroll_sideways(page, page_health, path,
                                                    name, width, height):
    """Horizontal overflow, per page, per viewport.

    Parametrized on both axes rather than looped so a failure names the exact
    page-and-width pair — the landing page is the one most likely to produce
    this, because it is the only page in the product with a two-column hero and
    a 240px mascot in it.
    """
    # `/register` answers 403 on this server, which runs with the default
    # ALLOW_REGISTRATION=0. That is the page working correctly -- see
    # test_a_closed_instance_explains_itself_rather_than_404ing -- but Chromium
    # logs every non-2xx document load as a console error.
    page_health.expect_error_status('/register')
    _sign_out(page)
    page.set_viewport_size({'width': width, 'height': height})
    page.goto(path, wait_until='load')
    assert_no_horizontal_overflow(page, f' [{path} @ {name} {width}px]')


@pytest.mark.parametrize('path', SIGNED_OUT_PATHS)
def test_a_signed_out_page_has_a_title_of_its_own(page, page_health, path):
    page_health.expect_error_status('/register')
    _sign_out(page)
    page.goto(path, wait_until='load')
    title = page.title()
    assert title and title != 'Dough', f'{path} has no title of its own'


# ---------------------------------------------------------------------------
# The landing page
# ---------------------------------------------------------------------------

def test_the_landing_page_renders_its_hero_and_features(page):
    _sign_out(page)
    visit(page, '/')

    assert page.get_by_role('heading', level=1).first.is_visible()
    for feature in ('Transaction management', 'Account aggregation',
                    'Investment tracking', 'AI financial assistant',
                    'Household collaboration'):
        assert page.get_by_text(feature).first.is_visible(), \
            f'the {feature!r} section is in the DOM but not on screen'


def test_the_landing_page_offers_a_way_in(page):
    """The primary action has to be clickable, not merely present.

    A CTA rendered underneath something, or with no href, is the failure this
    catches and the one no template test can see.
    """
    _sign_out(page)
    visit(page, '/')

    sign_in_link = page.get_by_role('link', name=re.compile('sign in', re.I)).first
    assert sign_in_link.is_visible()
    sign_in_link.click()
    page.wait_for_url(lambda url: '/login' in url, timeout=10_000)


def test_the_landing_page_shows_no_signed_in_navigation(page):
    """Every nav destination needs a session.

    A stranger would otherwise get seven links that bounce to `/login`, and on a
    phone a permanent bottom bar covering the page they can actually use.
    """
    _sign_out(page)
    page.set_viewport_size({'width': 375, 'height': 812})
    visit(page, '/', note=' at 375px')

    assert page.locator('#profile-btn').count() == 0
    assert page.locator('#tab-bar').count() == 0


def test_the_landing_page_follows_the_saved_theme(page):
    """The reason it extends base.html instead of using the auth shell.

    A returning visitor who chose Midnight three months ago must not be shown a
    copper marketing page that switches the instant they sign in.

    The preference is set by calling the application's own `applyTheme`, not by
    writing localStorage directly. The stored value is
    `{"name": …, "colors": {…}}`, and a test that wrote a bare string would
    silently store something the head-init script skips — passing, or failing,
    for reasons that have nothing to do with the landing page.
    """
    _sign_out(page)
    page.goto('/', wait_until='load')
    page.evaluate("applyTheme('midnight')")
    visit(page, '/')

    background = page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
    assert background.lower() == '#0d1117', (
        f'the landing page ignored the saved theme (--bg is {background!r})')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_a_closed_instance_explains_itself_rather_than_404ing(page, page_health):
    """The shared server runs with `ALLOW_REGISTRATION` at its default: off.

    "Not found" is indistinguishable from a typo, so somebody retries the URL
    instead of doing the thing that would work — asking for an invitation. The
    page says so, and offers the action that does work.
    """
    # 403 rather than 404, deliberately: the route exists and refuses, which is
    # a fact a monitor should be able to read. Tolerated here because Chromium
    # logs any non-2xx document load as a console error.
    page_health.expect_error_status('/register')
    _sign_out(page)
    visit(page, '/register')

    assert page.get_by_text('Registration is closed').first.is_visible()
    assert page.get_by_text('invitation').first.is_visible()
    assert page.get_by_role('link', name=re.compile('sign in', re.I)).first.is_visible()


def test_the_registration_form_creates_an_account(open_registration_page):
    """The whole flow through the markup, against a second application.

    This is what proves `{{ csrf_field() }}` is in the form: the server has CSRF
    on, so a missing token is a 403 rather than a redirect, and no amount of
    reading `dough/blueprints/auth.py` would tell you which.
    """
    page = open_registration_page
    page.goto('/register', wait_until='load')

    page.fill('input[name="username"]', 'newcomer')
    page.fill('input[name="email"]', 'newcomer@example.com')
    page.fill('input[name="password"]', 'correct7horse')
    page.fill('input[name="confirm"]', 'correct7horse')
    page.click('button[type="submit"]')

    page.wait_for_url(lambda url: '/register' not in url, timeout=10_000)
    # Landing anywhere but /register is necessary but not sufficient: assert the
    # session actually took by fetching a page that requires one.
    page.goto('/transactions', wait_until='load')
    assert '/login' not in page.url


def test_a_registration_error_is_rendered_where_a_person_would_look(open_registration_page):
    """Visible, not merely present in the response body.

    A message rendered into a container the design system hides is the failure
    this catches, and it is invisible to a test that asserts on `resp.data`.
    """
    page = open_registration_page
    page.goto('/register', wait_until='load')

    page.fill('input[name="username"]', 'someone')
    page.fill('input[name="email"]', 'someone@example.com')
    page.fill('input[name="password"]', 'short')
    page.fill('input[name="confirm"]', 'short')
    page.click('button[type="submit"]')
    page.wait_for_load_state('load')

    assert '/register' in page.url, 'a rejected registration must not navigate away'
    error = page.get_by_text(re.compile('at least 8 characters', re.I))
    assert error.count() >= 1, 'no rejection message rendered'
    assert error.first.is_visible(), 'the message is in the DOM but not on screen'


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

def test_the_forgot_password_form_says_the_same_thing_either_way(page):
    """The security property, asserted on what a person can read off the screen.

    `tests/test_identity.py` compares the response bytes. This compares the
    rendered text, which is the surface the property actually exists to protect
    — a difference introduced by a template conditional would pass there and
    fail here.
    """
    def _submit(address):
        visit(page, '/forgot-password')
        page.fill('input[name="email"]', address)
        page.click('button[type="submit"]')
        page.wait_for_load_state('load')
        return page.locator('body').inner_text()

    _sign_out(page)
    known = _submit(f'{USERNAME}@example.com')
    unknown = _submit('definitely-nobody@example.com')

    assert 'Check your email' in known
    assert known == unknown, (
        'the page differs between an address with an account and one without')


def test_an_invalid_reset_link_offers_a_way_forward(page, page_health):
    """It refuses without saying why, and then says what to do about it.

    Unknown, expired and already-used are indistinguishable on purpose. That
    would be a dead end without the "request a new link" action, which is the
    part that keeps a security decision from being a usability failure.
    """
    page_health.expect_error_status('/reset-password/')
    _sign_out(page)
    page.goto('/reset-password/' + 'x' * 43, wait_until='load')
    assert_no_horizontal_overflow(page)

    assert page.get_by_text(re.compile("doesn't work", re.I)).first.is_visible()
    link = page.get_by_role('link', name=re.compile('request a new link', re.I)).first
    assert link.is_visible()
    link.click()
    page.wait_for_url(lambda url: '/forgot-password' in url, timeout=10_000)


def test_the_login_page_links_to_recovery(page):
    """Recovery has to be reachable from the page somebody is stuck on."""
    _sign_out(page)
    visit(page, '/login')

    link = page.get_by_role('link', name=re.compile('forgotten your password', re.I)).first
    assert link.is_visible()
    link.click()
    page.wait_for_url(lambda url: '/forgot-password' in url, timeout=10_000)


# ---------------------------------------------------------------------------
# Account settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name, width, height', VIEWPORTS)
def test_the_settings_page_does_not_scroll_sideways(signed_in, name, width, height):
    """The token table is the risk here — a seven-column table on a 375px phone.

    It is allowed to scroll *inside* its own `.ds-table-wrap`; the document is
    not, which is exactly the distinction `assert_no_horizontal_overflow` draws.
    """
    page = signed_in
    page.set_viewport_size({'width': width, 'height': height})
    page.goto('/settings', wait_until='load')
    assert_no_horizontal_overflow(page, f' [/settings @ {name} {width}px]')


def test_the_settings_page_shows_the_account_and_its_controls(signed_in):
    page = signed_in
    visit(page, '/settings')

    # Located through the definition list rather than by bare text: `get_by_text`
    # on a three-letter username matches inside script tags and the profile
    # menu, and `.first` then lands on whichever of those the DOM happens to
    # order first.
    assert page.locator('dd', has_text=USERNAME).first.is_visible()
    assert page.locator('input[name="current_password"]').is_visible()
    assert page.locator('input[name="password"]').is_visible()
    assert page.get_by_role(
        'button', name=re.compile('sign out everywhere', re.I)).first.is_visible()


def test_creating_a_token_shows_it_exactly_once(signed_in):
    """"Shown once" has to be visible on screen, then gone on reload.

    Only a hash is stored, so this is a fact about the system rather than a
    policy about the page — and a page that could redisplay it would be reading
    it from somewhere it must not exist.
    """
    page = signed_in
    visit(page, '/settings')

    page.fill('input[name="name"]', 'Browser test token')
    page.click('button:has-text("Create token")')
    page.wait_for_load_state('load')

    shown = page.locator('#new-token')
    assert shown.count() == 1, 'the plaintext token was not shown'
    assert shown.first.is_visible()
    assert shown.first.input_value().startswith('dgh_')

    visit(page, '/settings')
    assert page.locator('#new-token').count() == 0, (
        'the token was shown again on reload')


def test_changing_the_password_keeps_this_browser_signed_in(open_registration_page):
    """The re-stamp, seen from the browser that made the change.

    Without it, a successful password change redirects the person who made it to
    the login page — which reads as a bug and teaches exactly the wrong lesson
    about what just happened.

    ## Why this runs against its own server

    It cannot use the shared one, and the reason is the feature itself. Changing
    a password raises `session_version`, which invalidates every credential
    issued under the old value — including the cookies in conftest's
    **session-scoped** `auth_cookies` fixture, which every other browser test
    reuses. Running this against the seeded account signs the entire rest of the
    suite out, and the failures land in files that have nothing to do with
    identity.

    Changing the password back does not help: the restore is a second bump, so
    the cached cookie is two generations stale rather than one. There is no
    version of this test that both exercises the real feature and leaves a
    shared session intact, because "leaves other sessions intact" is exactly
    what the feature must not do.

    So it registers its own account on the open-registration server and changes
    that one's password. Nothing else reads it.
    """
    page = open_registration_page
    password, new_password = 'correct7horse', 'temporary9passphrase'

    page.goto('/register', wait_until='load')
    page.fill('input[name="username"]', 'pwchanger')
    page.fill('input[name="email"]', 'pwchanger@example.com')
    page.fill('input[name="password"]', password)
    page.fill('input[name="confirm"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(lambda url: '/register' not in url, timeout=10_000)

    page.goto('/settings', wait_until='load')
    page.fill('input[name="current_password"]', password)
    page.fill('input[name="password"]', new_password)
    page.fill('input[name="confirm"]', new_password)
    page.click('button:has-text("Change password")')
    page.wait_for_load_state('load')

    assert '/login' not in page.url, (
        'changing the password signed out the browser that changed it')
    page.goto('/transactions', wait_until='load')
    assert '/login' not in page.url


# ---------------------------------------------------------------------------
# A second application, with registration open
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def open_registration_server(tmp_path_factory):
    """A live server whose `ALLOW_REGISTRATION` is on.

    Separate from the session-wide server because the default — and the state
    the shared server is deliberately in — is *closed*, and the closed page is
    itself under test above. Flipping the shared application's config would make
    the two tests order-dependent.

    Module-scoped, so the extra server and its database are paid for once by the
    two tests that need them rather than per test.
    """
    import socket
    import threading

    from werkzeug.serving import make_server

    from app import create_app
    from dough.ai import EchoAdapter

    db_path = tmp_path_factory.mktemp('open-reg') / 'open.db'
    app = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'ALLOW_REGISTRATION': True,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
        'SQLALCHEMY_ENGINE_OPTIONS': {'connect_args': {'check_same_thread': False}},
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': EchoAdapter(configured=False),
    })

    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address[0], server.server_address[1]
    deadline_ok = False
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                deadline_ok = True
                break
        except OSError:
            time.sleep(0.05)
    assert deadline_ok, 'the open-registration server never came up'

    try:
        yield f'http://{host}:{port}'
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def open_registration_page(context, open_registration_server):
    """A page pointed at the open-registration server.

    Its own browser context, so no cookie from the shared server's session
    follows it — `/register` redirects a signed-in visitor to the dashboard,
    which would make these tests pass without touching the form.
    """
    page = context.new_page()
    page.set_default_timeout(10_000)
    original_goto = page.goto
    page.goto = lambda path, **kw: original_goto(
        open_registration_server + path if path.startswith('/') else path, **kw)
    try:
        yield page
    finally:
        page.close()
