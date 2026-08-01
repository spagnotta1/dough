"""Delivering the two mails this application sends.  [Phase 10.5]

Allowed:   flask.current_app, models, sqlalchemy, stdlib
Must not:  app, render_template, url_for, redirect, flash, jsonify, anthropic,
           blueprints

`current_app` is read in exactly one place — `current_email()`, which resolves
the instance the factory installed. Every function that does work takes what it
needs as an argument, so the service is callable from a worker thread, a test,
or a CLI with no request and no application in scope.

## Why this is an abstraction and not four lines of `smtplib`

Not because SMTP is hard. Because the *decision* about where a password-reset
link goes has to be one line of configuration rather than a code change, and
because two of the three answers are not SMTP at all:

- `console` — print it to the terminal. The right default for a self-hosted
  instance with no mail server, and the only one under which the flow is
  usable before anybody has configured anything.
- `memory` — keep it in a list. What the test suite asserts against, so a test
  can prove a reset mail was sent to the right address without a network, a
  fixture mail server, or a monkeypatch of the module under test.
- `smtp` — actually send it.

The three share a `send()` signature and nothing else, which is what makes
`MAIL_BACKEND` a real switch rather than a branch inside every call site.

## The link is never written to the log

`ConsoleBackend` prints to **stdout directly** and deliberately does not go
through `logging`. A password-reset link is a bearer credential — holding it is
the whole authorization, and using it also locks the real owner out, so it is
strictly worse to leak than a session cookie.

`dough/logging.py` would not save us here. Its `_SECRETISH` pattern catches
`sk-…` keys, Plaid tokens and card-length digit runs; a `secrets.token_urlsafe`
value looks like none of those, so a link logged at INFO would be shipped to
whatever aggregates the logs and would sit there, valid, for the token's whole
lifetime. Printing is what keeps it in the operator's terminal and out of the
log stream.

What *is* logged — by every backend, through the logger, at INFO — is that a mail
of a given purpose went to a given address. That is the operational fact
(delivery is working, the flow was reached); the token is not.

## What happens when sending fails

`send()` raises `EmailError`, and the caller decides. There is no swallowing
here, and that is the opposite of `dough/services/audit.py`'s rule, on purpose:
an audit row that failed to write leaves the recorded operation correct, whereas
a reset mail that failed to send leaves a person waiting forever for a link that
does not exist. The route catches it and says so.

`EmailService._send` *enforces* that rather than trusting it: anything else a
backend raises is converted, because every caller catches `EmailError` and
nothing wider. A backend that raised something else would skip past all of them
and 500 the request instead — which is what `/settings/email` did, after
committing the new address, under a page that said nothing had changed.

The one thing a caller must *not* do is let the failure change its response —
see `dough/blueprints/auth.py`. "We could not send that mail" reveals that the
address matched an account.
"""

from __future__ import annotations

import logging
import smtplib
import sys
from email.message import EmailMessage as _MimeMessage

from flask import current_app

__all__ = [
    'ConsoleBackend',
    'EmailBackend',
    'EmailError',
    'EmailMessage',
    'EmailService',
    'MemoryBackend',
    'SmtpBackend',
    'build_backend',
    'current_email',
]

logger = logging.getLogger('dough.email')


class EmailError(Exception):
    """A message could not be delivered. Carries an operator-facing reason."""


class EmailMessage:
    """One outbound mail, backend-independent.

    A small class rather than a tuple because `purpose` is the field that
    matters most and is the one a positional tuple would make invisible at the
    call site. `MemoryBackend` assertions read `sent[0].purpose`, which is the
    question a test actually has ("was that the *reset* mail?").
    """

    __slots__ = ('to', 'subject', 'body', 'purpose')

    def __init__(self, to, subject, body, purpose):
        self.to = to
        self.subject = subject
        self.body = body
        self.purpose = purpose

    def __repr__(self):
        # Deliberately no body. This object holds a live token, and a repr is
        # the thing that ends up in a traceback, a debugger transcript, or a
        # pytest assertion diff -- three places nobody chose to put a secret.
        return f'<EmailMessage to={self.to!r} purpose={self.purpose!r}>'


class EmailBackend:
    """Where a message goes. One method, so a fourth backend is one class."""

    name = 'base'

    def send(self, message):
        raise NotImplementedError


class ConsoleBackend(EmailBackend):
    """Print the mail to stdout. The default, and the reason the flow works
    out of the box on an instance with no mail server.

    `print` rather than `logger.info` — see the module docstring. This is the one
    place in the application that deliberately renders a live credential to an
    output stream, and confining it to the terminal is what makes that
    acceptable.
    """

    name = 'console'

    def __init__(self, stream=None):
        # Resolved at send time rather than captured here, so a test that
        # redirects stdout after constructing the service still sees the output.
        self._stream = stream

    def send(self, message):
        stream = self._stream or sys.stdout
        text = (
            '\n'
            '─── dough: outbound mail ' + '─' * 40 + '\n'
            f'To:      {message.to}\n'
            f'Subject: {message.subject}\n'
            f'Purpose: {message.purpose}\n'
            '\n'
            f'{message.body}\n'
            '─' * 64 + '\n')
        try:
            stream.write(text)
        except UnicodeEncodeError:
            # The stream cannot represent every character in the message, and
            # the frame rules are the usual culprit -- U+2500 is absent from
            # cp1252, which is what `sys.stdout` uses on a Windows console.
            #
            # Degrading rather than raising, because of what is in `text`: the
            # link. This backend *is* the delivery mechanism, so a refusal here
            # is a verification mail that was never sent, and losing it over a
            # decoration is the worst possible trade. The link itself is
            # `secrets.token_urlsafe` and survives any encoding intact.
            #
            # `_send` would convert the raise into an `EmailError` and the route
            # would report a delivery failure. That message would be true and
            # useless: nothing the operator can configure fixes their terminal's
            # code page, and the mail they need is right there.
            encoding = getattr(stream, 'encoding', None) or 'ascii'
            stream.write(text.encode(encoding, 'replace').decode(encoding))
        stream.flush()


class MemoryBackend(EmailBackend):
    """Keep every message in a list. Used by the test suite.

    The list is per-instance rather than module-global for the same reason
    `LoginThrottle` is installed per application: the suite builds many
    applications in one process, and a shared list would let one test see
    another's mail.
    """

    name = 'memory'

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def clear(self):
        self.sent.clear()


class SmtpBackend(EmailBackend):
    """Actually send it.

    STARTTLS by default. There is no switch here to turn it off: the payload is
    a credential that grants access to an account, and an operator who needs
    plaintext SMTP has a mail server problem rather than a configuration
    preference this application should encode.
    """

    name = 'smtp'

    #: Set by `EmailService` after construction. An attribute rather than a
    #: constructor argument because it is a property of the *application's*
    #: identity, not of the transport, and the console and memory backends have
    #: no use for it.
    from_address = 'dough@localhost'

    def __init__(self, *, host, port=587, username='', password='',
                 use_tls=True, timeout=10):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, message):
        mime = _MimeMessage()
        mime['From'] = self.from_address
        mime['To'] = message.to
        mime['Subject'] = message.subject
        mime.set_content(message.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(mime)
        except (smtplib.SMTPException, OSError) as exc:
            # The exception type and the host, never the exception's own text:
            # an SMTP failure message can quote the message it was carrying, and
            # this one is carrying a reset link. Same rule as the logging
            # module's "type and message only" for tracebacks, one notch
            # stricter because of what the payload is.
            raise EmailError(
                f'SMTP delivery to {self.host}:{self.port} failed '
                f'({type(exc).__name__}).') from None


def build_backend(config):
    """Pick a backend from configuration. Unknown names fail loudly.

    A default-to-console fallback was considered and rejected: `MAIL_BACKEND=smpt`
    would then silently print reset links to a production terminal that nobody is
    reading, and the symptom is a password-reset feature that appears to work and
    delivers nothing.
    """
    name = (config.get('MAIL_BACKEND') or 'console').strip().lower()
    if name == 'console':
        return ConsoleBackend()
    if name == 'memory':
        return MemoryBackend()
    if name == 'smtp':
        host = (config.get('MAIL_SERVER') or '').strip()
        if not host:
            raise EmailError(
                'MAIL_BACKEND=smtp requires MAIL_SERVER to name a mail host.')
        return SmtpBackend(host=host, port=config.get('MAIL_PORT', 587),
                           username=config.get('MAIL_USERNAME', ''),
                           password=config.get('MAIL_PASSWORD', ''),
                           use_tls=config.get('MAIL_USE_TLS', True))
    raise EmailError(
        f'Unknown MAIL_BACKEND {name!r}; expected console, memory or smtp.')


class EmailService:
    """The two messages this application sends, and where they go.

    Installed on `app.extensions['dough_email']` by `init_app`, exactly as
    `AIService` and the login throttle are, so the suite's many applications
    cannot share one and a test can swap the backend without touching a module
    global.
    """

    def __init__(self, backend, *, from_address='dough@localhost'):
        self.backend = backend
        self.from_address = from_address
        # Only SmtpBackend has a use for it, but setting it unconditionally
        # keeps `build_backend` free of a special case and means a fourth
        # backend that needs one gets it without changing this line.
        setattr(backend, 'from_address', from_address)

    @classmethod
    def init_app(cls, app, backend=None):
        service = cls(backend or build_backend(app.config),
                      from_address=app.config.get('MAIL_FROM',
                                                  'dough@localhost'))
        app.extensions['dough_email'] = service
        return service

    # -- the two messages ---------------------------------------------------

    def send_verification_email(self, to, link, *, username=None):
        """Ask somebody to prove they can read mail at this address."""
        who = f'Hi {username},\n\n' if username else 'Hi,\n\n'
        return self._send(EmailMessage(
            to=to,
            subject='Confirm your email address · Dough',
            body=(
                f'{who}'
                'Confirm this address and Dough can reach you about your '
                'account — including if you ever need to reset your password.\n'
                '\n'
                f'{link}\n'
                '\n'
                'The link works once and expires. If you did not create a Dough '
                'account, you can ignore this message; nothing has been set up '
                'in your name.\n'),
            purpose='verify_email'))

    def send_password_reset_email(self, to, link, *, username=None):
        """Hand somebody the one link that can set a new password.

        The body says what to do if the request was not theirs, and it says the
        *useful* version of that: not "contact support", which this application
        does not have, but "your password has not changed" — which is the fact
        that actually tells a worried reader whether they need to act.
        """
        who = f'Hi {username},\n\n' if username else 'Hi,\n\n'
        return self._send(EmailMessage(
            to=to,
            subject='Reset your Dough password',
            body=(
                f'{who}'
                'Somebody asked to reset the password for your Dough account. '
                'If that was you, set a new one here:\n'
                '\n'
                f'{link}\n'
                '\n'
                'The link works once and expires shortly. Using it will sign you '
                'out everywhere and stop every API token this account has '
                'issued.\n'
                '\n'
                'If it was not you, nothing has happened — your password has not '
                'changed and this link can be ignored.\n'),
            purpose='password_reset'))

    # -- delivery -----------------------------------------------------------

    def _send(self, message):
        try:
            self.backend.send(message)
        except EmailError:
            raise
        except Exception as exc:
            # Every caller in the application catches `EmailError` and nothing
            # wider, because that is the contract this module's docstring
            # states. A backend that raises anything else therefore does not
            # produce "we could not send that mail" -- it produces a 500, on a
            # route that has already committed its database work. That is how
            # `/settings/email` came to save a new address and then answer with
            # an error page reading "nothing was changed".
            #
            # So the contract is enforced here rather than trusted. A backend is
            # the one part of this module that talks to the outside world; it is
            # the last place to assume only the anticipated exceptions arrive.
            #
            # Type only, never the exception's own text -- an SMTP failure can
            # quote the message it was carrying, and that message holds a live
            # token. Same rule as `SmtpBackend.send`, applied to the failures it
            # did not anticipate.
            raise EmailError(
                f'The {self.backend.name} mail backend failed '
                f'({type(exc).__name__}).') from None
        # The fact, not the payload. `to` and `purpose` are what answer "is
        # delivery working, and was this flow reached"; the body holds the token
        # and is never passed to the logger.
        logger.info('Sent %s mail to %s via %s', message.purpose, message.to,
                    self.backend.name)
        return message


def current_email():
    """The service this application installed.

    Mirrors `dough.ai.service.current_ai()`. A missing entry is a wiring bug
    rather than a runtime condition, so it raises rather than building a default
    — a lazily-constructed fallback would send real mail from a test.
    """
    service = current_app.extensions.get('dough_email')
    if service is None:
        raise EmailError(
            'No email service installed. EmailService.init_app(app) must run '
            'in create_app().')
    return service
