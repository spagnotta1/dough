"""Registration, recovery, and the account controls.  [Phase 10.5]

Four things are asserted here and they are not equally important. In the order
a review should read them:

1. **No response reveals whether an account exists.** `/forgot-password` must
   answer identically — wording, status, and elapsed time — for an address that
   has an account and one that does not. The timing assertion is the one that
   would be missing if nobody had thought about it, so it is written out rather
   than folded into a loop.

2. **A reset invalidates everything.** The premise of needing a reset is that
   somebody else may hold the old credential, so completing one must stop every
   session and every API token. This is asserted end-to-end through the routes,
   not against the model — `tests/test_session_version.py` covers the model.

3. **A token is single-use and expiring.** Including the case that is easy to
   get wrong: a token spent by loading the form stays spent even if the form is
   never submitted.

4. **Registration creates the whole shape.** A household, an owner in it, and
   nothing halfway — which `tools/verify_tenancy.py` would report as a failure
   if either half could exist alone.

The fixtures build their own application with `AUTH_ENABLED` and `CSRF_ENABLED`
on, matching `tests/test_api_auth.py`. The suite default is both off, and a test
of an authentication flow that ran with authentication off would pass without
exercising anything.
"""

import re
import time

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from dough.services import identity
from dough.services.email import EmailError

PASSWORD = 'hunter2boat'
NEW_PASSWORD = 'trombone9pastry'


@pytest.fixture()
def app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'CSRF_ENABLED': True,
        # The whole point of this suite: registration must be reachable.
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


@pytest.fixture()
def mailbox(app):
    """The messages this application sent. TestingConfig installs MemoryBackend."""
    return app.extensions['dough_email'].backend


def _csrf(response):
    match = re.search(r'name="_csrf_token" value="([^"]+)"',
                      response.get_data(as_text=True))
    return match.group(1) if match else None


def _post(client, path, data, page_path=None):
    """POST with a token minted from a real GET of the page.

    Not a helper that reaches into the session for the token: these routes are
    `@public` and `@public` has never meant CSRF-exempt, so a POST that skipped
    the token would be a 403 — and a suite that worked around that would stop
    noticing if the protection were removed.
    """
    token = _csrf(client.get(page_path or path))
    return client.post(path, data={**data, '_csrf_token': token})


def _logout(client):
    """Sign out properly. `/logout` is a POST and CSRF applies to it.

    A bare `client.post('/logout')` is a 403 that leaves the session intact, so
    a test using it would go on to assert against a browser that is still signed
    in -- and would pass or fail for reasons unrelated to what it is testing.
    """
    return _post(client, '/logout', {}, page_path='/settings')


def _register(client, username='sal', email='sal@example.com',
              password=PASSWORD):
    return _post(client, '/register',
                 {'username': username, 'email': email,
                  'password': password, 'confirm': password})


def _link_from(message):
    """The URL out of a sent message body."""
    match = re.search(r'(https?://\S+)', message.body)
    assert match, f'no link in the {message.purpose} mail'
    return match.group(1)


def _path_of(url):
    from urllib.parse import urlsplit

    split = urlsplit(url)
    return split.path + (('?' + split.query) if split.query else '')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registration_creates_a_household_and_an_owner_in_it(client):
    """Both halves, or the installation is in a state the verifier rejects.

    A user with no household cannot see anything — every scoped query needs one
    — and a household with no owner is one nobody can administer.
    `tools/verify_tenancy.py` reports each of those as a failure, so the pairing
    is an invariant of the database rather than a convenience of this route.
    """
    from models import AppUser, Household, ROLE_OWNER

    assert _register(client).status_code == 302

    user = AppUser.query.filter_by(username='sal').one()
    assert user.role == ROLE_OWNER
    assert user.email == 'sal@example.com'
    assert user.household_id is not None

    household = Household.query.get(user.household_id)
    assert household is not None
    assert [m.id for m in household.members] == [user.id]


def test_registration_signs_the_new_account_in(client):
    _register(client)
    # A protected page, not `/` -- `/` is public since the landing page landed,
    # so a 200 there would prove nothing about being signed in.
    assert client.get('/transactions').status_code == 200


def test_registration_stores_the_password_hashed(client):
    from models import AppUser

    _register(client)
    stored = AppUser.query.filter_by(username='sal').one().password_hash
    assert PASSWORD not in stored
    # The KDF `dough.auth.PASSWORD_METHOD` names. A registration path that
    # hashed with something else would be invisible until somebody compared two
    # accounts by hand.
    assert stored.startswith('scrypt:')


def test_registration_lowercases_the_stored_address(client):
    """Stored normalized, so uniqueness and the reset lookup are case-blind.

    Storing what was typed would make `Sam@x.com` and `sam@x.com` two accounts,
    and would make `/forgot-password` find one only when the address was typed
    the same way twice.
    """
    from models import AppUser

    _register(client, email='SaL@Example.COM')
    assert AppUser.query.filter_by(username='sal').one().email == 'sal@example.com'


def test_a_duplicate_username_is_refused_and_says_so(client):
    """Usernames may be named in the refusal; addresses may not.

    Somebody choosing a username needs to be told it is taken, or they retype
    the same value. Usernames have also been enumerable through `/join` since
    Phase 6, so this leaks nothing new.
    """
    from models import AppUser

    _register(client)
    response = _register(client.application.test_client(),
                         email='other@example.com')
    assert b'taken' in response.data
    assert AppUser.query.filter_by(username='sal').count() == 1


def test_a_duplicate_email_is_refused_without_confirming_it_is_registered(client):
    """The asymmetry, and the reason for it.

    An address is an identifier somebody else chose and holds. Confirming one is
    registered here tells a stranger that this person banks with Dough, which is
    a fact about them rather than about this application.
    """
    from models import AppUser

    _register(client)
    response = _register(client.application.test_client(), username='other')

    assert AppUser.query.count() == 1
    body = response.get_data(as_text=True)
    # It must not say the *address* is the problem. The wording names both
    # fields precisely so that neither is confirmed.
    assert 'username or email' in body
    assert 'That email is already' not in body
    assert 'address is taken' not in body


def test_registration_rejects_a_short_password(client):
    from models import AppUser

    response = _register(client, password='short')
    assert b'at least 8 characters' in response.data
    assert AppUser.query.count() == 0


def test_registration_rejects_a_password_containing_the_username(client):
    """The rule that gets stronger as the attacker learns more about the target.

    Length and a blocklist stop the passwords everybody knows. This one stops
    the password somebody who has just read a list of usernames tries first.
    """
    from models import AppUser

    response = _register(client, username='salvatore',
                         password='salvatore99')
    assert b'must not contain your username' in response.data
    assert AppUser.query.count() == 0


def test_registration_rejects_a_malformed_address(client):
    from models import AppUser

    assert b'email address' in _register(client, email='not-an-address').data
    assert AppUser.query.count() == 0


def test_registration_is_closed_by_default(tmp_path):
    """The config default is off, and the page says what to do instead.

    404 was the alternative and it is worse in both directions: for the person,
    "not found" is indistinguishable from a typo, so they retry the URL instead
    of asking for an invitation; for the product, a URL that exists on some
    deployments and not others makes the landing page's own button a dead link.
    """
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True, 'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'closed.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        assert application.config['ALLOW_REGISTRATION'] is False
        response = application.test_client().get('/register')
        assert response.status_code == 403
        assert b'Registration is closed' in response.data
        assert b'invitation' in response.data
    scheduler_module._scheduler = None


def _closed_app(tmp_path, name='closed'):
    """An app with authentication on and registration at its default: off."""
    scheduler_module._scheduler = None
    return create_app(test_config={
        'TESTING': True, 'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / f'{name}.db'}",
        'SYNC_SYNCHRONOUS': True, 'SYNC_AUTO_ENABLED': False,
    })


def test_a_closed_instance_refuses_a_posted_registration(tmp_path):
    """The gate is the route, not the rendered form.

    The test above proves the *page* says no. This proves the **operation** does,
    which is a different claim and the one that matters: somebody bypassing the
    UI does not need the form, and a check that only hid the button would let
    `curl -d` create an account on an instance whose owner believes it closed.

    The route refuses before reading the body, so no password is hashed and no
    row is written for a request that was never going to be honoured.
    """
    from models import AppUser

    application = _closed_app(tmp_path, 'posted')
    with application.app_context():
        response = application.test_client().post('/register', data={
            'username': 'intruder', 'email': 'intruder@example.com',
            'password': 'a-perfectly-good-password', 'confirm':
            'a-perfectly-good-password'})
        assert response.status_code == 403
        assert AppUser.query.count() == 0, (
            'a closed instance must not grow the user table')
    scheduler_module._scheduler = None


def test_closing_registration_does_not_lock_out_the_people_already_there(tmp_path):
    """Closing the door must not also lock the people inside.

    `ALLOW_REGISTRATION` is the switch an operator reaches for under pressure —
    an abuse spike, or the realisation that mail does not deliver yet. It has to
    be safe to flip on a running deployment, which means it governs *creating*
    an account and nothing else.
    """
    application = _closed_app(tmp_path, 'existing')
    with application.app_context():
        from models import db

        client = application.test_client()
        page = client.get('/setup')
        client.post('/setup', data={
            'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD,
            '_csrf_token': _csrf(page)})
        db.session.remove()

        signed_out = application.test_client()
        page = signed_out.get('/login')
        response = signed_out.post('/login', data={
            'username': 'sal', 'password': PASSWORD,
            '_csrf_token': _csrf(page)}, follow_redirects=True)
        assert response.status_code == 200
        assert signed_out.get('/').status_code == 200
    scheduler_module._scheduler = None


def test_an_invitation_still_works_while_registration_is_closed(tmp_path):
    """The intended path in, and the reason closing the door is not a lockdown.

    `ALLOW_REGISTRATION` governs *self-serve* signup. An invitation is the
    owner deciding, one person at a time, which is exactly the model an
    invite-only launch runs on — so it must keep working when the public form
    does not, or "closed" would mean the product cannot grow at all.
    """
    from dough.services.membership import issue_invite
    from dough.tenancy import tenant_scope, unscoped
    from models import ROLE_MEMBER, AppUser, db

    application = _closed_app(tmp_path, 'invited')
    with application.app_context():
        client = application.test_client()
        page = client.get('/setup')
        client.post('/setup', data={
            'username': 'sal', 'password': PASSWORD, 'confirm': PASSWORD,
            '_csrf_token': _csrf(page)})

        with unscoped():
            owner = AppUser.query.filter_by(username='sal').one()
            household_id = owner.household_id
        with tenant_scope(household_id):
            _row, token = issue_invite(household_id, owner, role=ROLE_MEMBER,
                                       label='partner')
        db.session.commit()
        db.session.remove()

        guest = application.test_client()
        page = guest.get(f'/join/{token}')
        assert page.status_code == 200, 'a closed instance must still honour an invite'
        guest.post(f'/join/{token}', data={
            'username': 'partner', 'password': PASSWORD, 'confirm': PASSWORD,
            '_csrf_token': _csrf(page)}, follow_redirects=True)

        with unscoped():
            joined = AppUser.query.filter_by(username='partner').one_or_none()
        assert joined is not None, 'the invited person must get an account'
        assert joined.household_id == household_id
    scheduler_module._scheduler = None


def test_registration_sends_a_verification_mail(client, mailbox):
    _register(client)
    sent = [m for m in mailbox.sent if m.purpose == 'verify_email']
    assert len(sent) == 1
    assert sent[0].to == 'sal@example.com'


def test_a_failed_mail_does_not_cost_the_registration(client, app, monkeypatch):
    """The account already exists by the time the mail is attempted.

    A mail server that is down must not turn a successful registration into an
    error page, because the account was created and the person is signed in —
    reporting failure would leave them believing they have no account while
    holding a session for one.
    """
    from models import AppUser
    from dough.services.email import EmailError

    def _explode(*_args, **_kwargs):
        raise EmailError('nope')

    monkeypatch.setattr(app.extensions['dough_email'].backend, 'send', _explode)

    assert _register(client).status_code == 302
    assert AppUser.query.filter_by(username='sal').count() == 1


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

def test_following_the_verification_link_marks_the_address_proved(client, mailbox):
    from models import AppUser

    _register(client)
    link = _link_from(mailbox.sent[-1])

    response = client.get(_path_of(link))
    assert response.status_code == 200
    assert b'Email confirmed' in response.data
    assert AppUser.query.filter_by(username='sal').one().email_verified_at


def test_a_verification_link_works_only_once(client, mailbox):
    _register(client)
    path = _path_of(_link_from(mailbox.sent[-1]))

    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 404


def test_a_reset_token_is_not_accepted_as_a_verification_token(client, mailbox):
    """`purpose` is what stops one link doing the other's job.

    Both are 256-bit strings in the same table. Without the purpose check, a
    link that merely proves an address is reachable would also set a password.
    """
    _register(client)
    _post(client, '/forgot-password', {'email': 'sal@example.com'})
    reset = [m for m in mailbox.sent if m.purpose == 'password_reset'][-1]
    reset_token = _path_of(_link_from(reset)).rsplit('/', 1)[-1]

    assert client.get(f'/verify-email/{reset_token}').status_code == 404


# ---------------------------------------------------------------------------
# Password reset — enumeration
# ---------------------------------------------------------------------------

def test_forgot_password_says_the_same_thing_for_both_outcomes(client, mailbox):
    """Part one of the rule: the wording and the shape must not differ."""
    _register(client)
    mailbox.clear()

    known = _post(client, '/forgot-password', {'email': 'sal@example.com'})
    unknown = _post(client, '/forgot-password', {'email': 'nobody@example.com'})

    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data, (
        'the response differs between a known and an unknown address')
    assert b'If there' in known.data      # "If there's a Dough account…"

    # And the mail really was only sent for the address that has an account,
    # so the identical responses are not identical because nothing happened.
    assert [m.to for m in mailbox.sent] == ['sal@example.com']


def test_forgot_password_takes_about_as_long_either_way(client):
    """Part three, and the part that would be missing if nobody thought of it.

    Sending mail takes tens of milliseconds; not sending it takes none. Over a
    few hundred requests that difference is measurable, and it answers the
    question the wording refuses to.

    The assertion is deliberately loose. It is not proving constant-time
    behaviour — it is proving that a floor exists at all, which is the property
    that would be lost if `_uniform_delay` were deleted. A tight bound here
    would be a flaky test on a loaded CI machine, and a flaky test gets deleted.
    """
    from dough.blueprints.auth import UNIFORM_RESPONSE_SECONDS

    _register(client)

    def _elapsed(address):
        started = time.monotonic()
        _post(client, '/forgot-password', {'email': address})
        return time.monotonic() - started

    known = _elapsed('sal@example.com')
    unknown = _elapsed('nobody@example.com')

    # Both are held to the floor...
    assert known >= UNIFORM_RESPONSE_SECONDS * 0.9
    assert unknown >= UNIFORM_RESPONSE_SECONDS * 0.9
    # ...so neither stands out. The real work is tens of milliseconds against a
    # 350ms floor, so a 150ms envelope is generous and still fails a missing
    # delay, where the gap would be the entire floor.
    assert abs(known - unknown) < 0.15


def test_forgot_password_does_not_leak_through_the_rate_limiter(client, app):
    """A refusal must look like a success, or the limiter becomes the oracle.

    This is the failure mode a limiter *introduces*: "you are being rate
    limited" for one address and "check your inbox" for another is the
    enumeration signal again, arriving through the control added to protect the
    route.
    """
    from dough.services.ratelimit import Limiter, MemoryBackend

    _register(client)
    # Enabled explicitly: TestingConfig turns limiting off so the older suites
    # can make as many requests as they like.
    app.extensions['dough_ratelimit'] = Limiter(MemoryBackend(), enabled=True)

    seen = set()
    for _ in range(8):     # the `password_reset` policy allows 5 per hour
        response = _post(client, '/forgot-password', {'email': 'sal@example.com'})
        seen.add((response.status_code, response.data))

    assert len(seen) == 1, 'a rate-limited response differs from an allowed one'


# ---------------------------------------------------------------------------
# Password reset — the flow
# ---------------------------------------------------------------------------

def _reset_path(client, mailbox, address='sal@example.com'):
    mailbox.clear()
    _post(client, '/forgot-password', {'email': address})
    assert mailbox.sent, 'no reset mail was sent'
    return _path_of(_link_from(mailbox.sent[-1]))


def test_a_reset_sets_the_new_password(client, mailbox):
    _register(client)
    _logout(client)
    path = _reset_path(client, mailbox)

    assert _post(client, path,
                 {'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD},
                 page_path=path).status_code == 302

    login = _post(client, '/login',
                  {'username': 'sal', 'password': NEW_PASSWORD})
    assert login.status_code == 302
    assert client.get('/transactions').status_code == 200


def test_a_reset_invalidates_every_session_and_every_api_token(client, app, mailbox):
    """The reason a reset exists: somebody else may hold the old credential.

    Asserted across both surfaces in one test on purpose. They are one
    guarantee — `session_version` is a single counter — and splitting them into
    two tests would let a half-working implementation pass one of them.
    """
    _register(client)

    token = app.test_client().post(
        '/api/v1/auth/login',
        json={'username': 'sal', 'password': PASSWORD,
              'device_name': 'phone'}).get_json()['data']['token']
    headers = {'Authorization': f'Bearer {token}'}
    assert app.test_client().get('/api/v1/auth/me',
                                 headers=headers).status_code == 200

    other_browser = app.test_client()
    _post(other_browser, '/login', {'username': 'sal', 'password': PASSWORD})
    assert other_browser.get('/transactions').status_code == 200

    path = _reset_path(client, mailbox)
    _post(client, path, {'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD},
          page_path=path)

    assert app.test_client().get('/api/v1/auth/me',
                                 headers=headers).status_code == 401
    assert other_browser.get('/transactions').status_code == 302


def test_a_reset_does_not_sign_the_resetter_in(client, mailbox):
    """The one flow whose premise is "your credentials may be compromised".

    Ending it by handing out a session, without anybody having typed the new
    password once, would mean a stolen reset link is a session rather than a
    password change somebody notices.
    """
    _register(client)
    _logout(client)
    path = _reset_path(client, mailbox)
    _post(client, path, {'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD},
          page_path=path)

    assert client.get('/transactions').status_code == 302


def test_a_reset_link_is_spent_by_loading_the_form(client, mailbox):
    """Deliberate, and it costs something real — see the route's docstring.

    Validate-on-GET/spend-on-POST leaves a redeemable token alive across a
    window the application does not control, which is the window somebody who
    has read the victim's mail is waiting in.
    """
    _register(client)
    path = _reset_path(client, mailbox)

    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 404


def test_an_expired_reset_link_is_refused(client, app, mailbox):
    from datetime import datetime, timedelta

    from models import EmailVerification, db

    _register(client)
    path = _reset_path(client, mailbox)

    row = EmailVerification.query.order_by(EmailVerification.id.desc()).first()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()

    assert client.get(path).status_code == 404


def test_requesting_a_second_link_retires_the_first(client, mailbox):
    """Otherwise "I clicked it three times, which one?" has a wrong answer.

    It also means a reset requested by somebody who has read the victim's inbox
    is cancelled the moment the victim requests their own.
    """
    _register(client)
    first = _reset_path(client, mailbox)
    second = _reset_path(client, mailbox)

    assert first != second
    assert client.get(first).status_code == 404
    assert client.get(second).status_code == 200


def test_changing_the_address_retires_a_link_already_in_flight(client, mailbox):
    """`sent_to` is compared back, so the link follows the address it was sent to.

    The attack: somebody requests a reset and waits on the mail. The real owner
    changes their address. Redeeming the old link would act on an address its
    holder never proved they control.
    """
    from models import AppUser, db

    _register(client)
    path = _reset_path(client, mailbox)

    user = AppUser.query.filter_by(username='sal').one()
    user.email = 'moved@example.com'
    db.session.commit()

    assert client.get(path).status_code == 404


def test_the_reset_grant_expires_even_if_the_form_is_never_submitted(client, mailbox):
    """Loading the form must not leave a takeover in the cookie for days.

    The token is spent by the GET, so what survives it is a session key saying
    "may set this account's password, no current password required". Left
    unbounded, that sits in the cookie for the whole session lifetime — turning
    a stolen session cookie into a full account takeover, on the one operation
    that otherwise always demands the current password.
    """
    from dough.blueprints.auth import RESET_GRANT_SECONDS

    _register(client)
    _logout(client)
    path = _reset_path(client, mailbox)

    # The CSRF token is taken from *this* GET, which is the one that renders the
    # form. It is also the GET that spends the reset token, so a later fetch of
    # the same URL returns the "link doesn't work" page with no form and no
    # token in it — and the POST below would be refused as a CSRF failure rather
    # than reaching the expiry check this test is about.
    csrf = _csrf(client.get(path))

    with client.session_transaction() as sess:
        user_id, _granted = sess['_reset_user_id']
        sess['_reset_user_id'] = (user_id, time.time() - RESET_GRANT_SECONDS - 1)

    assert client.post(path, data={'password': NEW_PASSWORD,
                                   'confirm': NEW_PASSWORD,
                                   '_csrf_token': csrf}).status_code == 404
    # And the stale grant is dropped rather than left to be retried.
    with client.session_transaction() as sess:
        assert '_reset_user_id' not in sess
    # The password really did not change.
    assert _post(client, '/login',
                 {'username': 'sal', 'password': PASSWORD}).status_code == 302


def test_an_unknown_reset_token_is_refused_without_saying_why(client):
    response = client.get('/reset-password/' + 'x' * 43)
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "doesn't work" in body
    # Unknown, expired and already-used must be indistinguishable. Naming the
    # reason confirms which tokens were once real.
    for tell in ('expired', 'already been used', 'unknown', 'not found'):
        assert f'This link has {tell}' not in body


# ---------------------------------------------------------------------------
# Account settings
# ---------------------------------------------------------------------------

def test_changing_a_password_requires_the_current_one(client):
    """A session is not proof that this person is here. An unlocked screen is
    a session, and a password change is what locks the real owner out."""
    _register(client)
    response = _post(client, '/settings/password',
                     {'current_password': 'wrong-password',
                      'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD},
                     page_path='/settings')
    assert response.status_code == 302

    # Still the old password.
    _logout(client)
    assert _post(client, '/login',
                 {'username': 'sal', 'password': PASSWORD}).status_code == 302


def test_changing_a_password_keeps_this_session_but_drops_the_others(client, app):
    """The re-stamp, and why it is not an exemption.

    This session is issued afresh under the new generation rather than carried
    over from the old one. Without it, a successful password change would
    redirect the person who made it to the login page — which reads as a bug.
    """
    _register(client)

    other = app.test_client()
    _post(other, '/login', {'username': 'sal', 'password': PASSWORD})
    assert other.get('/transactions').status_code == 200

    assert _post(client, '/settings/password',
                 {'current_password': PASSWORD,
                  'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD},
                 page_path='/settings').status_code == 302

    assert client.get('/transactions').status_code == 200
    assert other.get('/transactions').status_code == 302


def test_sign_out_everywhere_includes_this_browser(client, app):
    """The opposite decision, and the difference is what was asked for.

    Somebody reaching for this button does not know which of the live sessions
    is the one to worry about, so "everywhere" has to mean everywhere.
    """
    _register(client)
    other = app.test_client()
    _post(other, '/login', {'username': 'sal', 'password': PASSWORD})

    assert _post(client, '/settings/sessions/revoke', {},
                 page_path='/settings').status_code == 302

    assert client.get('/transactions').status_code == 302
    assert other.get('/transactions').status_code == 302


def test_sign_out_everywhere_stops_api_tokens_too(client, app):
    _register(client)
    token = app.test_client().post(
        '/api/v1/auth/login',
        json={'username': 'sal', 'password': PASSWORD,
              'device_name': 'phone'}).get_json()['data']['token']
    headers = {'Authorization': f'Bearer {token}'}
    assert app.test_client().get('/api/v1/auth/me',
                                 headers=headers).status_code == 200

    _post(client, '/settings/sessions/revoke', {}, page_path='/settings')

    assert app.test_client().get('/api/v1/auth/me',
                                 headers=headers).status_code == 401


def _stashed_token(client):
    """The plaintext a `/settings/tokens` POST just handed off.

    Read out of the session rather than off the page. The settings page no
    longer has an API-tokens section to render it into, but the route that mints
    it is still the one `/api/v1/auth/tokens` shares, so it is still worth
    holding to the contract that what it issues actually authenticates.
    """
    with client.session_transaction() as sess:
        return sess.get('_new_api_token')


def test_a_token_created_from_the_settings_page_works(client):
    _register(client)
    response = _post(client, '/settings/tokens',
                     {'name': 'Shortcuts', 'scopes': ['read']},
                     page_path='/settings')
    assert response.status_code == 302

    plaintext = _stashed_token(client)
    assert plaintext and plaintext.startswith('dgh_'), 'no token was issued'
    assert client.application.test_client().get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {plaintext}'}
    ).status_code == 200


def test_the_settings_page_does_not_show_api_tokens(client):
    """The section is gone, and so is any plaintext on the page.

    Only a hash is stored, so a page that rendered the plaintext would be
    reading it from somewhere it must not persist. With the section removed
    there is nowhere on this page it can surface at all.
    """
    _register(client)
    _post(client, '/settings/tokens', {'name': 'Shortcuts', 'scopes': ['read']},
          page_path='/settings')

    page = client.get('/settings').get_data(as_text=True)
    assert 'id="new-token"' not in page
    assert 'dgh_' not in page
    assert 'API tokens' not in page


def test_revoking_a_token_stops_it_immediately(client):
    from models import ApiToken

    _register(client)
    _post(client, '/settings/tokens', {'name': 'Shortcuts', 'scopes': ['read']},
          page_path='/settings')
    plaintext = _stashed_token(client)
    token_id = ApiToken.query.one().id

    _post(client, f'/settings/tokens/{token_id}/revoke', {},
          page_path='/settings')

    assert client.application.test_client().get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {plaintext}'}
    ).status_code == 401


def test_changing_the_address_clears_the_verified_stamp(client):
    """A proof attaches to an address, not to an account.

    Carrying the timestamp forward would mark an unproven address as verified —
    and that address is where a password reset gets sent.
    """
    from models import AppUser

    _register(client)
    user = AppUser.query.filter_by(username='sal').one()
    identity.mark_email_verified(user)
    assert user.email_verified_at is not None

    _post(client, '/settings/email', {'email': 'moved@example.com'},
          page_path='/settings')

    user = AppUser.query.filter_by(username='sal').one()
    assert user.email == 'moved@example.com'
    assert user.email_verified_at is None


def test_changing_the_address_sends_a_verification_to_the_new_one(client, mailbox):
    _register(client)
    mailbox.clear()

    _post(client, '/settings/email', {'email': 'moved@example.com'},
          page_path='/settings')

    assert [(m.purpose, m.to) for m in mailbox.sent] == \
        [('verify_email', 'moved@example.com')]


def _breaking_mail(app, exception):
    """Install a backend that raises `exception` on every send."""
    from dough.services.email import EmailBackend, EmailService

    class _Broken(EmailBackend):
        name = 'broken'

        def send(self, message):
            raise exception

    app.extensions['dough_email'] = EmailService(_Broken())


@pytest.mark.parametrize('exception', [
    # What a backend is documented to raise...
    EmailError('nope'),
    # ...and what one actually raised. `ConsoleBackend` writes the frame rules
    # with U+2500, which cp1252 cannot encode, so on a Windows console this is
    # every send.
    UnicodeEncodeError('cp1252', '─', 0, 1, 'unmapped'),
])
def test_an_address_change_reports_a_delivery_failure_rather_than_500ing(
        client, app, exception):
    """The address is committed before the mail is attempted.

    So a 500 here is worse than a broken button: the account now points at an
    unverified new address while the error page says "nothing was changed" —
    which is the one state in which somebody stops watching the new inbox.

    Parametrised over both exception families on purpose. Only the first was
    ever caught, and the route's `except (EmailError, IdentityError)` is exactly
    what the second walked past.
    """
    from models import AppUser

    _register(client)
    _breaking_mail(app, exception)

    response = _post(client, '/settings/email', {'email': 'moved@example.com'},
                     page_path='/settings')

    assert response.status_code == 302
    assert AppUser.query.filter_by(username='sal').one().email == \
        'moved@example.com'
    assert b'could not be sent' in client.get('/settings').data


def test_resending_a_verification_reports_a_delivery_failure_the_same_way(
        client, app):
    _register(client)
    _breaking_mail(app, UnicodeEncodeError('cp1252', '─', 0, 1, 'unmapped'))

    response = _post(client, '/settings/verify-email/resend', {},
                     page_path='/settings')

    assert response.status_code == 302
    assert b'could not be sent' in client.get('/settings').data


def test_settings_requires_a_session(client):
    _register(client)
    _logout(client)
    response = client.get('/settings', follow_redirects=False)
    assert response.status_code == 302 and '/login' in response.headers['Location']


# ---------------------------------------------------------------------------
# CSRF still applies to every one of these
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path,data', [
    ('/register', {'username': 'x', 'email': 'x@example.com',
                   'password': PASSWORD, 'confirm': PASSWORD}),
    ('/forgot-password', {'email': 'sal@example.com'}),
    ('/settings/password', {'current_password': PASSWORD,
                            'password': NEW_PASSWORD, 'confirm': NEW_PASSWORD}),
    ('/settings/sessions/revoke', {}),
    ('/settings/tokens', {'name': 'x'}),
])
def test_the_new_routes_reject_a_post_with_no_csrf_token(client, path, data):
    """`@public` has never meant CSRF-exempt, and this is where that is pinned.

    The failure it prevents is concrete on `/register`: an unprotected one lets
    an attacker sign a victim's browser into the *attacker's* account, where
    anything the victim then uploads lands in a ledger the attacker can read.
    That is the same argument that has protected `/login` since Phase 6.
    """
    _register(client)
    assert client.post(path, data=data).status_code == 403


# ---------------------------------------------------------------------------
# The service layer, without a request
# ---------------------------------------------------------------------------

def test_normalize_email_only_touches_case_and_whitespace():
    """Not Gmail's dots-and-plus rule. That is one provider's routing decision,
    and applying it universally merges genuinely different addresses everywhere
    that treats them as different."""
    assert identity.normalize_email('  Sam@Example.COM ') == 'sam@example.com'
    assert identity.normalize_email('s.a.m+dough@example.com') == \
        's.a.m+dough@example.com'


@pytest.mark.parametrize('password', [
    'password', 'PASSWORD', 'password123', '12345678', 'qwerty123',
])
def test_obvious_passwords_are_refused_whatever_their_case(password):
    with pytest.raises(identity.IdentityError, match='commonly guessed'):
        identity.validate_password(password)


def test_a_very_long_password_is_refused_as_a_denial_of_service_bound():
    """Not a security rule about the password — scrypt's cost is in its
    parameters. It is a bound on work an unauthenticated caller can request."""
    with pytest.raises(identity.IdentityError, match='too long'):
        identity.validate_password('a' * 2000)


def test_redeem_reports_a_reason_that_never_reaches_the_caller(app):
    """The reason exists for the audit log. `tests` read it; responses do not.

    `api_tokens.authenticate` returns one the same way and for the same purpose:
    the distinction is not lost, it is relocated somewhere an operator can read
    it and an attacker cannot.
    """
    row, reason = identity.redeem('nope', 'password_reset')
    assert row is None
    assert reason == 'unknown'
