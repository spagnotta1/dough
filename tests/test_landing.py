"""The public landing page, and the guarantees `@public` on `/` must not break.
[Phase 10.5]

`/` is now the one route in the application that renders two different pages
depending on who is asking. That is a small change to the view and a large one
to what has to be checked, because every guarantee that used to be implied by
"`/` requires a session" now has to be asserted directly:

- An anonymous request must see marketing and **no data**.
- A request whose session has been invalidated must be treated as anonymous,
  not as signed in — `@public` short-circuits `_require_login`, so this is the
  one place the marker could have become a way around the session-lifetime
  check.
- A signed-in request must still see the dashboard.
- The URL surface must not have moved, because `redirect('/')` appears at the
  end of three flows and `url_for('core.dashboard')` appears in every template.

The tenancy assertion is the one worth reading twice. An anonymous request binds
no household, so any tenant query reached on that path raises
`TenantContextMissing` rather than leaking — that is the backstop, and the test
below proves the landing branch returns before ever reaching it.
"""

import re

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

PASSWORD = 'hunter2boat'


@pytest.fixture()
def auth_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
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
def client(auth_app):
    return auth_app.test_client()


def _account(client, username='sal'):
    client.post('/setup', data={'username': username, 'password': PASSWORD,
                                'confirm': PASSWORD})
    return client


def _signed_out(client):
    client.post('/logout')
    return client


# ---------------------------------------------------------------------------
# Which page `/` renders
# ---------------------------------------------------------------------------

def test_a_fresh_install_still_goes_to_setup(client):
    """An installation with no accounts is an unfinished install, not a visit.

    The only useful thing anybody can do is create the first owner, and a
    marketing page whose "Sign in" leads to `/login` leads to `/setup` anyway --
    the same destination with a page in front of it, shown to the person
    installing the software.
    """
    response = client.get('/', follow_redirects=False)
    assert response.status_code == 302
    assert '/setup' in response.headers['Location']


def test_an_anonymous_visitor_sees_the_landing_page(client):
    _signed_out(_account(client))

    response = client.get('/')
    assert response.status_code == 200
    assert b'Your money' in response.data
    assert b'AI financial assistant' in response.data
    assert b'Household collaboration' in response.data


def test_a_signed_in_visitor_still_sees_the_dashboard(client):
    _account(client)

    response = client.get('/')
    assert response.status_code == 200
    assert b'Your money' not in response.data
    # The signed-in chrome, by markup-only markers -- base.html's stylesheet
    # ships with every page, so asserting on class names would pass either way.
    assert b'id="profile-btn"' in response.data
    assert b'id="tab-bar"' in response.data


def test_the_landing_page_drops_the_signed_in_chrome(client):
    """Every nav destination needs a session.

    A stranger would otherwise get seven links that bounce to `/login`, a
    profile menu offering to sign them out of a session they do not have, and --
    on a phone -- a permanent bottom bar of five pages they cannot open,
    covering the page they can.
    """
    _signed_out(_account(client))
    response = client.get('/')

    assert b'id="profile-btn"' not in response.data
    assert b'id="tab-bar"' not in response.data
    assert b'id="mobile-menu"' not in response.data
    # The public header is present in its place.
    assert b'nav-brand' in response.data
    assert b'Sign in' in response.data


# ---------------------------------------------------------------------------
# It shows no data, and cannot
# ---------------------------------------------------------------------------

def test_the_landing_page_binds_no_household(client, auth_app):
    """The tenancy backstop is what makes the early return load-bearing.

    An anonymous request has no household bound, so a tenant query on this path
    would raise rather than leak. This asserts the state directly, so the
    property survives somebody adding a query to the landing branch — they get
    a 500 in the test suite rather than another household's figures on a public
    page.
    """
    from dough.tenancy import current_household

    seen = {}

    # Registered before any request is made. Flask refuses to add a
    # `before_request` to an application that has already served one, so this
    # cannot be moved below the sign-up calls.
    @auth_app.before_request
    def _capture():
        from flask import request
        if request.path == '/':
            seen['household'] = current_household()

    _signed_out(_account(client))
    client.get('/')

    assert 'household' in seen, 'the hook never ran; the test proved nothing'
    assert seen['household'] is None


def test_no_transaction_data_reaches_an_anonymous_visitor(client, auth_app):
    """Anti-vacuity: there is real data, and none of it is on the page."""
    from datetime import date

    from models import Transaction, db

    from dough.tenancy import tenant_scope

    _account(client)
    # Written under an explicit scope: `transactions` is tenant-scoped, so the
    # ORM write guard requires a bound household and this test has no request.
    with tenant_scope(1):
        db.session.add(Transaction(
            account_name='checking', date=date(2026, 7, 1),
            description='SECRET MERCHANT LTD', amount=-42.5,
            category='Groceries', household_id=1))
        db.session.commit()

    _signed_out(client)
    response = client.get('/')
    assert b'SECRET MERCHANT LTD' not in response.data
    assert b'42.5' not in response.data


# ---------------------------------------------------------------------------
# `@public` did not become a hole
# ---------------------------------------------------------------------------

def test_an_expired_session_is_cleared_rather_than_honoured(client, auth_app):
    """The failure `@public` on `/` makes possible.

    `_require_login` short-circuits on a public view, so without the
    session-lifetime call added to that branch, a browser past its absolute
    lifetime would still be handed the dashboard here -- with a bound household
    and real figures on it.
    """
    import time

    _account(client)
    with client.session_transaction() as sess:
        sess['signed_in_at'] = int(time.time()) - 10 ** 7
        sess['seen_at'] = int(time.time()) - 10 ** 7

    response = client.get('/')
    assert response.status_code == 200
    assert b'Your money' in response.data          # the landing page
    assert b'id="profile-btn"' not in response.data
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_the_landing_page_is_not_marked_no_store(client):
    """It holds nothing private, and it is the page a stranger loads first.

    `_no_store_for_authenticated_responses` applies by session rather than by
    route, so this is what says the condition is actually the session and not
    something that happens to be true today.
    """
    _signed_out(_account(client))
    response = client.get('/')
    assert 'no-store' not in response.headers.get('Cache-Control', '')


def test_the_dashboard_is_still_marked_no_store(client):
    """SEC-0008, unchanged: a signed-in page must stay out of bfcache."""
    _account(client)
    response = client.get('/')
    assert 'no-store' in response.headers['Cache-Control']


# ---------------------------------------------------------------------------
# The URL surface did not move
# ---------------------------------------------------------------------------

def test_signing_in_still_lands_on_the_dashboard(client):
    """`redirect('/')` ends sign-in, setup and invitation redemption.

    The landing page shares `/` precisely so those three did not have to change,
    and this is what would fail if `/` had been given to marketing outright.
    """
    _signed_out(_account(client))

    response = client.post('/login', data={'username': 'sal',
                                           'password': PASSWORD})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/')

    landed = client.get('/')
    assert b'id="profile-btn"' in landed.data


def test_the_landing_page_offers_registration_only_when_it_is_open(tmp_path):
    """A "Create account" button leading to "registration is closed" is worse
    than no button -- it advertises a door that is not there."""
    for allowed in (False, True):
        scheduler_module._scheduler = None
        application = create_app(test_config={
            'TESTING': True, 'AUTH_ENABLED': True,
            'ALLOW_REGISTRATION': allowed,
            'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / f'reg{allowed}.db'}",
            'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
        })
        with application.app_context():
            client = application.test_client()
            _signed_out(_account(client))
            body = client.get('/').get_data(as_text=True)
            assert ('Create your account' in body) is allowed
            # Sign in is offered either way -- it is the action that always works.
            assert 'Sign in' in body
        scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# The product preview
# ---------------------------------------------------------------------------

def test_the_product_preview_says_its_figures_are_invented(client):
    """A mock of the dashboard good enough to sell the product is good enough
    to be mistaken for somebody's actual ledger.

    The landing branch binds no household, so there is nothing real on this
    path to leak -- that is asserted above. This is the other half: a visitor
    looking at $184,320 has to be able to tell it is a sample.
    """
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    assert 'lp-preview' in body, 'the product preview is gone from the hero'
    assert 'Sample data' in body, (
        'the dashboard preview no longer labels itself; invented figures on a '
        'finance landing page have to say that they are invented')


def test_the_preview_is_one_sentence_to_a_screen_reader(client):
    """Forty invented numbers read aloud in sequence is not a preview.

    role="img" collapses the whole mock to its label, which is the same thing a
    screenshot would have given -- and the reason the mock may be built from
    live markup without costing anything at the assistive-technology layer.
    """
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    preview = re.search(r'<div class="lp-preview"[^>]*>', body)
    assert preview, 'the preview element changed shape; update this test'
    assert 'role="img"' in preview.group(0)
    assert 'aria-label=' in preview.group(0)


def test_the_preview_holds_no_form_controls_or_links(client):
    """It is a picture. Anything focusable inside it is a tab stop that leads
    nowhere, and role="img" would hide it from a screen reader while leaving it
    reachable by keyboard."""
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    start = body.index('<div class="lp-preview"')
    end = body.index('<!-- ── Trust', start)
    preview = body[start:end]

    for tag in ('<a ', '<button', '<input', '<select', '<textarea', 'tabindex'):
        assert tag not in preview, f'the preview contains a focusable {tag!r}'


# ---------------------------------------------------------------------------
# Social proof is configuration, because it is the one claim nobody can check
# ---------------------------------------------------------------------------

def test_social_proof_renders_nothing_until_it_is_configured(client):
    """The default install has no users to be trusted by.

    Every other claim on this page -- read-only connections, encryption at
    rest, household isolation -- is checkable by reading the repository, which
    is the rule templates/privacy.html is maintained under. "$18M+ in tracked
    assets" is not, and a visitor cannot tell an invented one from a real one.
    So it ships empty, exactly as MARKETING_TESTIMONIALS already did.
    """
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    assert 'As seen in' not in body
    assert 'lp-proof' not in body
    assert 'lp-press' not in body


def test_configured_social_proof_reaches_the_page(tmp_path):
    """Anti-vacuity for the test above: the section is empty because it is
    unset, not because it stopped working."""
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True, 'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'proof.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
        'MARKETING_STATS': [{'value': '1,200', 'label': 'households'}],
        'MARKETING_PRESS': [{'name': 'The Example Post', 'url': 'https://example.test'}],
        'MARKETING_TESTIMONIALS': [{'quote': 'It found a subscription I forgot.',
                                    'name': 'A. Tester'}],
    })
    with application.app_context():
        client = application.test_client()
        _signed_out(_account(client))
        body = client.get('/').get_data(as_text=True)

        assert '1,200' in body and 'households' in body
        assert 'The Example Post' in body and 'As seen in' in body
        assert 'It found a subscription I forgot.' in body
    scheduler_module._scheduler = None


# ---------------------------------------------------------------------------
# The sections a visitor looks for before handing over a bank login
# ---------------------------------------------------------------------------

def test_the_page_answers_the_questions_asked_before_connecting_a_bank(client):
    """These six are what the FAQ exists for, and "can it move my money" is the
    one whose absence costs the most signups."""
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    for question in ('How does Dough connect to my bank?',
                     'Can Dough move my money?',
                     'Is my information secure?',
                     'Which banks are supported?',
                     'Can I disconnect an account?',
                     'Does Dough sell my data?'):
        assert question in body, f'the FAQ no longer answers {question!r}'

    # The two links the same visitor goes looking for next.
    assert '/privacy' in body and '/terms' in body


def test_the_headings_form_a_single_ordered_outline(client):
    """One h1, and no level skipped.

    A screen-reader user navigates a marketing page by heading, and this page
    is long enough that the outline *is* the navigation. It also went wrong the
    obvious way once: the detail sections used h3 with no h2 above them, so
    two-thirds of the page hung off nothing.
    """
    _signed_out(_account(client))
    body = client.get('/').get_data(as_text=True)

    levels = [int(n) for n in re.findall(r'<h([1-6])[\s>]', body)]

    assert levels.count(1) == 1, f'expected exactly one h1, found {levels.count(1)}'
    assert levels[0] == 1, f'the page opens on an h{levels[0]}, not an h1'
    for previous, current in zip(levels, levels[1:]):
        assert current <= previous + 1, (
            f'heading level jumps from h{previous} to h{current}')


# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------

def test_the_landing_stylesheet_uses_no_literal_colours():
    """It extends base.html, so it inherits the runtime theme engine.

    That is the difference from auth.css, which pins the Light palette because
    login/join/setup have no runtime to read a saved preference with. A literal
    colour here would be a landing page that ignores the theme a returning
    visitor chose, and then switches the instant they sign in.
    """
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'static', 'css', 'landing.css')
    with open(path, encoding='utf-8') as handle:
        css = handle.read()

    # Comments hold worked examples and token names; the rules must not.
    rules = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    literals = re.findall(r'#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)', rules)
    assert literals == [], f'literal colours in landing.css: {literals}'


def test_the_landing_page_loads_its_own_stylesheet(client):
    _signed_out(_account(client))
    assert b'css/landing.css' in client.get('/').data
