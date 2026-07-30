"""Structured logging, health probes, and what a user sees when it breaks.

The three are one file because they answer one question: when something goes
wrong in production, can it be found? A trace id that is not on the error page
is a trace id nobody will ever quote; a readiness probe that returns 200 while
the database is down is worse than no probe.

What is asserted here is mostly *negative*: no stack traces to users, no
secrets in log lines, no configuration in the health body. Those are the
properties that decay silently -- a positive assertion fails the build when
somebody breaks it, but nothing fails when a new `logger.info` starts printing
an access token.
"""

import json
import logging

import pytest

from dough import logging as dough_logging
from dough.logging import (ContextFilter, HumanFormatter, JsonFormatter,
                           TRACE_HEADER, bind_trace, current_trace_id, scrub)


def _format(record, formatter=None):
    ContextFilter().filter(record)
    return (formatter or JsonFormatter()).format(record)


def _record(msg, *args, **extra):
    record = logging.LogRecord('test', logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# The log line is machine-readable and carries context
# ---------------------------------------------------------------------------

def test_json_formatter_emits_one_parseable_object():
    payload = json.loads(_format(_record('hello')))
    assert payload['msg'] == 'hello'
    assert payload['level'] == 'INFO'
    assert payload['logger'] == 'test'
    assert payload['ts'].endswith('Z')


def test_extra_fields_are_merged_into_the_object():
    payload = json.loads(_format(_record('synced', sync_id='s-42', accounts=3)))
    assert payload['sync_id'] == 's-42'
    assert payload['accounts'] == 3


def test_the_trace_id_travels_with_the_context_not_the_call_site():
    """The point of the ContextVar: no call site passes this."""
    bind_trace('abc123', kind='sync')
    try:
        payload = json.loads(_format(_record('working')))
        assert payload['trace_id'] == 'abc123'
        assert payload['trace_kind'] == 'sync'
    finally:
        bind_trace(None, kind=None)


def test_a_fresh_thread_does_not_inherit_a_trace_id():
    """Why this is a ContextVar rather than a global or flask.g.

    A worker that inherited the previous request's id would file its lines under
    a request it has nothing to do with -- which is worse than no correlation,
    because it is confidently wrong.
    """
    import threading
    bind_trace('outer-request')
    seen = []
    thread = threading.Thread(target=lambda: seen.append(current_trace_id()))
    thread.start()
    thread.join()
    assert seen == [None]


def test_the_request_hook_binds_household_and_user(client, app):
    """Context arrives without the call site asking for it."""
    with app.test_request_context('/transactions'):
        record = _record('anything')
        ContextFilter().filter(record)
        assert record.path == '/transactions'
        assert record.method == 'GET'
        assert record.household_id == app.config['DEFAULT_HOUSEHOLD_ID']


def test_the_query_string_is_never_logged(app):
    """Filters are the query string on this app, and a category is not a thing
    to spray into a log aggregator. `request.path`, never `full_path`."""
    with app.test_request_context('/transactions?category=Therapy&q=divorce'):
        record = _record('anything')
        ContextFilter().filter(record)
        assert record.path == '/transactions'
        assert 'Therapy' not in json.dumps(record.__dict__, default=str)


# ---------------------------------------------------------------------------
# Nothing sensitive reaches the log
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('secret', [
    'sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    'access-production-1a2b3c4d-5e6f-7890-abcd-ef1234567890',
    '4111111111111111',
])
def test_credential_shaped_values_are_scrubbed_from_the_message(secret):
    """At the formatter, because `logger.info('token=%s', tok)` is a reasonable
    thing to type in a hurry and the formatter is the last place to say no."""
    payload = json.loads(_format(_record('calling with %s', secret)))
    assert secret not in payload['msg']
    assert dough_logging.REDACTED in payload['msg']


@pytest.mark.parametrize('field', ['password', 'api_key', 'access_token',
                                   'authorization', 'card_number', 'ssn'])
def test_extra_fields_with_sensitive_names_are_redacted(field):
    payload = json.loads(_format(_record('x', **{field: 'value'})))
    assert payload[field] == dough_logging.REDACTED


def test_the_human_formatter_scrubs_too():
    """Development is where the secret actually got printed the first time."""
    line = _format(_record('key=%s', 'sk-ant-api03-BBBBBBBBBBBBBBBBBBBB'),
                   HumanFormatter())
    assert 'sk-ant' not in line


def test_an_exception_logs_its_type_and_message_but_never_its_frames():
    """A traceback in an aggregator is fine. A traceback is also the most common
    way a local variable holding a secret escapes into one."""
    try:
        raise ValueError('token was sk-ant-api03-CCCCCCCCCCCCCCCCCCCC')
    except ValueError:
        import sys
        record = _record('failed')
        record.exc_info = sys.exc_info()
        payload = json.loads(_format(record))
    assert payload['exc_type'] == 'ValueError'
    assert 'sk-ant' not in payload['exc_msg']
    assert 'Traceback' not in json.dumps(payload)
    assert 'test_observability' not in payload.get('exc_msg', '')


def test_scrub_passes_non_strings_through():
    assert scrub(42) == 42
    assert scrub(None) is None


# ---------------------------------------------------------------------------
# Correlation across the request boundary
# ---------------------------------------------------------------------------

def test_every_response_carries_a_trace_id(client):
    assert client.get('/health/live').headers[TRACE_HEADER]


def test_a_supplied_trace_id_is_honoured(client):
    """What makes a proxy's access log and this application's log line up."""
    resp = client.get('/health/live', headers={TRACE_HEADER: 'edge-abc-123'})
    assert resp.headers[TRACE_HEADER] == 'edge-abc-123'


@pytest.mark.parametrize('hostile', [
    'has spaces', '<script>alert(1)</script>', 'a' * 200,
    'semi;colon', '../../etc/passwd', 'null\x00byte',
])
# CRLF injection is deliberately absent: werkzeug refuses to construct a header
# containing a newline, so the test client cannot send one and a test for it
# would assert something about werkzeug rather than about this application. The
# allowlist regex rejects it regardless.
def test_a_hostile_trace_id_is_replaced_rather_than_echoed(client, hostile):
    """Inbound, attacker-controlled, echoed back and written to the log.

    Accepting it lets a caller forge log entries or inject into whatever reads
    them. Rejecting costs one uncorrelated request.
    """
    resp = client.get('/health/live', headers={TRACE_HEADER: hostile})
    assert resp.headers[TRACE_HEADER] != hostile
    assert resp.headers[TRACE_HEADER].isalnum()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_live_says_nothing_but_ok(client):
    """It must not fail for any reason a restart would not fix -- so it touches
    nothing. A liveness probe that checked the database would restart a healthy
    application to punish it for a dependency's outage."""
    resp = client.get('/health/live')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_ready_reports_each_check(client):
    body = client.get('/health/ready').get_json()
    assert body['status'] == 'ok'
    assert body['checks'] == {'database': True, 'migrations': True,
                              'configuration': True}


def test_ready_returns_503_when_the_database_is_unreachable(client, monkeypatch):
    from dough.blueprints import health
    monkeypatch.setattr(health, '_database_reachable', lambda: False)
    resp = client.get('/health/ready')
    assert resp.status_code == 503
    assert resp.get_json()['status'] == 'unavailable'
    assert resp.get_json()['checks']['database'] is False


def test_ready_returns_503_when_required_configuration_is_missing(client, app):
    app.config['SECRET_KEY'] = ''
    assert client.get('/health/ready').status_code == 503


def test_the_health_body_discloses_nothing_but_names_and_booleans(client):
    """Both probes are public, deliberately: a health check behind a login is
    not a health check. That makes the body an unauthenticated disclosure
    surface, so it carries check names and booleans and nothing else."""
    body = client.get('/health/ready').get_json()
    assert set(body) == {'status', 'checks'}
    for value in body['checks'].values():
        assert isinstance(value, bool)
    text = json.dumps(body)
    for leak in ('sqlite', 'SECRET', 'sqlalchemy', '/', '20260727'):
        assert leak not in text


def test_health_is_reachable_without_a_session(app):
    """The one property that makes these useful: a probe holds no session."""
    from app import create_app
    guarded = create_app(test_config={
        'TESTING': True, 'AUTH_ENABLED': True, 'SYNC_AUTO_ENABLED': False,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://'})
    with guarded.app_context():
        from models import db
        db.create_all()
    assert guarded.test_client().get('/health/live').status_code == 200


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_a_404_renders_a_page_not_a_traceback(client):
    resp = client.get('/no-such-page')
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert 'Traceback' not in body
    assert 'werkzeug' not in body.lower()


def test_an_error_page_quotes_the_trace_id(client):
    """The one piece of internal detail a user gets, and the only one worth
    giving: it is the key that finds every log line for what just happened."""
    resp = client.get('/no-such-page')
    trace = resp.headers[TRACE_HEADER]
    assert trace in resp.get_data(as_text=True)


def test_an_unhandled_exception_returns_500_without_internals(app, client, caplog):
    @app.route('/boom-for-test')
    def boom():
        raise RuntimeError('secret internal detail sk-ant-api03-DDDDDDDDDDDDDDDD')

    app.config['PROPAGATE_EXCEPTIONS'] = False
    resp = client.get('/boom-for-test')
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert 'secret internal detail' not in body
    assert 'RuntimeError' not in body
    assert 'Traceback' not in body


def test_a_json_client_gets_json_and_a_browser_gets_html(client):
    html = client.get('/no-such-page')
    assert 'text/html' in html.headers['Content-Type']

    api = client.get('/no-such-page', headers={'Accept': 'application/json'})
    assert api.status_code == 404
    body = api.get_json()
    assert 'error' in body
    # The trace id is in the body as well as the header, because an API client
    # logging a failure keeps the body and rarely keeps the headers.
    assert body['trace_id'] == api.headers[TRACE_HEADER]
