"""The way *out* of the signed-out pages.

The auth shell is six templates that share one stylesheet and one problem:
they are reached by people who may not have chosen to be there — every
involuntary sign-out lands on `/login`, an invitation link lands a stranger on
`/join`, and a mail client lands somebody on `/reset-password` — and until this
phase not one of them offered a route to `/`. The mascot above the card, which
is the first thing anybody clicks when they want to get out, was an inert
`<span>` on all six.

`tests/test_auth_feedback.py` covers what those pages *say* when somebody
arrives. This file covers whether there is anywhere to go afterwards.

## The two rules, and the pages they exclude

**Branding links home** wherever Dough is branding: above or beside a form. Not
on the notice branches (a spent reset link, a closed instance, a bad
invitation), where he is drawn "concerned" or "thinking" *about the message*
rather than standing in for the product, and where a primary action already
exists.

**"Back to home"** wherever somebody might decide not to proceed and leaving
costs nothing. That excludes two pages, both deliberately, and both asserted
below rather than left to a comment:

  * `/setup` — a fresh install has no accounts, so `/` redirects straight back
    to `/setup`. A link home that lands you where you started is worse than
    none.
  * `/reset-password` — the link that got somebody there is spent by the time
    the form renders, so leaving means asking for a new email. The branding
    stays clickable, because a logo is not an invitation to leave; this is.

## Why so much of this is markup-level

These pages do not extend `base.html` and so are not on
`tests/test_ui_invariants.py`'s ledger — see the note there. That exemption is
about *colour tokens*, not about the rest of the design system, so the
invariants they can still be held to (no inline styles, no literal colours
outside the token block, links that clear the contrast floor, one
implementation rather than six) are held to here instead of nowhere.

Nothing below touches authentication. The sign-in test near the end exists only
to prove the added markup did not break the form it now surrounds.
"""

import html as html_mod
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.contrast import contrast, parse_hex

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'templates'
AUTH_CSS = ROOT / 'static' / 'css' / 'auth.css'

PASSWORD = 'hunter2boat'

#: Every page that renders on the auth shell. A new one belongs on this list.
AUTH_SHELL = [
    'login.html', 'register.html', 'join.html', 'setup.html',
    'forgot_password.html', 'reset_password.html', 'verified.html',
    'error.html', '_auth_brand.html', '_auth_back.html', '_auth_flash.html',
]

#: The smallest ratio WCAG AA accepts for text this size. One of these colours
#: clears it by 0.01, which is exactly why it is asserted rather than eyeballed.
CONTRAST_FLOOR = 4.5


@pytest.fixture()
def app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'ALLOW_REGISTRATION': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    from models import db
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Reaching each page
# ---------------------------------------------------------------------------
#
# Every fetcher below takes the application and returns rendered HTML. Three of
# these pages cannot be reached by asking for a URL — `/join` needs an
# invitation, `/reset-password` needs a token that the GET then spends, and
# `/setup` refuses to render once any account exists — so the arrangements are
# here, once, rather than inside the tests that parametrize over them.

def _own_account(app, username='sal'):
    """The first account, through `/setup`, which is what a fresh install does.

    Returns the *signed-in* client. Pages are then fetched with a second,
    anonymous client, because several of them render something else entirely
    for a visitor who already has a session.
    """
    client = app.test_client()
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD})
    return client


def _stranger(app):
    return app.test_client()


def _page_login(app):
    _own_account(app)
    return _stranger(app).get('/login').get_data(as_text=True)


def _page_register(app):
    return _stranger(app).get('/register').get_data(as_text=True)


def _page_setup(app):
    """No account is created first: `/setup` redirects to `/login` once one
    exists, and a fresh install is the only state in which it renders."""
    return _stranger(app).get('/setup').get_data(as_text=True)


def _page_forgot_password(app):
    return _stranger(app).get('/forgot-password').get_data(as_text=True)


def _page_join(app):
    from dough.services.membership import issue_invite
    from dough.tenancy import tenant_scope
    from models import AppUser, ROLE_MEMBER

    _own_account(app)
    owner = AppUser.query.filter_by(username='sal').first()
    with tenant_scope(owner.household_id):
        _invite, token = issue_invite(owner.household_id, owner,
                                      role=ROLE_MEMBER, label='for Jamie')
    return _stranger(app).get(f'/join/{token}').get_data(as_text=True)


def _page_join_invalid(app):
    """Unknown, expired, revoked and already-used all render this, and none of
    them says which — see the route. It is the one notice branch that offers no
    action of its own, which is why it is the one that gained a way home."""
    return _stranger(app).get('/join/' + 'x' * 43).get_data(as_text=True)


def _page_reset_password(app):
    from dough.services import identity
    from models import AppUser, PURPOSE_PASSWORD_RESET, db

    _own_account(app)
    user = AppUser.query.filter_by(username='sal').first()
    # `/setup` takes no address -- the first owner is created before anything
    # can be mailed to them -- and `issue_token` refuses an account with none,
    # since a reset link that cannot be delivered is not a reset link.
    user.email = 'sal@example.com'
    db.session.commit()
    _row, token = identity.issue_token(user, PURPOSE_PASSWORD_RESET)
    # The GET spends the token. That is deliberate and documented on the route;
    # it means one fetch per test, which is what this is.
    return _stranger(app).get(f'/reset-password/{token}').get_data(as_text=True)


PAGES = {
    'login': _page_login,
    'register': _page_register,
    'join': _page_join,
    'join-invalid': _page_join_invalid,
    'forgot-password': _page_forgot_password,
    'reset-password': _page_reset_password,
    'setup': _page_setup,
}

#: Dough is the page's branding here, so he is a link home.
BRANDED = ['login', 'register', 'join', 'forgot-password', 'reset-password']

#: A form somebody may decide not to fill in, on a page where leaving is free.
OFFERS_A_WAY_BACK = ['login', 'register', 'join', 'join-invalid',
                     'forgot-password']

#: The two exclusions, each for its own reason. See the module docstring.
NO_WAY_BACK = ['reset-password', 'setup']


# ---------------------------------------------------------------------------
# Reading links out of the page
# ---------------------------------------------------------------------------

class _Link:
    """One `<a>`: where it goes, what it is called, and what it contains."""

    def __init__(self, href, classes, inner):
        self.href = href
        self.classes = classes
        self.inner = inner

    @property
    def text(self):
        """The accessible name, near enough.

        Tags dropped, entities resolved, whitespace collapsed — which is what a
        screen reader is left with, and therefore the thing worth asserting on.
        `aria-hidden` children are *not* removed: the one these pages have is
        checked directly by its own test, and stripping them here would hide
        the case where somebody hides a whole link's text by accident.
        """
        return re.sub(r'\s+', ' ',
                      html_mod.unescape(re.sub(r'<[^>]*>', ' ', self.inner))).strip()


class _LinkReader(HTMLParser):
    """Every anchor in a document, with its markup intact.

    A parser rather than one regex per test: the brand link wraps three nested
    elements, and `<a[^>]*>(.*?)</a>` cannot be trusted to find the right
    closing tag once anything nests. This also lets a test say "the mascot is
    inside the link" and have that mean what it says.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.links = []
        self._open = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        if tag == 'a' and self._open is None:
            attrs = dict(attrs)
            self._open = _Link(attrs.get('href', ''),
                               (attrs.get('class') or '').split(), '')
            self._depth = 0
            return
        if self._open is not None:
            if tag == 'a':
                self._depth += 1
            self._open.inner += raw

    def handle_startendtag(self, tag, attrs):
        if self._open is not None:
            self._open.inner += self.get_starttag_text()

    def handle_endtag(self, tag):
        if self._open is None:
            return
        if tag == 'a' and self._depth == 0:
            self.links.append(self._open)
            self._open = None
            return
        if tag == 'a':
            self._depth -= 1
        self._open.inner += f'</{tag}>'

    def handle_data(self, data):
        if self._open is not None:
            self._open.inner += data

    def handle_entityref(self, name):
        if self._open is not None:
            self._open.inner += f'&{name};'

    def handle_charref(self, name):
        if self._open is not None:
            self._open.inner += f'&#{name};'


def _links(markup, css_class=None):
    reader = _LinkReader()
    reader.feed(markup)
    return [link for link in reader.links
            if css_class is None or css_class in link.classes]


def _only(markup, css_class):
    found = _links(markup, css_class)
    assert len(found) == 1, (
        f'expected exactly one .{css_class} on the page, found {len(found)}')
    return found[0]


# ---------------------------------------------------------------------------
# 1. The branding goes home
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('page', BRANDED)
def test_the_branding_is_a_link_to_the_public_page(app, page):
    """`/` — the same endpoint the landing page's own brand link uses.

    Not `/landing`, which does not exist: `/` serves the marketing page to a
    visitor with no session and the dashboard to one who kept theirs, and
    "home" means whichever of those you actually have.
    """
    assert _only(PAGES[page](app), 'auth-brand').href == '/'


@pytest.mark.parametrize('page', BRANDED)
def test_the_branding_link_says_where_it_goes(app, page):
    """dough.js renders the mascot `aria-hidden` (see its header comment), so
    without text of its own this link is announced as "link" and nothing else.

    The name holds whether or not the wordmark is visible — register.html hides
    it, because "Hi! I'm Dough." is the next thing on the page — which is the
    reason both halves are always in the markup.
    """
    name = _only(PAGES[page](app), 'auth-brand').text.lower()
    assert 'dough' in name, f'the brand link on /{page} has no accessible name'
    assert 'home' in name, (
        f'the brand link on /{page} is named "{name}" — a logo link should say '
        f'where it goes, not only what it is')


@pytest.mark.parametrize('page', BRANDED)
def test_the_mascot_is_inside_the_link_not_beside_it(app, page):
    """The whole lockup is the target, which is what somebody aims at.

    A link around the wordmark alone would pass both tests above while leaving
    the mascot — the largest, most obviously clickable thing on these pages —
    doing nothing.
    """
    brand = _only(PAGES[page](app), 'auth-brand')
    assert 'data-dough=' in brand.inner, (
        f'the mascot on /{page} is not inside the brand link')


@pytest.mark.parametrize('page', sorted(PAGES))
def test_the_way_home_needs_no_javascript(app, page):
    """Every route out is a plain `<a href>`.

    These pages load one deferred script, and the way back must work before it
    arrives, with it blocked, and for anybody who middle-clicks or copies the
    address. A handler would also make the link unannounceable as a link.
    """
    markup = PAGES[page](app)
    for link in _links(markup, 'auth-brand') + _links(markup, 'auth-back__link'):
        assert link.href and not link.href.startswith('#'), (
            f'/{page} has a navigation link with no destination: {link.href!r}')
    assert 'onclick' not in markup, f'/{page} grew an inline handler'


# ---------------------------------------------------------------------------
# 2. "Back to home"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('page', OFFERS_A_WAY_BACK)
def test_a_way_back_is_offered(app, page):
    link = _only(PAGES[page](app), 'auth-back__link')
    assert link.href == '/'
    assert 'back to home' in link.text.lower(), (
        f'the secondary navigation on /{page} reads {link.text!r}')


@pytest.mark.parametrize('page', OFFERS_A_WAY_BACK)
def test_the_way_out_does_not_compete_with_the_form(app, page):
    """Secondary means secondary. The submit button is the only filled control
    on these pages, and a second one would make the page ask a question it does
    not mean to ask."""
    back = _only(PAGES[page](app), 'auth-back__link')
    assert not ({'auth-btn', 'auth-cta'} & set(back.classes)), (
        f'the way home on /{page} is dressed as a primary action: {back.classes}')


@pytest.mark.parametrize('page', ['login', 'register', 'join',
                                  'forgot-password'])
def test_the_way_back_sits_outside_the_form(app, page):
    """It is navigation away from the page, not one more thing to do with the
    form — and outside the card it also cannot be pushed around by the flash
    banners that stack at the top of it."""
    markup = PAGES[page](app)
    assert markup.rindex('</form>') < markup.index('auth-back__link'), (
        f'the way home on /{page} is inside the form')


def test_the_arrow_is_not_read_out(app):
    """"←" is announced as "left arrow" mid-sentence by some screen readers,
    and the word "Back" already carries the meaning."""
    back = _only(PAGES['login'](app), 'auth-back__link')
    assert 'aria-hidden="true"' in back.inner, (
        'the decorative arrow is exposed to assistive technology')


# ---------------------------------------------------------------------------
# 3. The two pages that deliberately do not get one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('page', NO_WAY_BACK)
def test_the_excluded_pages_offer_no_way_back(app, page):
    """Asserted, not merely commented.

    `/setup` renders on a fresh install where `/` redirects back to `/setup`,
    so a link home is a loop. `/reset-password` renders after its link has been
    spent, so leaving means asking for a new email — an inviting exit there is
    a trap, and the page's own copy already says what to do.

    Both are the kind of exclusion a later reader deletes as an oversight. This
    is what makes that a failing test rather than a silent regression.
    """
    assert not _links(PAGES[page](app), 'auth-back__link'), (
        f'/{page} offers a way home; see the module docstring for why it must '
        f'not')


def test_setup_keeps_its_mascot_inert(app):
    """The same reasoning, one step further: on a fresh install even the logo
    would be a loop, so `/setup` is the one shell page whose Dough is not a
    link."""
    markup = PAGES['setup'](app)
    assert 'data-dough=' in markup, 'the setup page lost its mascot entirely'
    assert not _links(markup, 'auth-brand'), (
        '/setup links home, which on a fresh install redirects back to /setup')


# ---------------------------------------------------------------------------
# 4. A message never replaces the way out
# ---------------------------------------------------------------------------

def _arrive_by_signing_out(client, app):
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD})
    return client.post('/logout')


def _arrive_by_expiring(client, app):
    import time
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD})
    with client.session_transaction() as sess:
        sess['seen_at'] = int(time.time()) - app.config['SESSION_IDLE_SECONDS'] - 1
    return client.get('/settings')


@pytest.mark.parametrize('arrive, kind', [
    (_arrive_by_signing_out, 'auth-flash--ok'),
    (_arrive_by_expiring, 'auth-flash--info'),
])
def test_an_explained_arrival_still_offers_a_way_home(client, app, arrive, kind):
    """The two arrivals `tests/test_auth_feedback.py` taught the page to tell
    apart, checked from the other side: whichever it was, there is still an exit.

    The banner renders *inside* the card and the way home sits outside it, so
    this is the assertion that the two do not displace each other. On a phone
    that is the whole question — a message that pushes the exit off the bottom
    of a 375px screen is a message that removed it.

    Only the presentation class is checked here. The wording is pinned in
    test_auth_feedback.py, and a sentence asserted in two files is a sentence
    that gets changed in one.
    """
    response = arrive(client, app)
    assert response.status_code in (301, 302)
    page = client.get(response.headers['Location']).get_data(as_text=True)

    assert kind in page, f'the arrival was not explained as {kind}'
    assert _only(page, 'auth-brand').href == '/'
    assert _only(page, 'auth-back__link').href == '/'


# ---------------------------------------------------------------------------
# 5. The forms the new markup is wrapped around
# ---------------------------------------------------------------------------

def test_signing_in_still_works(client):
    """The one behavioural assertion in this file, and it is a guard rather
    than coverage: `tests/test_auth.py` owns sign-in. If an added `<a>` had
    landed inside the `<form>` or displaced the submit button, every other test
    above would still pass.
    """
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD})
    client.post('/logout')
    response = client.post('/login', data={'username': 'sal',
                                           'password': PASSWORD})
    assert response.status_code in (301, 302)
    assert response.headers['Location'] == '/'
    with client.session_transaction() as sess:
        assert sess.get('user_id')


def test_the_login_form_still_posts_to_itself(app):
    """No `action`, so the POST goes to `/login?next=…` with the query string
    the redirect put there. An `action` added by hand would drop it and send
    everybody to the dashboard instead of where they were going."""
    form = re.search(r'<form class="auth-card"[^>]*>', PAGES['login'](app))
    assert form and 'action=' not in form.group(0), (
        'the login form now names an action, which discards ?next=')


@pytest.mark.parametrize('page', ['register', 'join', 'forgot-password',
                                  'reset-password'])
def test_the_other_forms_still_carry_their_csrf_field(app, page):
    """The wrapper `<div>` that puts the way home in the right grid column goes
    around the form on two of these pages. This is the cheap check that it went
    *around* rather than *inside* — a token accidentally moved out of the form
    is a 403 for every real user and nothing at all for the test suite, which
    runs with CSRF off outside tests/test_csrf.py and tests/browser/.
    """
    markup = PAGES[page](app)
    form = re.search(r'<form[^>]*>(.*?)</form>', markup, re.S)
    assert form, f'/{page} renders no form'
    assert 'name="_csrf_token"' in form.group(1), (
        f'/{page} carries its CSRF token outside the form it protects')


# ---------------------------------------------------------------------------
# 6. One implementation, not six
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('template, partial', [
    ('login.html', '_auth_brand.html'),
    ('login.html', '_auth_back.html'),
    ('register.html', '_auth_brand.html'),
    ('register.html', '_auth_back.html'),
    ('join.html', '_auth_brand.html'),
    ('join.html', '_auth_back.html'),
    ('forgot_password.html', '_auth_brand.html'),
    ('forgot_password.html', '_auth_back.html'),
    ('reset_password.html', '_auth_brand.html'),
])
def test_the_navigation_comes_from_the_shared_partial(template, partial):
    """Six pages, one markup.

    The accessible name, the destination and the aria-hidden arrow are all
    decisions that have to be made the same way every time, and a copy is how
    one page keeps an old one. The partials are also where the reasoning lives.
    """
    source = (TEMPLATES / template).read_text(encoding='utf-8')
    assert partial in source, f'{template} does not include {partial}'


@pytest.mark.parametrize('kind, marker', [
    ('brand', 'class="auth-brand"'),
    ('back', 'class="auth-back__link"'),
])
def test_nobody_hand_rolls_their_own_copy(kind, marker):
    """The other half of the rule above: the markup exists in exactly one file.

    Including the partial *and* hand-writing a second anchor next to it would
    pass every test above and put two links home on one page.
    """
    written_in = [path.name for path in sorted(TEMPLATES.glob('*.html'))
                  if marker in path.read_text(encoding='utf-8')]
    assert written_in == [f'_auth_{kind}.html'], (
        f'the {kind} markup is written out in {written_in}; it belongs only in '
        f'_auth_{kind}.html')


# ---------------------------------------------------------------------------
# 7. Design-system invariants for the shell these pages share
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('template', AUTH_SHELL)
def test_no_inline_styles_on_the_auth_shell(template):
    """Spacing and alignment belong in auth.css with everything else.

    Three of these pages carried the same `style="text-align:center;
    margin-top:14px"` — a decision no token could reach and no theme could
    follow, repeated until somebody would have had to change it in three
    places. It is `.auth-hint--center` now.
    """
    source = (TEMPLATES / template).read_text(encoding='utf-8')
    source = re.sub(r'\{#.*?#\}', '', source, flags=re.S)
    assert 'style="' not in source, (
        f'{template} styles something inline; auth.css is where that goes')


@pytest.mark.parametrize('template', AUTH_SHELL)
def test_no_tailwind_utilities_on_the_auth_shell(template):
    """The shell never loaded Tailwind and must not start.

    Not a hypothetical borrowed from test_ui_invariants.py: these pages are the
    ones most likely to be edited by somebody used to the migrated templates,
    and a `class="mt-4 text-center"` here would render as nothing at all.
    """
    source = re.sub(r'\{#.*?#\}', '',
                    (TEMPLATES / template).read_text(encoding='utf-8'), flags=re.S)
    hits = re.findall(
        r'class="[^"]*\b((?:m|p)[trblxy]?-\d+|text-(?:xs|sm|lg|xl|center)'
        r'|flex-(?:col|row)|gap-\d+|w-full|rounded-(?:sm|md|lg))\b',
        source)
    assert not hits, f'{template} uses Tailwind utilities: {sorted(set(hits))}'


def test_the_auth_stylesheet_keeps_its_colours_in_the_token_block():
    """auth.css is allowed literal colours in `:root` and nowhere else.

    That exemption is the reason these pages are off test_ui_invariants.py's
    ledger: they have no runtime theme engine, so the four base tokens are
    pinned by hand (see the header comment in the file). It is an exemption for
    the *token block*. A colour written into a rule further down is the thing
    the ledger's rule exists to stop, and it is not covered by that reasoning.
    """
    css = re.sub(r'/\*.*?\*/', '', AUTH_CSS.read_text(encoding='utf-8'), flags=re.S)
    root = re.search(r':root\s*\{.*?\n\}', css, re.S)
    assert root, 'auth.css no longer has a :root token block'
    rules = css[:root.start()] + css[root.end():]
    stray = sorted(set(re.findall(r'#[0-9a-fA-F]{3,8}\b', rules)))
    assert not stray, (
        f'literal colours outside the token block: {stray}. Add a token to '
        f':root and reference it, so the pinned Light palette stays in one '
        f'place.')


@pytest.mark.parametrize('ink, paper, where', [
    ('--auth-fg', '--auth-bg', 'the wordmark and "Back to home", on the page'),
    ('--auth-accent-ink', '--auth-bg', 'those two on hover'),
    ('--auth-accent-ink', '--auth-panel', 'the hint links, on the card'),
])
def test_every_link_colour_clears_the_contrast_floor(ink, paper, where):
    """Read from the stylesheet, not from a list of hexes kept here.

    These are small text — .78rem and .8rem — so the 4.5:1 floor applies with
    no large-text concession. The accent ink clears it by 0.01 on the page
    background, which is exactly why this is asserted rather than eyeballed:
    it is one nudge of the palette away from failing, and nothing else would
    report it.
    """
    css = AUTH_CSS.read_text(encoding='utf-8')

    def token(name):
        found = re.search(rf'{name}:\s*(#[0-9a-fA-F]{{3,8}})\s*;', css)
        assert found, f'{name} is not a literal colour in auth.css any more'
        return parse_hex(found.group(1))

    ratio = contrast(token(ink), token(paper))
    assert ratio >= CONTRAST_FLOOR, (
        f'{where}: {ink} on {paper} is {ratio:.2f}:1, under {CONTRAST_FLOOR}:1')
