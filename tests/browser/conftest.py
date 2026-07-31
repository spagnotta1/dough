"""Browser-level regression baseline — the half of the UI that files cannot show.

``tests/test_ui_invariants.py`` reads templates and stylesheets, and it is good
at what it does: it can prove a page uses ``.ds-btn``, defines no literal
colours, and shadows no design-system token. What it cannot do is prove that the
button is on screen, that the dialog centred, that the composer is not sitting
underneath the on-screen keyboard, that the sidebar drawer actually opened, or
that nothing threw during a soft navigation. Those are properties of a rendered
page in a real engine, and the only way to assert them is to render it.

Phase 9 pulled this forward, ahead of the remaining migration waves, on the
reasoning that the waves still to come are the ones most likely to introduce
exactly these regressions — and a baseline established *after* the migrations is
a baseline that recorded whatever the migrations broke.

── How this differs from the rest of the suite ──
Everything else runs against ``app.test_client()``, with ``AUTH_ENABLED`` and
``CSRF_ENABLED`` off (see ``config.TestingConfig``, which explains why). These
run against a real socket with **both turned on**, because that is the
configuration a person actually meets, and because a login form whose CSRF token
never reaches the server is a bug you can only find by submitting the form.

── Cost ──
One Flask server and one browser for the whole session; the per-test cost is a
fresh browser context. The database is seeded once and shared, so tests here
read but do not mutate shared rows. A test that needs to mutate should create
what it mutates.
"""

import socket
import threading
from datetime import datetime
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items):
    """Mark everything under tests/browser/ so `-m "not browser"` works.

    The browser suite is roughly half the wall-clock of the whole run. It should
    be part of the default run — a gate nobody runs is not a gate — but it also
    has to be possible to skip while iterating on something unrelated.
    """
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.browser)

# Skips the entire directory when the browser stack is absent, rather than
# failing collection. The other 633 tests must not depend on a 130MB download.
pytest.importorskip(
    'playwright.sync_api',
    reason='browser tests need: pip install pytest-playwright && python -m playwright install chromium')

from werkzeug.serving import make_server   # noqa: E402

from app import create_app                 # noqa: E402
from dough.ai import EchoAdapter           # noqa: E402
from dough.auth import hash_password       # noqa: E402
from dough.tenancy import tenant_scope     # noqa: E402
from models import AppUser, ROLE_OWNER, Transaction, db   # noqa: E402

USERNAME = 'sal'
PASSWORD = 'hunter2boat'

#: The seeded ledger. Shared by every browser test, so tests read it and do not
#: mutate it — anything that needs to change a row should create its own.
#:
#: The last two are deliberately Uncategorized: /rules disables its Analyse
#: button when nothing is uncategorised, and `/rules/ai-suggest` returns early
#: with an empty list, so with a fully-categorised ledger the entire AI half of
#: that page would be untestable.
TRANSACTIONS = [
    ('Checking', '2026-07-02', "Trader Joe's",             '-84.21',   'Groceries'),
    ('Checking', '2026-07-03', 'Rent — 44 Elm St Apt 3B',  '-2150.00', 'Housing'),
    ('Checking', '2026-07-05', 'Paycheck',                 '4210.55',  'Income'),
    ('Checking', '2026-07-09', 'Con Edison',               '-118.40',  'Utilities'),
    ('Visa',     '2026-07-11', 'Delta Air Lines 0062119',  '-612.80',  'Travel'),
    ('Visa',     '2026-07-14', 'Netflix',                  '-15.99',   'Subscriptions'),
    ('Visa',     '2026-07-18', 'Whole Foods Market',       '-141.07',  'Groceries'),
    ('Checking', '2026-07-21', 'Transfer to savings',      '-500.00',  'Transfer'),
    ('Visa',     '2026-07-16', 'STARBUCKS STORE 8891',     '-6.45',    'Uncategorized'),
    ('Visa',     '2026-07-19', 'SQ *UNKNOWN VENDOR',       '-31.00',   'Uncategorized'),
]

TRANSACTION_COUNT = len(TRANSACTIONS)
UNCATEGORIZED_COUNT = sum(1 for t in TRANSACTIONS if t[4] == 'Uncategorized')


# ── The application under test ──────────────────────────────────────────────

def _seed(app):
    """The owner account, and enough money to make the pages non-empty.

    A dashboard with no transactions renders empty states, and an empty state is
    not what these tests are here to check — a table that overflows its column
    only overflows once there is a row in it.
    """
    household_id = app.config['DEFAULT_HOUSEHOLD_ID']
    with app.app_context():
        # Attached to the household `create_app` seeds under TESTING rather than
        # to one of its own, so the transactions below are visible to this user.
        db.session.add(AppUser(username=USERNAME,
                               password_hash=hash_password(PASSWORD),
                               household_id=household_id,
                               role=ROLE_OWNER))
        db.session.commit()

        from decimal import Decimal
        with tenant_scope(household_id):
            for account, when, desc, amount, category in TRANSACTIONS:
                db.session.add(Transaction(
                    account_name=account,
                    date=datetime.strptime(when, '%Y-%m-%d').date(),
                    description=desc, amount=Decimal(amount),
                    category=category, source='manual'))
            db.session.commit()


@pytest.fixture(scope='session')
def live_server(tmp_path_factory):
    """A real HTTP server on an ephemeral port, for the whole session.

    Threaded, and that is not incidental: ``/chat`` answers over a streaming
    response, and a single-threaded server would sit inside that stream unable to
    serve the stylesheet the same page is still asking for.

    Threaded *plus* a file-backed SQLite database is what makes the explicit
    ``check_same_thread: False`` necessary — the connection pool hands one
    connection to whichever worker thread asks, and pysqlite refuses that by
    default.
    """
    db_path = tmp_path_factory.mktemp('browser') / 'browser.db'
    app = create_app(test_config={
        'TESTING': True,
        # Both on. See the module docstring.
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
        'SQLALCHEMY_ENGINE_OPTIONS': {'connect_args': {'check_same_thread': False}},
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
        # Configured, so the chat route streams a deterministic answer instead of
        # rendering the "no API key" state. Still never touches the network.
        'AI_ADAPTER': EchoAdapter(configured=True),
    })
    _seed(app)

    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address[0], server.server_address[1]
    _wait_until_listening(host, port)
    try:
        yield LiveServer(f'http://{host}:{port}', app)
    finally:
        server.shutdown()
        thread.join(timeout=5)


class LiveServer:
    """Where the server is, and the application object behind it.

    The application comes along because a browser test sometimes has to arrange
    what the server will say — chat is the case that matters: the only way to
    get a chart into an answer is for the model to emit one, and the model here
    is an `EchoAdapter` whose replies a test can queue up in advance.
    """

    def __init__(self, url, app):
        self.url = url
        self.app = app

    @property
    def ai(self):
        """The EchoAdapter serving this app. `.scripted` is a reply queue."""
        return self.app.config['AI_ADAPTER']


def _wait_until_listening(host, port, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f'live server never came up on {host}:{port}')


@pytest.fixture(scope='session')
def base_url(live_server):
    """Overrides pytest-base-url's fixture so `page.goto('/rules')` resolves."""
    return live_server.url


@pytest.fixture()
def ai(live_server):
    """The model, with its reply queue emptied first.

    Draining `scripted` in setup rather than teardown means a test that queued a
    reply and then failed before it was consumed cannot hand that reply to
    whatever runs next — which would fail somewhere else entirely and send the
    reader to the wrong file.
    """
    adapter = live_server.ai
    adapter.scripted.clear()
    return adapter


# ── Global checks that run on every browser test ────────────────────────────

# Console noise that is the harness talking, not the application. Kept
# deliberately short: every entry here is a class of real bug this suite has
# agreed to stop seeing, so an addition needs a reason written next to it.
IGNORED_CONSOLE = (
    # Chromium reports a missing favicon on the console as a failed resource
    # load. Not a page defect, and not something a template can fix.
    'favicon.ico',
)


class PageHealth:
    """The escape hatch for tests that provoke a failure on purpose.

    A test that exercises an error path makes the server return 500 deliberately,
    and the guard below is right to notice. Rather than loosening the guard for
    everybody, such a test asks for the `page_health` fixture by name and says
    which endpoint it expects to break:

        def test_...(signed_in, page_health):
            page_health.expect_server_error('/rules/ai-suggest')

    Narrow by URL fragment, so a 500 from anywhere else still fails the test —
    which matters, because "I expected an error" is exactly the state in which a
    second, unexpected error is easiest to miss.
    """

    def __init__(self):
        self.allowed = []

    def expect_server_error(self, url_fragment):
        self.allowed.append(url_fragment)

    def expect_error_status(self, url_fragment):
        """Tolerate a deliberate 4xx on a document load.  [Phase 10.5]

        Same mechanism as `expect_server_error`, named apart because the two
        mean different things and a reader should not have to decide which.
        A 5xx is the application breaking on purpose for a test; a 4xx here is
        the application *working* — `/register` on a closed instance answers 403
        with a page explaining why, and `/reset-password/<bad>` answers 404 with
        a page offering a new link.

        Chromium logs any non-2xx document load as a console error, so without
        this a correctly-behaving refusal fails the health guard. Still narrow by
        URL fragment, so an unexpected status from anywhere else still fails.
        """
        self.allowed.append(url_fragment)

    def tolerates(self, url):
        return any(fragment in (url or '') for fragment in self.allowed)


@pytest.fixture(autouse=True)
def page_health(page, live_server):
    """Fails any test during which the page threw, logged an error, or 5xx'd.

    Autouse on purpose. The value of these three checks is that no one has to
    remember them: a test written to click a button gets "…and nothing caught
    fire while you did" for free, which is most of what a browser suite is for.

    Uncaught exceptions are separated from console errors because they are a
    different severity — `console.error` is something the code chose to say,
    while a `pageerror` is a stack that escaped. Both fail; the message says
    which.

    Reporting note: because the check can only run once the test is over, a
    failure here is reported by pytest as an ERROR at teardown rather than as a
    failed test, and the summary line reads `12 passed, 12 errors`. The run
    still exits non-zero. Do not read the "passed" and stop.
    """
    health = PageHealth()
    #: (description, url) — the url is kept so the allow-list can be applied at
    #: teardown, by which time the test has had its chance to declare one.
    problems = []
    origin = live_server.url

    def ours(url):
        """Whether a failure is the application's to answer for.

        base.html loads Alpine and Chart.js from public CDNs (Tailwind's is
        gone; Plaid Link is a third on /connections). A full run is roughly a
        hundred page loads, so across a suite those requests occasionally
        fail — and when one does, `net::ERR_CONNECTION_FAILED` arrives on the
        console and fails a test that has nothing to do with it. That
        happened to the login journey on the first full run of this file.

        A gate that goes red when jsdelivr has a bad minute is not a gate, so
        third-party failures are not counted. First-party ones still are, and
        that is the line: this suite is responsible for what this server serves.
        """
        return url is None or url.startswith(origin)

    def on_console(msg):
        if msg.type != 'error':
            return
        if any(s in msg.text for s in IGNORED_CONSOLE):
            return
        # A failed request logs here too, and its text carries the status but not
        # the URL — that is on msg.location, which is why it is kept. It also
        # goes into the message, because "failed to load resource" names nothing.
        url = (msg.location or {}).get('url')
        problems.append((f'console.error: {msg.text}' + (f'  [{url}]' if url else ''), url))

    def on_page_error(exc):
        problems.append((f'uncaught exception: {exc}', None))

    def on_response(response):
        # 4xx is left alone: the suite deliberately provokes some (a bad login
        # is a 200-with-error here, but a rejected fetch is not), and a 404 on a
        # deliberately-missing resource is a test's business. A 5xx is never
        # anybody's business unless the test said so — see PageHealth.
        if response.status >= 500:
            problems.append((f'HTTP {response.status} from {response.url}', response.url))

    page.on('console', on_console)
    page.on('pageerror', on_page_error)
    page.on('response', on_response)

    yield health

    unexpected = [text for text, url in problems
                  if ours(url) and not health.tolerates(url)]
    assert not unexpected, (
        'the page reported problems during this test:\n  '
        + '\n  '.join(unexpected))


# ── Helpers ─────────────────────────────────────────────────────────────────

#: Layout has stopped moving: webfonts have resolved and the document's width
#: has read the same on three consecutive animation frames.
#:
#: A fixed sleep was not good enough, and the way it failed is worth keeping in
#: mind. /rules measured 3690px on one run and 1440px on the next with the same
#: 120ms wait — a table inside an `overflow-x: auto` wrapper is briefly wider
#: than the wrapper before the wrapper's own width resolves, and a measurement
#: taken in that window reports overflow that the user never sees. Three frames
#: rather than two because a value that is flapping between two numbers can
#: match itself on any single pair.
_SETTLED = """() => {
  if (document.fonts && document.fonts.status !== 'loaded') return false;
  const w = document.documentElement.scrollWidth;
  const s = window.__dsSettle || (window.__dsSettle = {last: -1, runs: 0});
  s.runs = (w === s.last) ? s.runs + 1 : 0;
  s.last = w;
  return s.runs >= 2;
}"""


def wait_for_layout(page, timeout=5_000):
    page.evaluate('window.__dsSettle = {last: -1, runs: 0}')
    page.wait_for_function(_SETTLED, timeout=timeout)


def assert_no_horizontal_overflow(page, note=''):
    """The document must not scroll sideways.

    Horizontal overflow is the single most common mobile regression and the one
    static analysis is least able to see: it is produced by the *interaction* of
    a min-width, a padding and a viewport, none of which is wrong on its own.

    The assertion is on the document, because a `.ds-table-wrap` that scrolls
    inside itself is correct and must not fail.
    """
    wait_for_layout(page)
    width = page.evaluate('document.documentElement.clientWidth')
    scroll_width = page.evaluate('document.documentElement.scrollWidth')
    if scroll_width <= width + 1:
        return

    # Only elements that nothing clips can be the cause. Without this filter the
    # message leads with the widest thing on the page, which is usually a table
    # sitting correctly inside its scroll wrapper — the one element that is
    # certainly innocent, named first.
    culprits = page.evaluate("""() => {
      const limit = document.documentElement.clientWidth;
      const out = [];
      for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.right <= limit + 1) continue;
        let clipped = false;
        for (let p = el.parentElement; p; p = p.parentElement) {
          const ox = getComputedStyle(p).overflowX;
          if (ox === 'auto' || ox === 'hidden' || ox === 'scroll') { clipped = true; break; }
        }
        if (clipped) continue;
        const cls = typeof el.className === 'string' && el.className.trim()
          ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
        out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls
                 + ' → ' + Math.round(r.right) + 'px');
      }
      return out.slice(0, 6);
    }""")
    detail = ('\nReaching past the edge, clipped by nothing:\n  ' + '\n  '.join(culprits)
              if culprits else
              '\nNo unclipped element reaches past the edge, so the overflow is '
              'coming from a scroll container that has not been given a width to '
              'work against.')
    raise AssertionError(
        f'the page scrolls horizontally{note}: {scroll_width}px of content in a '
        f'{width}px viewport.' + detail)


def visit(page, path, note=''):
    """Navigate and hold the result to the baseline every page must meet."""
    page.goto(path, wait_until='load')
    assert_no_horizontal_overflow(page, note)


def sign_in(page):
    """Sign in through the form, the way a person does.

    Deliberately not a cookie injected into the context: the login form is one
    of the things under test, and a helper that bypassed it would let the whole
    suite keep passing after the form stopped working.
    """
    page.goto('/login', wait_until='load')
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url(lambda url: '/login' not in url, timeout=10_000)


@pytest.fixture(scope='session')
def auth_cookies(browser, live_server):
    """Sign in once for the whole session and keep the cookies.

    Signing in costs a page load and a form round-trip. Paying that in every one
    of the several dozen tests below would make the suite slow enough that
    somebody stops running it, which is the only way a browser suite really
    fails.

    What keeps this honest is that the *login form itself* is still exercised the
    long way, right here — this fixture calls `sign_in`, so a form that stopped
    working takes the whole suite down rather than letting it pass on an injected
    cookie. tests/test_auth_journey.py then asserts the details.

    ── The one thing a test must not do with this  [Phase 10.5] ──
    **Do not change the seeded account's password, and do not sign it out
    everywhere.** Both raise `AppUser.session_version`, which invalidates every
    credential issued under the old value — including these cookies. The next
    test to ask for `signed_in` is then anonymous, and so is every test after it,
    and the failures land in whatever file happens to run next rather than in the
    one that caused them. That is how a password-change test written here turned
    into 75 failures across the theme sweep.

    Changing the password back does not fix it: the restore is a second bump, so
    the cached cookie ends up two generations stale rather than one. There is no
    ordering that works, because "invalidate every other session" is precisely
    what the feature under test has to do.

    A test that needs to exercise credential invalidation must build its own
    application and its own account. `tests/browser/test_identity_journey.py`
    does exactly that, and says why at the test.
    """
    context = browser.new_context(base_url=live_server.url)
    page = context.new_page()
    sign_in(page)
    cookies = context.cookies()
    context.close()
    assert cookies, 'signing in produced no cookies'
    return cookies


@pytest.fixture()
def signed_in(page, auth_cookies):
    """A page carrying a session, ready to navigate anywhere."""
    page.context.add_cookies(auth_cookies)
    return page
