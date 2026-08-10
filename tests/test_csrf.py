"""CSRF, session lifetime, and password storage — the Phase 6 security core.

SEC-0002 was the oldest open finding in `docs/security.md`: this application
had no CSRF protection of any kind, and `SameSite=Lax` (SEC-0001) was doing all
the work alone. Lax does not cover top-level GET navigation, it treats every
subdomain as same-site, and older browsers ignore it entirely, so it was never
a mechanism — it was the absence of one, spelled defensively.

These tests are built to fail for the right reason. The sweep at the bottom
enumerates the URL map rather than listing routes, so a route added in a later
phase is covered the day it exists; and every positive case asserts a *token
that works* alongside its negative, because a CSRF test that only checks
rejection passes just as happily against an endpoint that rejects everything.
"""

import re
import time

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.auth import CSRF_FIELD, CSRF_HEADER, SESSION_CSRF_KEY
from models import db, AppUser

PASSWORD = 'hunter2boat'


@pytest.fixture()
def csrf_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


def _token(client):
    """This client's token, read out of a rendered page.

    Scraped from the HTML rather than lifted from the session on purpose: a
    token the server knows about but never renders is useless to a real
    browser, and every one of these tests would still pass. Following redirects
    matters too — before an account exists /login bounces to /setup, and a
    redirect renders no template and therefore mints no token.
    """
    page = client.get('/login', follow_redirects=True)
    body = page.get_data(as_text=True)
    match = re.search(
        r'name="%s"\s+value="([^"]+)"' % re.escape(CSRF_FIELD), body)
    assert match, 'no CSRF field was rendered into the page'
    with client.session_transaction() as sess:
        assert sess.get(SESSION_CSRF_KEY) == match.group(1), (
            'the rendered token is not the one stored in the session')
    return match.group(1)


@pytest.fixture()
def signed_in(csrf_app):
    """A signed-in client, created through the real setup form."""
    client = csrf_app.test_client()
    token = _token(client)
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD, CSRF_FIELD: token})
    return client


# ---------------------------------------------------------------------------
# The token is required, and it works
# ---------------------------------------------------------------------------

def test_a_post_without_a_token_is_refused(signed_in):
    resp = signed_in.post('/budgets', data={'action': 'delete', 'budget_id': 1})
    assert resp.status_code == 403


def test_a_post_with_the_form_token_is_accepted(signed_in):
    """The positive half. Without it, a route that 403s everything would pass."""
    token = _token(signed_in)
    resp = signed_in.post('/budgets', data={
        'action': 'add', 'category': 'Groceries', 'monthly_limit': '400',
        CSRF_FIELD: token})
    assert resp.status_code != 403


def test_a_post_with_the_header_token_is_accepted(signed_in):
    """The channel `fetch` uses. base.html patches window.fetch to send it."""
    token = _token(signed_in)
    resp = signed_in.post('/rules/test', json={'keyword': 'coffee'},
                          headers={CSRF_HEADER: token})
    assert resp.status_code != 403


def test_safe_methods_need_no_token(signed_in):
    assert signed_in.get('/transactions').status_code == 200


def test_another_sessions_token_is_refused(csrf_app, signed_in):
    """The token is session-bound, so a valid-looking one is not enough.

    This is the assertion that distinguishes a real check from one that merely
    tests whether the field is non-empty.
    """
    stranger = csrf_app.test_client()
    resp = signed_in.post('/budgets', data={
        'action': 'delete', 'budget_id': 1, CSRF_FIELD: _token(stranger)})
    assert resp.status_code == 403


def test_signing_in_rotates_the_token(csrf_app):
    """Session fixation: a token minted before sign-in must not survive it."""
    client = csrf_app.test_client()
    before = _token(client)
    client.post('/setup', data={'username': 'sal', 'password': PASSWORD,
                                'confirm': PASSWORD, CSRF_FIELD: before})
    with client.session_transaction() as sess:
        assert sess.get(SESSION_CSRF_KEY) != before

    resp = client.post('/budgets', data={'action': 'delete', 'budget_id': 1,
                                         CSRF_FIELD: before})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Origin / Sec-Fetch-Site — the second signal
# ---------------------------------------------------------------------------

def test_a_foreign_origin_is_refused_even_with_a_valid_token(signed_in):
    """Defence in depth against a token that leaked some other way."""
    token = _token(signed_in)
    resp = signed_in.post('/budgets',
                          data={'action': 'delete', 'budget_id': 1,
                                CSRF_FIELD: token},
                          headers={'Origin': 'https://evil.example'})
    assert resp.status_code == 403


def test_a_cross_site_fetch_is_refused(signed_in):
    token = _token(signed_in)
    resp = signed_in.post('/budgets',
                          data={'action': 'delete', 'budget_id': 1,
                                CSRF_FIELD: token},
                          headers={'Sec-Fetch-Site': 'cross-site'})
    assert resp.status_code == 403


def test_a_missing_origin_is_not_treated_as_evidence(signed_in):
    """Absence proves nothing, so it must fall through to the token check.

    Getting this wrong is how a CSRF layer breaks legitimate clients: several
    browsers omit Origin on same-origin requests, and Sec-Fetch-Site does not
    exist at all before 2020.
    """
    token = _token(signed_in)
    resp = signed_in.post('/rules/test', json={'keyword': 'x'},
                          headers={CSRF_HEADER: token})
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Public routes are not exempt
# ---------------------------------------------------------------------------

def test_login_itself_requires_a_token(csrf_app, signed_in):
    """An unprotected /login is a real attack, not a technicality.

    Forging it signs the victim's browser into the *attacker's* account, where
    everything the victim then uploads or connects lands in a ledger the
    attacker can read at leisure. Being `@public` exempts a route from needing
    a session, never from needing a token.
    """
    signed_in.post('/logout', data={CSRF_FIELD: _token(signed_in)})
    fresh = csrf_app.test_client()
    resp = fresh.post('/login', data={'username': 'sal', 'password': PASSWORD})
    assert resp.status_code == 403


def test_setup_requires_a_token(csrf_app):
    resp = csrf_app.test_client().post('/setup', data={
        'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD})
    assert resp.status_code == 403
    assert AppUser.query.count() == 0


# ---------------------------------------------------------------------------
# What the caller is told
# ---------------------------------------------------------------------------

def test_a_json_caller_gets_json(signed_in):
    resp = signed_in.post('/rules/test', json={'keyword': 'x'},
                          headers={'Accept': 'application/json'})
    assert resp.status_code == 403
    assert resp.is_json


def test_a_rejected_form_post_is_not_redirected(signed_in):
    """303-to-GET would lose the refusal and invite a blind retry."""
    resp = signed_in.post('/budgets', data={'action': 'delete', 'budget_id': 1},
                          headers={'Sec-Fetch-Mode': 'navigate'})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Every unsafe route, enumerated
# ---------------------------------------------------------------------------

def _unsafe_rules(app):
    """Every unsafe (path, method) that must carry a token.

    Exempt views are excluded here rather than tolerated in the assertion, so
    the sweep stays a strict "everything else must 403". The exempt set itself
    is pinned by `test_the_csrf_exempt_set_is_exactly_the_api_login`, which is
    where an exemption has to be argued for -- filtering by the marker means a
    view that quietly gained one would vanish from this sweep *and* fail that
    test, rather than silently leaving the sweep alone.
    """
    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is not None and getattr(view, '_dough_csrf_exempt', False):
            continue
        for method in sorted(rule.methods - {'GET', 'HEAD', 'OPTIONS'}):
            values = {name: 1 for name in rule.arguments}
            out.append((rule.build(values, append_unknown=False)[1], method,
                        rule.endpoint))
    return sorted(out)


def test_no_unsafe_route_accepts_a_missing_token(csrf_app, signed_in):
    """The sweep. A route added later is covered the day it exists.

    Enumerating the URL map is the entire point: the mutating route that leaks
    is never the one somebody remembered to write a test for.
    """
    rules = _unsafe_rules(csrf_app)
    # ~34 before Phase 10, ~55 after /api/v1. Raised with the count: a floor
    # every plausible regression clears has stopped guarding anything.
    assert len(rules) >= 50, f'only {len(rules)} unsafe routes found'

    leaked = []
    for path, method, endpoint in rules:
        resp = signed_in.open(path, method=method)
        if resp.status_code != 403:
            leaked.append((method, path, endpoint, resp.status_code))
    assert not leaked, 'accepted without a CSRF token:\n' + '\n'.join(
        f'  {m} {p} -> {code} ({ep})' for m, p, ep, code in leaked)


def test_the_csrf_exempt_set_is_exactly_the_api_login_and_the_plaid_webhook(csrf_app):
    """Two exemptions, and each had to be argued for.  [Phase 10, UAT round 1]

    This was `== set()` from Phase 6 to Phase 9, pinning the set at zero so an
    exemption could not be added as a quick way to make a failing test pass.
    Phase 10 spends it, once, on `api_v1_auth.login`.

    **`finance_sync.api_plaid_webhook`, the second.**  [UAT round 1] The caller
    is Plaid's servers announcing that a linked Item has new data — most
    importantly that its historical backfill has finished, which is the only
    authoritative signal that a connection's transaction history is actually
    complete. There is no browser in this request and no session behind it, so
    there is nothing a CSRF token could be bound to.

    The safety argument is the same shape as the login's, and it has to be made
    separately because this endpoint does not accept a password. What protects
    it is that it *has* its own credential: every accepted request carries an
    ES256 signature over the exact request body, verified against Plaid's
    published key. `finance_sync/plaid_webhook.py` is fail-closed on every path
    — no header, wrong algorithm, unfetchable key, bad signature, stale
    timestamp and mismatched body hash all refuse — so an unsigned cross-site
    POST reaches nothing. That is a strictly stronger check than a CSRF token,
    which only proves the request came from our own page.

    What it is *not* is a precedent for "webhooks are exempt". The exemption is
    paid for by the signature verification, and a second webhook without one
    would not qualify.

    **Why `api_v1_auth.login`, the first.** `@csrf_exempt` was written for "machine
    callers that authenticate some other way", and the API login is the one
    route that cannot participate in CSRF even in principle: there is no session
    yet to bind a token to, because obtaining a credential is what the endpoint
    is for.

    What makes it safe is narrower than "it is an API endpoint". CSRF exists
    because a browser attaches cookies to cross-site requests automatically —
    the credential travels without the attacking page knowing it. This endpoint
    accepts a *password*, which no browser attaches automatically, so a
    cross-site forgery would have to already know the password, at which point
    the forgery buys nothing.

    Every other `/api/v1` route is **not** exempt, and that is load-bearing: the
    web UI calls them with a session cookie, and a blueprint-wide exemption
    would reopen the hole CSRF closes for exactly those calls. Bearer-carrying
    requests skip the check in `app.py` on the strength of the *credential*, not
    the path — see `test_session_authenticated_api_calls_still_need_a_token` in
    tests/test_api_auth.py, which pins that distinction from the other side.
    """
    exempt = {endpoint for endpoint, view in csrf_app.view_functions.items()
              if getattr(view, '_dough_csrf_exempt', False)}
    assert exempt == {'api_v1_auth.login', 'finance_sync.api_plaid_webhook'}


def test_every_post_form_in_the_templates_carries_the_field():
    """The fetch wrapper cannot help a plain form, so this checks them directly.

    A template is not Python, so no import-time check will catch a form that
    forgets the hidden input; it fails as a 403 the first time a user submits
    it. Reading the templates is the only place that failure can be caught
    before a person hits it.
    """
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'templates')
    form_open = re.compile(r'<form\b[^>]*>', re.IGNORECASE | re.DOTALL)
    missing = []
    for name in sorted(os.listdir(root)):
        if not name.endswith('.html'):
            continue
        with open(os.path.join(root, name), encoding='utf-8') as handle:
            text = handle.read()
        for match in form_open.finditer(text):
            if not re.search(r'method\s*=\s*["\']?post', match.group(0), re.I):
                continue
            if 'csrf' not in text[match.end():match.end() + 200].lower():
                line = text[:match.start()].count('\n') + 1
                missing.append(f'{name}:{line}')
    assert missing == [], f'POST forms with no csrf_field(): {missing}'


# ---------------------------------------------------------------------------
# Session lifetime  [SESSION_IDLE_SECONDS / SESSION_ABSOLUTE_SECONDS]
# ---------------------------------------------------------------------------

def test_an_idle_session_expires(csrf_app, signed_in):
    csrf_app.config['SESSION_IDLE_SECONDS'] = 60
    with signed_in.session_transaction() as sess:
        sess['seen_at'] = int(time.time()) - 3600
    resp = signed_in.get('/transactions')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']


def test_a_long_lived_session_expires_even_while_active(csrf_app, signed_in):
    """The idle timer alone can be held open forever by a tab that polls.

    Several pages here do exactly that, so without the absolute limit an
    unattended browser stays signed in indefinitely.
    """
    csrf_app.config['SESSION_ABSOLUTE_SECONDS'] = 3600
    with signed_in.session_transaction() as sess:
        sess['signed_in_at'] = int(time.time()) - 7200
        sess['seen_at'] = int(time.time())
    resp = signed_in.get('/transactions')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']


def test_activity_slides_the_idle_window(csrf_app, signed_in):
    csrf_app.config['SESSION_IDLE_SECONDS'] = 3600
    with signed_in.session_transaction() as sess:
        sess['seen_at'] = int(time.time()) - 1800
    assert signed_in.get('/transactions').status_code == 200
    with signed_in.session_transaction() as sess:
        assert int(time.time()) - sess['seen_at'] < 5


def test_a_session_whose_user_is_gone_signs_out_cleanly(csrf_app, signed_in):
    """Regression: this used to be a 500.

    `_require_login` admitted the request on the strength of the session's user
    id, `_household_for_request` then found no user and bound no household, and
    the first tenant-scoped query raised TenantContextMissing. Unreachable
    before Phase 6 because nothing deleted an AppUser — "remove member" is what
    makes it reachable, so it is fixed alongside that feature rather than after
    somebody meets it.
    """
    db.session.delete(AppUser.query.one())
    db.session.commit()

    resp = signed_in.get('/transactions')
    assert resp.status_code == 302, f'expected a redirect, got {resp.status_code}'
    assert '/login' in resp.headers['Location']
    with signed_in.session_transaction() as sess:
        assert 'user_id' not in sess


# ---------------------------------------------------------------------------
# Password storage  [SEC-0005]
# ---------------------------------------------------------------------------

def test_passwords_are_stored_under_a_memory_hard_kdf(signed_in):
    """SEC-0005 asked for a memory-hard KDF instead of pbkdf2.

    Werkzeug 3's default is already scrypt, so the finding was stale by the
    time this phase reached it — but "it happens to be right today" is not the
    same as "it stays right", and the default has moved before. This pins the
    stored prefix so a dependency upgrade that changed it fails here.
    """
    stored = AppUser.query.one().password_hash
    assert stored.startswith('scrypt:'), stored.split('$')[0]


def test_an_older_hash_is_upgraded_on_the_next_sign_in(csrf_app, signed_in):
    """A stored pbkdf2 hash cannot be upgraded by a migration.

    Re-deriving needs the plaintext, and the only moment that exists is a
    successful sign-in. Anything that has not signed in since keeps its old
    hash, which is a limitation rather than a bug and is recorded as such.
    """
    from werkzeug.security import generate_password_hash

    user = AppUser.query.one()
    user.password_hash = generate_password_hash(PASSWORD, method='pbkdf2:sha256')
    db.session.commit()
    assert user.password_hash.startswith('pbkdf2:')

    signed_in.post('/logout', data={CSRF_FIELD: _token(signed_in)})
    token = _token(signed_in)
    resp = signed_in.post('/login', data={'username': 'sal', 'password': PASSWORD,
                                          CSRF_FIELD: token})
    assert resp.status_code == 302, 'sign-in with the old hash must still work'
    assert AppUser.query.one().password_hash.startswith('scrypt:')
