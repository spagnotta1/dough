"""Structured logging: one JSON object per line, with the context attached.

Allowed: flask, stdlib
Must not: app, models, dough.blueprints, dough.services

Before this, the application logged 34 free-text lines through `app.logger` and
two module loggers, formatted for a human watching a terminal. That is fine
until something goes wrong in production, where the questions are "show me
everything for the request the user is complaining about" and "did this sync
touch the wrong household" — neither of which a string can answer.

Three ideas, in the order they matter:

**A correlation id per request, carried in a ContextVar.** Not `flask.g`, for
the same reason `dough/tenancy.py` uses a ContextVar: the sync engine and the
scheduler run on worker threads with no request at all, and they need the same
field so their lines join up with the request that triggered them. A ContextVar
also has the property that matters here -- a new thread starts empty -- so a
worker cannot silently inherit and mislabel itself with the previous request's
id. Background work therefore takes a *job* id instead, and both are emitted
under `trace_id` so one filter finds either.

**Context is added by a filter, not by call sites.** Every existing
`logger.warning(...)` gains household, user, path and trace id without being
touched. Asking 34 call sites to remember to pass context is how logs end up
with the context on the lines that did not need it.

**Redaction is the formatter's job.** A log line is written by whoever is
nearest the failure, often in a hurry, and `logger.info('token=%s', tok)` is a
completely reasonable thing to type. The formatter is the last place that can
still say no, so the deny-list runs there as well as in the audit service.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

from flask import g, has_request_context, request, session

#: The current request's id, or a background job's. Empty in a fresh thread.
_trace_id: ContextVar = ContextVar('dough_trace_id', default=None)
#: What kind of work this trace is: 'request', 'sync', 'ai', 'scheduler'.
_trace_kind: ContextVar = ContextVar('dough_trace_kind', default=None)

#: Header a caller may supply to join its own trace, and the one we always
#: return. Accepting an inbound value is what makes a proxy's log and this
#: application's log line up; it is sanitised because it is attacker-controlled.
TRACE_HEADER = 'X-Request-ID'
_SAFE_TRACE = re.compile(r'^[A-Za-z0-9._\-]{1,64}$')

#: Substrings that make a log field's *name* unloggable. Same list as the audit
#: service's, kept separate on purpose: they protect different surfaces and
#: should be able to diverge without one silently loosening the other.
REDACTED_KEYS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey',
    'authorization', 'cookie', 'credential', 'access_token', 'refresh_token',
    'account_number', 'routing', 'ssn', 'card', 'cvv',
)

#: Values that look like a credential wherever they appear in a message.
_SECRETISH = re.compile(
    r'sk-[A-Za-z0-9_\-]{16,}'
    r'|access-(?:sandbox|development|production)-[0-9a-f\-]{8,}'
    r'|\b\d{13,19}\b'
)

REDACTED = '[redacted]'

#: Fields LogRecord always has. Anything else a caller attached via `extra=`
#: is merged into the JSON object.
_STANDARD = frozenset((
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs',
    'message', 'msg', 'name', 'pathname', 'process', 'processName',
    'relativeCreated', 'stack_info', 'thread', 'threadName', 'taskName',
))


def new_trace_id():
    return uuid.uuid4().hex[:16]


def current_trace_id():
    """The active trace id, or None. Safe to call from anywhere."""
    return _trace_id.get()


def bind_trace(trace_id=None, kind='request'):
    """Start a trace on this context. Returns the id.

    Called by the request hook, and by any background worker that wants its
    lines to be findable -- `bind_trace(kind='sync')` at the top of a sync gives
    every line that run emits a shared id.
    """
    trace_id = trace_id or new_trace_id()
    _trace_id.set(trace_id)
    _trace_kind.set(kind)
    return trace_id


def scrub(text):
    """Redact anything credential-shaped in a rendered string."""
    if not isinstance(text, str):
        return text
    return _SECRETISH.sub(REDACTED, text)


class ContextFilter(logging.Filter):
    """Attach trace, household, user and request fields to every record.

    Everything here is best-effort. Logging must never be the thing that raises
    -- a filter that threw inside an exception handler would replace a real
    error with a confusing one -- so each lookup is guarded and simply omits the
    field it could not resolve.
    """

    def filter(self, record):
        record.trace_id = _trace_id.get()
        record.trace_kind = _trace_kind.get()

        record.household_id = None
        record.user_id = None
        try:
            from dough.tenancy import current_household
            record.household_id = current_household()
        except Exception:
            pass

        if has_request_context():
            try:
                record.user_id = session.get('user_id')
            except Exception:
                pass
            try:
                record.method = request.method
                # `request.path` only -- never `full_path`. Query strings on this
                # application carry filters, and a category or a date range is
                # not something to spray into a log aggregator.
                record.path = request.path
                record.endpoint = request.endpoint
            except Exception:
                pass
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record):
        payload = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))
                  + '.%03dZ' % record.msecs,
            'level': record.levelname,
            'logger': record.name,
            'msg': scrub(record.getMessage()),
        }
        for field in ('trace_id', 'trace_kind', 'household_id', 'user_id',
                      'method', 'path', 'endpoint'):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        for key, value in record.__dict__.items():
            if key in _STANDARD or key in payload or key.startswith('_'):
                continue
            if any(bad in key.lower() for bad in REDACTED_KEYS):
                payload[key] = REDACTED
            else:
                payload[key] = scrub(value) if isinstance(value, str) else value

        if record.exc_info:
            # The type and message, never the frames. A traceback in a log
            # aggregator is fine; a traceback is also the most common way local
            # variables holding secrets escape. Flask still writes the full
            # traceback through its own handler in development.
            exc_type, exc_value, _tb = record.exc_info
            payload['exc_type'] = getattr(exc_type, '__name__', str(exc_type))
            payload['exc_msg'] = scrub(str(exc_value))

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """The development format: readable, with the trace id kept."""

    def format(self, record):
        trace = getattr(record, 'trace_id', None)
        prefix = f'[{trace}] ' if trace else ''
        household = getattr(record, 'household_id', None)
        scope = f'(h{household}) ' if household else ''
        return (f'{self.formatTime(record, "%H:%M:%S")} {record.levelname:<8} '
                f'{prefix}{scope}{record.name}: {scrub(record.getMessage())}')


def configure_logging(app):
    """Install the handler, the filter and the request hooks.

    JSON unless the application is in debug, where a human is reading the
    terminal and a wall of JSON is worse than useless. `LOG_JSON` overrides
    both ways, so the production format can be reproduced locally when the
    question is about the logs themselves.
    """
    use_json = app.config.get('LOG_JSON')
    if use_json is None:
        use_json = not app.config.get('DEBUG', False)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if use_json else HumanFormatter())
    handler.addFilter(ContextFilter())

    level = getattr(logging, str(app.config.get('LOG_LEVEL', 'INFO')).upper(),
                    logging.INFO)

    root = logging.getLogger()
    # Replace rather than append: Flask and werkzeug install their own, and two
    # handlers means every line twice, which is how a log becomes something
    # nobody reads.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    app.logger.handlers = []
    app.logger.propagate = True
    app.logger.setLevel(level)

    # werkzeug's access log is already one line per request and duplicates what
    # the hooks below record with more context.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    @app.before_request
    def _begin_trace():
        supplied = request.headers.get(TRACE_HEADER)
        if supplied and not _SAFE_TRACE.match(supplied):
            # Attacker-controlled and echoed back to the client and into the
            # logs. A rejected value costs one uncorrelated request; an accepted
            # one lets a caller forge log entries or inject into whatever reads
            # them.
            supplied = None
        g._trace_started = time.monotonic()
        bind_trace(supplied, kind='request')
        return None

    @app.after_request
    def _finish_trace(response):
        trace = current_trace_id()
        if trace:
            response.headers[TRACE_HEADER] = trace
        started = g.pop('_trace_started', None)
        if started is not None and not request.path.startswith('/health'):
            app.logger.info(
                'request', extra={
                    'status': response.status_code,
                    'duration_ms': round((time.monotonic() - started) * 1000, 1),
                })
        return response
