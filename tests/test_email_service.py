"""Where a verification or reset link goes, and what is never written down.
[Phase 10.5]

The security assertion in this file is the third section: **a live token must
not reach the log stream.** Everything above it is the plumbing that makes the
backend a one-line configuration switch rather than a branch at every call site.

That assertion is worth stating plainly because `dough/logging.py` would not
catch this on its own. Its `_SECRETISH` pattern recognises `sk-…` keys, Plaid
tokens and card-length digit runs; a `secrets.token_urlsafe` value looks like
none of those, so a reset link logged at INFO would be shipped to whatever
aggregates the logs and would sit there, valid, for the token's whole lifetime.
Nothing else in the application would report it.
"""

import io
import json
import logging
import urllib.error
import urllib.request

import pytest

from dough.services.email import (ConsoleBackend, EmailBackend, EmailError,
                                  EmailService, MemoryBackend, PostmarkBackend,
                                  SmtpBackend, build_backend)

LINK = 'https://dough.example/reset-password/s3cr3t-t0k3n-value-here'


# ---------------------------------------------------------------------------
# Choosing a backend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name,expected', [
    ('console', ConsoleBackend),
    ('memory', MemoryBackend),
])
def test_the_backend_comes_from_configuration(name, expected):
    assert isinstance(build_backend({'MAIL_BACKEND': name}), expected)


def test_console_is_the_default():
    """The right default for a self-hosted instance with no mail server, and
    the only one under which the flow is usable before anything is configured."""
    assert isinstance(build_backend({}), ConsoleBackend)


def test_smtp_requires_a_host():
    with pytest.raises(EmailError, match='MAIL_SERVER'):
        build_backend({'MAIL_BACKEND': 'smtp'})


def test_smtp_is_built_from_the_mail_settings():
    backend = build_backend({'MAIL_BACKEND': 'smtp', 'MAIL_SERVER': 'mail.example',
                             'MAIL_PORT': 2525, 'MAIL_USERNAME': 'u',
                             'MAIL_PASSWORD': 'p', 'MAIL_USE_TLS': True})
    assert isinstance(backend, SmtpBackend)
    assert (backend.host, backend.port, backend.use_tls) == ('mail.example', 2525, True)


def test_an_unknown_backend_name_is_refused_rather_than_defaulted():
    """`MAIL_BACKEND=smpt` must not silently print reset links to a terminal.

    A default-to-console fallback produces a password-reset feature that appears
    to work and delivers nothing — in production, to the one audience who cannot
    reach the terminal.
    """
    with pytest.raises(EmailError, match='Unknown MAIL_BACKEND'):
        build_backend({'MAIL_BACKEND': 'smpt'})


# ---------------------------------------------------------------------------
# The two messages
# ---------------------------------------------------------------------------

@pytest.fixture()
def service():
    return EmailService(MemoryBackend(), from_address='dough@example')


def test_the_verification_mail_carries_the_link_and_its_purpose(service):
    message = service.send_verification_email('sam@example.com', LINK,
                                              username='sam')
    assert message.purpose == 'verify_email'
    assert message.to == 'sam@example.com'
    assert LINK in message.body
    assert 'sam' in message.body


def test_the_reset_mail_says_what_to_do_if_it_was_not_you(service):
    """And says the *useful* version of it.

    Not "contact support", which this application does not have — "your password
    has not changed", which is the fact that tells a worried reader whether they
    need to act at all.
    """
    message = service.send_password_reset_email('sam@example.com', LINK)
    assert message.purpose == 'password_reset'
    assert LINK in message.body
    assert 'has not changed' in message.body
    # It also warns that using the link is destructive to other sessions, so
    # nobody is surprised by being signed out on their other devices.
    assert 'sign you out everywhere' in message.body


def test_the_memory_backend_keeps_messages_per_instance(service):
    """Not a module global — the suite builds many applications in one process,
    and a shared list would let one test read another's mail."""
    other = EmailService(MemoryBackend())
    service.send_verification_email('a@example.com', LINK)

    assert len(service.backend.sent) == 1
    assert other.backend.sent == []


def test_the_from_address_reaches_the_backend():
    backend = SmtpBackend(host='mail.example')
    EmailService(backend, from_address='dough@example')
    assert backend.from_address == 'dough@example'


# ---------------------------------------------------------------------------
# The token never reaches the log
# ---------------------------------------------------------------------------

def test_the_console_backend_prints_the_link_but_does_not_log_it(caplog):
    """The one place that deliberately renders a live credential to a stream.

    Confining it to stdout is what makes that acceptable: the terminal is read
    by the operator running the process, and the log stream is shipped, indexed
    and retained.
    """
    stream = io.StringIO()
    service = EmailService(ConsoleBackend(stream=stream))

    with caplog.at_level(logging.DEBUG):
        service.send_password_reset_email('sam@example.com', LINK)

    assert LINK in stream.getvalue(), 'the operator cannot reach the link'
    assert LINK not in caplog.text, 'a live reset link was written to the log'


def test_what_is_logged_is_the_fact_and_not_the_payload(caplog):
    """Delivery working and the flow being reached are operational facts.

    They are worth logging and they are all that is logged. The recipient and
    the purpose answer "is mail going out, and was this flow used"; the body
    answers nothing an operator needs and holds the credential.
    """
    service = EmailService(MemoryBackend())
    with caplog.at_level(logging.INFO):
        service.send_password_reset_email('sam@example.com', LINK)

    assert 'password_reset' in caplog.text
    assert 'sam@example.com' in caplog.text
    assert LINK not in caplog.text


def test_a_message_repr_does_not_include_the_body():
    """A repr is what ends up in a traceback, a debugger, and a pytest diff.

    Three places nobody chose to put a secret, reached by code that is not
    thinking about secrets at all.
    """
    service = EmailService(MemoryBackend())
    message = service.send_password_reset_email('sam@example.com', LINK)

    assert LINK not in repr(message)
    assert 'sam@example.com' in repr(message)


def test_an_smtp_failure_does_not_quote_the_message_it_was_carrying(monkeypatch):
    """An SMTP error can echo the payload, and the payload is a reset link.

    Same rule as `dough/logging.py`'s "type and message only" for tracebacks,
    one notch stricter because of what this particular payload is.
    """
    import smtplib

    class _Boom:
        def __init__(self, *a, **k):
            raise smtplib.SMTPException(f'rejected message containing {LINK}')

    monkeypatch.setattr(smtplib, 'SMTP', _Boom)
    service = EmailService(SmtpBackend(host='mail.example'))

    with pytest.raises(EmailError) as excinfo:
        service.send_password_reset_email('sam@example.com', LINK)

    assert LINK not in str(excinfo.value)
    assert 'mail.example' in str(excinfo.value)
    assert 'SMTPException' in str(excinfo.value)


# ---------------------------------------------------------------------------
# A delivery failure is an EmailError, whatever the backend raised
# ---------------------------------------------------------------------------

def test_a_backend_raising_something_unexpected_still_raises_EmailError():
    """Every caller catches `EmailError` and nothing wider.

    So a backend that raises anything else does not produce "we could not send
    that mail" — it escapes the route entirely and becomes a 500, on a request
    that has already committed its database work. Enforced here rather than
    trusted, because a backend is the part of this module that talks to the
    outside world.
    """
    class _Erratic(EmailBackend):
        name = 'erratic'

        def send(self, message):
            raise ValueError('something nobody anticipated')

    service = EmailService(_Erratic())
    with pytest.raises(EmailError) as excinfo:
        service.send_verification_email('sam@example.com', LINK)

    assert 'erratic' in str(excinfo.value)
    assert 'ValueError' in str(excinfo.value)


def test_the_conversion_does_not_quote_what_the_backend_said():
    """Same rule as `SmtpBackend.send`, applied to the failures it did not
    anticipate: the text of a delivery error can echo the message it was
    carrying, and this one is carrying a live token."""
    class _Chatty(EmailBackend):
        name = 'chatty'

        def send(self, message):
            raise RuntimeError(f'refused: {message.body}')

    service = EmailService(_Chatty())
    with pytest.raises(EmailError) as excinfo:
        service.send_password_reset_email('sam@example.com', LINK)

    assert LINK not in str(excinfo.value)


def test_the_console_backend_survives_a_stream_that_cannot_encode_the_frame():
    """`sys.stdout` on a Windows console is cp1252, which has no U+2500.

    The frame rules are decoration and the link is the payload, so an
    unencodable character degrades the decoration rather than failing the
    send — losing a verification mail over a code page is the worst available
    trade, and no configuration change fixes the operator's terminal.
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', newline='')
    service = EmailService(ConsoleBackend(stream=stream))

    service.send_verification_email('sam@example.com', LINK, username='sam')

    stream.seek(0)
    written = stream.buffer.getvalue().decode('cp1252')
    assert LINK in written, 'the link must survive whatever the frame does'
    assert 'sam@example.com' in written


# ---------------------------------------------------------------------------
# Postmark over HTTPS, for hosts that block SMTP
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def postmark(monkeypatch):
    """A `PostmarkBackend` whose HTTP call is captured rather than made."""
    sent = []

    def _urlopen(request, timeout=None):
        sent.append(request)
        return _FakeResponse({'ErrorCode': 0, 'MessageID': 'abc-123'})

    monkeypatch.setattr(urllib.request, 'urlopen', _urlopen)
    backend = PostmarkBackend(token='server-token-value')
    return EmailService(backend, from_address='dough@example'), sent


def test_postmark_posts_the_message_as_json(postmark):
    service, sent = postmark
    service.send_verification_email('sam@example.com', LINK, username='sam')

    assert len(sent) == 1
    body = json.loads(sent[0].data.decode('utf-8'))
    assert body['To'] == 'sam@example.com'
    assert body['From'] == 'dough@example'
    assert LINK in body['TextBody']
    # Postmark routes on this and rejects a message whose stream does not
    # exist, so a default of "outbound" is the difference between working out
    # of the box and a 422 nobody expects.
    assert body['MessageStream'] == 'outbound'


def test_postmark_authenticates_with_the_server_token(postmark):
    service, sent = postmark
    service.send_verification_email('sam@example.com', LINK)
    # urllib title-cases header names it is given.
    assert sent[0].get_header('X-postmark-server-token') == 'server-token-value'


def test_a_200_carrying_an_error_code_is_not_a_send(monkeypatch):
    """The lesson the SMTP path taught expensively.

    Postmark accepts an SMTP session, returns success, and turns the message
    into a bounce afterwards — so "nothing raised" was reported as a delivered
    mail that had never left. Over HTTP the outcome is in the response, and
    ignoring it would reproduce exactly that bug in a place that can avoid it.
    """
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda request, timeout=None: _FakeResponse(
                            {'ErrorCode': 412, 'Message': 'pending approval'}))
    service = EmailService(PostmarkBackend(token='t'))

    with pytest.raises(EmailError, match='412'):
        service.send_verification_email('sam@example.com', LINK)


def test_a_rejection_reports_the_code_and_not_postmark_s_prose(monkeypatch):
    """Postmark's error text echoes fields from the request, and the request
    carries a live link. The numeric code identifies the problem on its own."""
    def _raise(request, timeout=None):
        raise urllib.error.HTTPError(
            'https://api.postmarkapp.com/email', 422, 'Unprocessable', {},
            io.BytesIO(json.dumps({'ErrorCode': 300,
                                   'Message': f'rejected: {LINK}'}).encode()))

    monkeypatch.setattr(urllib.request, 'urlopen', _raise)
    service = EmailService(PostmarkBackend(token='t'))

    with pytest.raises(EmailError) as excinfo:
        service.send_password_reset_email('sam@example.com', LINK)

    assert '300' in str(excinfo.value)
    assert LINK not in str(excinfo.value)


def test_an_unreachable_postmark_is_a_delivery_failure(monkeypatch):
    """Not a 500. This is the case that started the whole exercise."""
    def _raise(request, timeout=None):
        raise urllib.error.URLError('connection timed out')

    monkeypatch.setattr(urllib.request, 'urlopen', _raise)
    service = EmailService(PostmarkBackend(token='t'))

    with pytest.raises(EmailError, match='could not be reached'):
        service.send_verification_email('sam@example.com', LINK)


def test_a_response_that_is_not_json_is_not_a_send(monkeypatch):
    """A proxy or captive portal answering 200 with HTML is a real deployment
    condition, and it must not read as delivered."""
    class _Html:
        def read(self):
            return b'<html>you are behind a portal</html>'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda request, timeout=None: _Html())
    service = EmailService(PostmarkBackend(token='t'))

    with pytest.raises(EmailError, match='not JSON'):
        service.send_verification_email('sam@example.com', LINK)


def test_neither_the_token_nor_the_link_reaches_the_log(postmark, caplog):
    service, _sent = postmark
    with caplog.at_level(logging.DEBUG):
        service.send_password_reset_email('sam@example.com', LINK)

    assert 'server-token-value' not in caplog.text
    assert LINK not in caplog.text
    # What is logged is the MessageID, which is the row in Postmark's Activity
    # list — the only place the true outcome of a send is recorded.
    assert 'abc-123' in caplog.text


def test_a_postmark_backend_repr_does_not_carry_the_token():
    """It is the whole credential — it authenticates the API and doubles as the
    SMTP password — and a backend lands in tracebacks like anything else."""
    assert 'server-token-value' not in repr(PostmarkBackend(token='server-token-value'))


def test_postmark_is_built_from_configuration():
    backend = build_backend({'MAIL_BACKEND': 'postmark', 'MAIL_POSTMARK_TOKEN': 'tok'})
    assert isinstance(backend, PostmarkBackend)
    assert backend.token == 'tok'


def test_postmark_falls_back_to_the_smtp_password_for_its_token():
    """With Postmark they are the same string — their SMTP endpoint takes the
    server API token as both username and password. Reading it here makes
    moving a working SMTP setup onto the API one variable, with no second copy
    of the credential to keep in step."""
    backend = build_backend({'MAIL_BACKEND': 'postmark', 'MAIL_PASSWORD': 'tok'})
    assert backend.token == 'tok'


def test_postmark_without_a_token_is_refused_rather_than_attempted():
    with pytest.raises(EmailError, match='MAIL_POSTMARK_TOKEN'):
        build_backend({'MAIL_BACKEND': 'postmark'})


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_service_is_installed_per_application(app):
    from dough.services.email import current_email

    with app.test_request_context('/'):
        assert current_email() is app.extensions['dough_email']


def test_a_missing_service_raises_rather_than_building_a_default(app):
    """A lazily-constructed fallback would send real mail from a test.

    Absence here is a wiring bug — `EmailService.init_app` did not run — not a
    runtime condition to paper over.
    """
    from dough.services.email import current_email

    with app.test_request_context('/'):
        app.extensions.pop('dough_email')
        with pytest.raises(EmailError, match='No email service installed'):
            current_email()
