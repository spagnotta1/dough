"""Why the login page is on screen, said out loud.

A browser reaches `/login` four ways. One is a button somebody pressed; three
are the application deciding for them:

1. Sign out.
2. An idle or absolute session limit ran out.
3. `session_version` moved past what the session was minted under — a password
   changed, here or in another browser.
4. The `AppUser` row is gone: removed from the household while signed in.

Until this phase all four rendered an identical, silent form, and 3 and 4 are
exactly the cases where somebody needs to be told something happened.

## What these tests actually guard

The ordering. Flask keeps flashes *in the session*, and every one of these paths
calls `session.clear()`. Queue the message first and the clear discards it; the
page renders silently and every assertion about "did we call flash" still
passes. So each test below asserts on the **rendered login page**, after
following the redirect — which is the only place the distinction between
`flash()` and `flash()` that survived is visible.

The same trap is why `templates/_auth_flash.html` exists at all: the six
auth-shell pages do not extend base.html, which is where `get_flashed_messages`
was called. A flash landing on one of them used to stay in the session and
surface as a toast on the dashboard after the next sign-in — `/reset-password`
had been doing that since Phase 10.5. `test_reset_confirmation_is_not_deferred`
covers that specific regression.
"""

import html as html_mod
import re
import time

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.auth import SESSION_VERSION_KEY

PASSWORD = 'hunter2boat'


@pytest.fixture()
def app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
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


def _csrf(response):
    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _post(client, path, data, page_path=None):
    token = _csrf(client.get(page_path or path))
    return client.post(path, data={**data, '_csrf_token': token})


def _register(client, username='sal', email='sal@example.com'):
    return _post(client, '/register',
                 {'username': username, 'email': email,
                  'password': PASSWORD, 'confirm': PASSWORD})


def _login_page_after(client, response):
    """Follow a redirect and return the rendered login page.

    Deliberately not `client.get('/login')` as a separate request: a flash is
    consumed by whichever request renders it, so fetching the page separately
    would pass even if the redirect itself had dropped the message.
    """
    assert response.status_code in (301, 302), (
        f'expected a redirect to the login page, got {response.status_code}')
    followed = client.get(response.headers['Location'])
    return followed.get_data(as_text=True)


def _messages(html):
    """The text of every rendered flash banner, as a reader would see it.

    Unescaped, because the template escapes and should: the apostrophe in
    "You've" arrives as `&#39;`. Comparing against the raw markup would make
    every expectation here a statement about HTML entities rather than about
    what the page says. `test_the_message_is_escaped_not_injected` is what
    holds the escaping itself in place.
    """
    return [html_mod.unescape(re.sub(r'\s+', ' ', m)).strip() for m in
            re.findall(r'<p class="auth-flash__msg">(.*?)</p>', html, re.S)]


# ---------------------------------------------------------------------------
# 1. Signing out
# ---------------------------------------------------------------------------

def test_logout_confirms_it_happened(client):
    _register(client)
    html = _login_page_after(client, _post(client, '/logout', {},
                                           page_path='/settings'))
    assert _messages(html) == ["You've been signed out successfully."]
    assert 'auth-flash--ok' in html


def test_logout_still_lands_on_the_login_page(client):
    """The behaviour this phase was told not to change."""
    _register(client)
    response = _post(client, '/logout', {}, page_path='/settings')
    assert response.headers['Location'].endswith('/login')
    # And the session really is gone, not merely re-flashed over.
    assert client.get('/settings').status_code in (301, 302)


# ---------------------------------------------------------------------------
# 2. Expiry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('stamp, limit', [
    ('seen_at', 'SESSION_IDLE_SECONDS'),
    ('signed_in_at', 'SESSION_ABSOLUTE_SECONDS'),
])
def test_expired_session_says_so(app, client, stamp, limit):
    """Both limits produce the same message: nothing is wrong, sign in again.

    The two are separate code paths in `_enforce_session_lifetime` and both
    were silent, so both are parametrised rather than one standing in for the
    other.
    """
    _register(client)
    with client.session_transaction() as sess:
        sess[stamp] = int(time.time()) - app.config[limit] - 1

    html = _login_page_after(client, client.get('/settings'))
    assert _messages(html) == ['Your session has expired. Please sign in again.']
    assert 'auth-flash--info' in html


def test_a_live_session_is_told_nothing(client):
    """No banner on an ordinary visit. A page that always explains itself is a
    page nobody reads."""
    _register(client)
    _post(client, '/logout', {}, page_path='/settings')
    client.get('/login')          # consumes the sign-out message
    assert _messages(client.get('/login').get_data(as_text=True)) == []


# ---------------------------------------------------------------------------
# 3. Forced invalidation
# ---------------------------------------------------------------------------

def test_invalidated_credentials_get_a_security_message(app, client):
    """A password change elsewhere signed this browser out.

    Simulated by moving the account's `session_version` past the one this
    session holds — which is exactly what `identity.set_password` does, and
    what a reset in another browser would do to this one.
    """
    _register(client)
    from models import AppUser, db
    user = AppUser.query.filter_by(username='sal').first()
    user.session_version = (user.session_version or 0) + 1
    db.session.commit()

    html = _login_page_after(client, client.get('/settings'))
    assert _messages(html) == [
        'Your password was changed, so every session using the old one was '
        'signed out. Please sign in again.']
    assert 'auth-flash--warn' in html


def test_a_removed_account_is_told_it_is_gone(client):
    """Distinct from the password case, and it has to be.

    `current_user()` answers None for both and the handling is identical — the
    session cannot be repaired either way. But "your password was changed" is a
    false statement to show somebody who was removed from the household, and it
    sends them looking for a password problem that does not exist.

    Two accounts, because `/login` redirects to `/setup` when no `AppUser`
    exists at all — deleting the only one would test the first-run path
    instead of this one.
    """
    from models import AppUser, db

    _register(client, 'keeper', 'keeper@example.com')
    _post(client, '/logout', {}, page_path='/settings')
    client.get('/login')                      # drain the sign-out message

    _register(client, 'removed', 'removed@example.com')
    db.session.delete(AppUser.query.filter_by(username='removed').first())
    db.session.commit()

    html = _login_page_after(client, client.get('/settings'))
    assert _messages(html) == [
        'This account is no longer active. Ask whoever runs the household if '
        'you think that is a mistake.']


def test_the_three_involuntary_messages_are_all_different(app, client):
    """The point of the whole exercise, asserted directly.

    Each of the three is pinned individually above; this fails if a later edit
    collapses two of them into the same wording, which no single-scenario test
    would notice.
    """
    from models import AppUser, db

    def expired():
        _register(client, 'alpha', 'alpha@example.com')
        with client.session_transaction() as sess:
            sess['seen_at'] = int(time.time()) - app.config['SESSION_IDLE_SECONDS'] - 1
        return _messages(_login_page_after(client, client.get('/settings')))[0]

    def invalidated():
        _register(client, 'bravo', 'bravo@example.com')
        user = AppUser.query.filter_by(username='bravo').first()
        user.session_version = (user.session_version or 0) + 1
        db.session.commit()
        return _messages(_login_page_after(client, client.get('/settings')))[0]

    def signed_out():
        _register(client, 'charlie', 'charlie@example.com')
        return _messages(_login_page_after(
            client, _post(client, '/logout', {}, page_path='/settings')))[0]

    # Each leaves the client signed out, so the next can register in turn.
    seen = [expired(), invalidated(), signed_out()]
    assert len(set(seen)) == 3, f'these should not read alike: {seen}'


# ---------------------------------------------------------------------------
# 4. The pre-existing flash that nobody could see
# ---------------------------------------------------------------------------

def test_reset_confirmation_is_not_deferred_to_the_next_page(app, client):
    """`/reset-password` has flashed since Phase 10.5, onto a page that did not
    render flashes.

    The message therefore survived in the session and appeared as a toast on
    the dashboard *after* the next sign-in, describing something that had
    happened a page and a half earlier. Asserting it lands on the login page is
    what stops that returning.
    """
    from dough.services import identity
    from models import AppUser

    _register(client)
    _post(client, '/logout', {}, page_path='/settings')
    client.get('/login')          # drain the sign-out message

    user = AppUser.query.filter_by(username='sal').first()
    _row, token = identity.issue_token(user, 'password_reset')

    # One GET only, and the CSRF token is taken from *it*. The reset token is
    # spent by loading the form — that is deliberate, and documented in
    # tests/test_identity.py — so `_post`'s habit of re-fetching the page for a
    # fresh CSRF token would land on the "that link doesn't work" branch, which
    # has no form and no token, and the POST would then fail CSRF rather than
    # testing anything. The session's `_reset_user_id` grant is what authorises
    # the POST from here.
    form = client.get(f'/reset-password/{token}')
    new = 'trombone9pastry'
    response = client.post(f'/reset-password/{token}',
                           data={'password': new, 'confirm': new,
                                 '_csrf_token': _csrf(form)})

    html = _login_page_after(client, response)
    assert any('password has been changed' in m for m in _messages(html)), (
        'the reset confirmation did not reach the login page; it is queued for '
        'whatever renders flashes next')


# ---------------------------------------------------------------------------
# 5. Accessibility and dismissal
# ---------------------------------------------------------------------------

def test_security_messages_are_assertive_and_the_rest_polite(app, client):
    """An expiry can wait for a pause in speech; a credential change should not."""
    _register(client, 'alpha', 'alpha@example.com')
    with client.session_transaction() as sess:
        sess['seen_at'] = int(time.time()) - app.config['SESSION_IDLE_SECONDS'] - 1
    calm = _login_page_after(client, client.get('/settings'))
    assert 'role="status"' in calm and 'aria-live="polite"' in calm

    from models import AppUser, db
    _register(client, 'bravo', 'bravo@example.com')
    user = AppUser.query.filter_by(username='bravo').first()
    user.session_version = (user.session_version or 0) + 1
    db.session.commit()
    urgent = _login_page_after(client, client.get('/settings'))
    assert 'role="alert"' in urgent and 'aria-live="assertive"' in urgent


def test_every_banner_is_dismissible_with_a_labelled_control(client):
    """An icon-only button with no accessible name is a button a screen reader
    announces as "button"."""
    _register(client)
    html = _login_page_after(client, _post(client, '/logout', {},
                                           page_path='/settings'))
    assert 'class="auth-flash__close"' in html
    assert 'aria-label="Dismiss this message"' in html


def test_the_message_is_escaped_not_injected(app, client):
    """The banner renders whatever was flashed, so it must not render markup.

    None of today's messages contain any, but they are the only strings on this
    page that come from application state rather than the template.
    """
    from flask import flash, render_template_string

    # Rendered through the same partial the routes use, rather than a copy of
    # its markup, so this fails if the partial itself is ever marked |safe.
    with app.test_request_context():
        flash('<img src=x onerror=alert(1)>', 'info')
        html = render_template_string("{% include '_auth_flash.html' %}")

    assert '<img src=x' not in html
    assert '&lt;img' in html


# ---------------------------------------------------------------------------
# 6. No page can silently swallow a message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('template', [
    'login.html', 'register.html', 'join.html', 'setup.html',
    'forgot_password.html', 'reset_password.html',
])
def test_every_signed_out_page_renders_flashes(template):
    """The six auth-shell pages do not extend base.html.

    Any one of them that omits the partial does not merely fail to show a
    message — it leaves it queued, and it turns up later somewhere it makes no
    sense. That is a worse failure than showing nothing, and it is invisible
    until somebody notices a stale toast on their dashboard.
    """
    with open(f'templates/{template}', encoding='utf-8') as handle:
        assert '_auth_flash.html' in handle.read(), (
            f'{template} renders no flashes, so one landing there is deferred '
            f'to whatever page renders them next')
