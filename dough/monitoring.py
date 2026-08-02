"""Error monitoring: what leaves this process when something breaks.
[Phase 10.7]

Allowed:   flask (current_app, request, session, g), dough.logging, stdlib
Must not:  app, models, blueprints, render_template/url_for/jsonify

## What this module is actually for

Not "add Sentry". Sentry is four lines. This module exists because an error
reporter is the one component whose *entire job* is to take data out of the
process and send it to a third party, on the code path where the application has
already stopped behaving as designed — which is precisely when local variables
hold half-parsed provider responses, request bodies and, on this application,
somebody's bank data.

So the interesting code below is the scrubbing, and the interesting decision is
that it is **deny-by-default**: local variables are switched off wholesale
rather than filtered, because a filter has to anticipate what it is filtering
and a stack frame in this application can hold literally anything.

## It reuses the redactor rather than adding one

`dough/logging.py` already owns `REDACTED_KEYS` and `scrub()`, and they are
already the thing that keeps credentials out of the log stream. A second list
here would be a second thing to keep in sync, and the failure mode of the two
drifting is that one surface silently stops protecting something. This imports
them.

The one thing it adds on top is financial: a log line naming a merchant is
ordinary, and an error report carrying a household's transaction list to a
third-party service is not. That is `_FINANCIAL_KEYS`.

## Optional by construction

`sentry-sdk` is an optional dependency and this module works without it. An
installation with no `SENTRY_DSN` gets a no-op, and one whose DSN is set but
whose package is missing gets a warning rather than a failed boot — an error
reporter must never be the reason an application cannot start, which would be
the most ironic possible outage.
"""

from __future__ import annotations

import logging

from dough.logging import REDACTED, REDACTED_KEYS, scrub

logger = logging.getLogger('dough.monitoring')

__all__ = ['before_send', 'init_app', 'is_enabled']

#: Names that make a field financial rather than merely private. Kept separate
#: from `REDACTED_KEYS` because they are a different category with a different
#: justification: those are credentials, which must never leave the process at
#: all; these are the user's money, which must not leave it *to a third party*
#: but is perfectly fine in a local log the operator already has.
_FINANCIAL_KEYS = (
    'amount', 'balance', 'transaction', 'merchant', 'account_name',
    'holding', 'shares', 'ticker', 'budget', 'net_worth', 'salary',
    'iban', 'auth_blob', 'plaid', 'institution',
)

_ALL_SENSITIVE = tuple(REDACTED_KEYS) + _FINANCIAL_KEYS

#: Request headers that are always dropped. `Cookie` carries the session and
#: `Authorization` carries a bearer token, so both are credentials by
#: definition; the SDK's own default scrubber covers these, and doing it here
#: too means the guarantee does not depend on that default staying put.
_DROP_HEADERS = ('cookie', 'authorization', 'x-api-key', 'proxy-authorization')

_enabled = False


def is_enabled():
    """Whether reports are actually being sent."""
    return _enabled


def _sensitive(name):
    lowered = str(name).lower()
    return any(bad in lowered for bad in _ALL_SENSITIVE)


def _clean(value, depth=0):
    """Recursively redact a structure bound for the wire.

    Depth-limited because an event is arbitrary nested data and a cyclic or
    very deep structure would otherwise turn error reporting into a hang — on
    the path that runs when things are already going wrong.
    """
    if depth > 6:
        return REDACTED

    if isinstance(value, dict):
        return {
            key: (REDACTED if _sensitive(key) else _clean(val, depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return scrub(value)
    return value


def before_send(event, _hint=None):
    """The last thing that runs before an event leaves the process.

    Written as a plain function taking and returning a dict so it is testable
    without the SDK installed, without a network, and without an application —
    which matters because this is the function whose failure mode is silent
    data exfiltration, and it should be the easiest thing here to write a test
    against.

    Returning `None` would drop the event entirely. It never does: an error
    report that is dropped because one field looked sensitive is an outage
    nobody can see.
    """
    if not event:
        return event

    # 1. Local variables, wholesale. Not filtered -- removed.
    #
    # A stack frame in this application can hold a decrypted Plaid token, a
    # DataFrame of somebody's transactions, or a prompt containing their whole
    # financial context. There is no key-name filter that reliably catches all
    # of that, because the sensitive thing is often the *value* of a variable
    # named `df` or `rows`. The tracebacks stay; only the variable dumps go.
    for exception in (event.get('exception', {}) or {}).get('values', []) or []:
        for frame in (exception.get('stacktrace', {}) or {}).get('frames', []) or []:
            frame.pop('vars', None)

    # 2. Request data: headers, cookies, query string, body.
    request = event.get('request')
    if isinstance(request, dict):
        request.pop('cookies', None)
        headers = request.get('headers')
        if isinstance(headers, dict):
            request['headers'] = {
                key: (REDACTED if key.lower() in _DROP_HEADERS else scrub(str(val)))
                for key, val in headers.items()
            }
        # The body is dropped rather than scrubbed. On this application a POST
        # body is a password, a CSV of somebody's statement, or a chat message
        # about their finances -- there is no version of it worth the risk.
        request.pop('data', None)
        if 'query_string' in request:
            request['query_string'] = scrub(str(request['query_string']))

    # 3. Everything else the event carries.
    for key in ('extra', 'contexts', 'tags', 'breadcrumbs'):
        if key in event:
            event[key] = _clean(event[key])

    # 4. The user. An id and a username are what make a report actionable --
    #    "which account hit this" is the first question -- and an email address
    #    is not needed to answer it.
    user = event.get('user')
    if isinstance(user, dict):
        event['user'] = {key: user[key] for key in ('id', 'username', 'household')
                         if key in user}

    # 5. The message itself, last, so anything the steps above reassembled is
    #    still covered.
    if isinstance(event.get('message'), str):
        event['message'] = scrub(event['message'])

    return event


def init_app(app):
    """Wire up error reporting, if this deployment has any configured.

    Silent and successful when `SENTRY_DSN` is unset, which is the state of
    every development machine and the test suite.
    """
    global _enabled

    dsn = (app.config.get('SENTRY_DSN') or '').strip()
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        # A warning, never a failure. An error reporter that stops the
        # application from booting is a worse outage than the one it was
        # installed to catch.
        app.logger.warning(
            'SENTRY_DSN is set but sentry-sdk is not installed; error '
            'reporting is off. Install it with: pip install sentry-sdk')
        return

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=app.config.get('APP_ENV') or 'production',
            release=app.config.get('RELEASE') or None,
            integrations=[
                FlaskIntegration(),
                # Breadcrumbs from INFO, events from ERROR. Without this the SDK
                # would also *report* every logger.error as its own issue, which
                # on this application means a failed institution sync -- an
                # expected, handled condition -- becomes a page.
                LoggingIntegration(level=logging.INFO,
                                   event_level=logging.ERROR),
            ],
            # The SDK's default behaviour is to detect every library it knows
            # about and patch it. Turned off, for a reason that is not
            # hypothetical: it broke this application the first time it was run.
            # `huggingface_hub` is present in this environment as somebody's
            # transitive dependency, the SDK auto-enabled its integration, and
            # the version installed did not have the attribute the integration
            # patches -- so `create_app` raised an AttributeError at import
            # time and the whole application failed to boot.
            #
            # That is the exact outage this module's docstring says an error
            # reporter must never cause, arriving through the one mechanism
            # nobody reviews: a hook installed by a library the application does
            # not use and did not ask to instrument. The integrations we want
            # are two, they are listed above, and an unreviewed set of patches
            # on a financial application is not worth the convenience.
            auto_enabling_integrations=False,
            before_send=before_send,
            # Off, both of them. PII here means somebody's financial life, and
            # `max_request_body_size='never'` is a second lock on the same door
            # `before_send` closes: defence that does not depend on our own code
            # being right.
            send_default_pii=False,
            max_request_body_size='never',
            traces_sample_rate=float(
                app.config.get('SENTRY_TRACES_SAMPLE_RATE', 0.0)),
        )
    except Exception as exc:
        # The backstop behind the specific fix above. `sentry_sdk.init` reaches
        # into other libraries, so the set of things that can go wrong here is
        # not bounded by anything in this repository -- and none of them is a
        # reason for the application to be down. Bare `Exception` on purpose.
        app.logger.warning('Error reporting could not start (%s: %s); '
                           'continuing without it.', type(exc).__name__, exc)
        return

    _install_user_scope(app)
    _enabled = True
    app.logger.info('Error reporting enabled (environment=%s)',
                    app.config.get('APP_ENV'))


def _install_user_scope(app):
    """Tag each event with who hit it, without naming them to a third party.

    The user id and household id answer "whose request was this" and are
    meaningless outside this database — which is the property that makes them
    safe to send. The email address would identify a real person to a vendor and
    is deliberately not attached; `before_send` drops it as well, so this is not
    the only thing standing between an address and the wire.
    """
    import sentry_sdk
    from flask import session

    @app.before_request
    def _tag_actor():
        try:
            scope = sentry_sdk.get_current_scope()
        except AttributeError:      # older SDK
            return
        user_id = session.get('user_id')
        if user_id is not None:
            scope.set_user({'id': user_id})
        from dough.tenancy import current_household
        household = current_household()
        if household is not None:
            scope.set_tag('household', household)
