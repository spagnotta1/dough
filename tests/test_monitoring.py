"""What leaves the process when something breaks.  [Phase 10.7]

`before_send` is the most consequential function in `dough/monitoring.py` and
the one with the quietest failure mode: if it lets something through, nothing
in this application reports it — the data simply arrives at a third party and
sits there. There is no error, no log line, and no way to notice from the
inside.

So it is written as a plain dict-in/dict-out function, and these tests call it
directly. No SDK, no network, no application. That is deliberate: a scrubber
that can only be tested through a live integration is a scrubber nobody tests.

The events below are shaped like real Sentry payloads. They are hand-built
rather than captured, because the point is to assert on the *categories* that
must never pass — stack-frame locals, request bodies, credentials, financial
data — and a captured fixture would only ever cover the shapes that happened to
occur on the day it was taken.
"""

import pytest

from dough.monitoring import before_send, is_enabled


def _event(**overrides):
    """A plausible Sentry event with something sensitive in every slot."""
    event = {
        'message': 'boom',
        'exception': {'values': [{
            'type': 'ValueError',
            'stacktrace': {'frames': [{
                'filename': 'dough/services/ledger.py',
                'function': 'import_csv',
                'vars': {
                    'password': 'hunter2',
                    'access_token': 'access-production-abc123def456',
                    'rows': [{'amount': -4.5, 'description': 'Coffee'}],
                    'harmless': 'ok',
                },
            }]},
        }]},
        'request': {
            'url': 'https://dough.example.com/upload',
            'method': 'POST',
            'headers': {
                'Cookie': 'session=abcdef',
                'Authorization': 'Bearer sk-ant-abcdefghijklmnop',
                'User-Agent': 'Mozilla/5.0',
            },
            'cookies': {'session': 'abcdef'},
            'data': {'password': 'hunter2', 'csv': 'date,amount\n2026-01-01,-40'},
            'query_string': 'token=sk-ant-abcdefghijklmnop',
        },
        'extra': {
            'household_balance': 4021.55,
            'plaid_item': 'access-production-zzz',
            'note': 'nothing secret',
        },
        'user': {'id': 7, 'username': 'sal', 'email': 'sal@example.com',
                 'ip_address': '198.51.100.4'},
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# The categories that must never leave
# ---------------------------------------------------------------------------

def test_stack_frame_locals_are_removed_wholesale(_=None):
    """Not filtered by name — removed.

    A frame in this application can hold a decrypted institution token, a
    DataFrame of somebody's transactions, or a prompt containing their whole
    financial context. The sensitive thing is frequently the *value* of a
    variable called `df` or `rows`, which no key-name filter catches. So the
    variable dump goes entirely and the traceback stays.
    """
    cleaned = before_send(_event())

    frame = cleaned['exception']['values'][0]['stacktrace']['frames'][0]
    assert 'vars' not in frame
    assert frame['function'] == 'import_csv', 'the traceback itself must survive'


def test_the_request_body_is_dropped(_=None):
    """On this application a POST body is a password, a bank statement, or a
    chat message about somebody's money. There is no version worth sending."""
    cleaned = before_send(_event())
    assert 'data' not in cleaned['request']


def test_cookies_and_authorization_never_leave(_=None):
    """Both are credentials by definition, not by heuristic."""
    cleaned = before_send(_event())

    assert 'cookies' not in cleaned['request']
    headers = cleaned['request']['headers']
    assert 'abcdef' not in str(headers)
    assert 'sk-ant-abcdefghijklmnop' not in str(headers)
    assert headers['User-Agent'] == 'Mozilla/5.0', (
        'a harmless header should survive — this is a scrubber, not a shredder')


def test_financial_values_do_not_reach_a_third_party(_=None):
    """The category `dough/logging.py` deliberately does not cover.

    A local log line naming a merchant is ordinary — the operator already has
    the database. The same string arriving at a vendor's servers is a different
    act, which is why `_FINANCIAL_KEYS` exists on top of the shared redactor.
    """
    cleaned = before_send(_event())

    serialized = str(cleaned)
    assert '4021.55' not in serialized
    assert 'access-production-zzz' not in serialized
    assert cleaned['extra']['note'] == 'nothing secret'


def test_the_email_address_is_not_attached_to_the_user(_=None):
    """An id answers "whose request was this". An address identifies a person.

    The id and username are meaningless outside this database, which is the
    property that makes them safe to send.
    """
    cleaned = before_send(_event())

    assert cleaned['user'] == {'id': 7, 'username': 'sal'}
    assert 'email' not in cleaned['user']
    assert 'ip_address' not in cleaned['user']


def test_credential_shaped_strings_are_scrubbed_wherever_they_appear(_=None):
    """The value scanner from `dough/logging.py`, reused rather than rewritten.

    It catches things that look like credentials in places no key-name filter
    would look — inside a message, a query string, a tag value.
    """
    cleaned = before_send(_event(
        message='failed with key sk-ant-abcdefghijklmnopqrst'))

    assert 'sk-ant-abcdefghijklmnopqrst' not in cleaned['message']
    assert 'sk-ant-abcdefghijklmnop' not in cleaned['request']['query_string']


def test_a_card_number_in_a_message_is_scrubbed(_=None):
    cleaned = before_send(_event(message='charge failed for 4111111111111111'))
    assert '4111111111111111' not in cleaned['message']


# ---------------------------------------------------------------------------
# It must not become the outage
# ---------------------------------------------------------------------------

def test_an_event_is_never_dropped_entirely(_=None):
    """Returning None would discard the report.

    An error that goes unreported because one field looked sensitive is an
    outage nobody can see — strictly worse than a redacted report.
    """
    assert before_send(_event()) is not None
    assert before_send({}) is not None


def test_a_deeply_nested_structure_terminates(_=None):
    """Depth-limited, because this runs when things are already going wrong.

    Arbitrary nested data is exactly what an error event carries, and a hang
    here would turn a handled exception into a stuck worker.
    """
    deep = {'a': None}
    node = deep
    for _ in range(50):
        node['a'] = {'a': None}
        node = node['a']

    cleaned = before_send(_event(extra=deep))
    assert cleaned is not None


def test_an_event_with_no_optional_sections_survives(_=None):
    """Sentry omits sections that do not apply; indexing them would raise."""
    assert before_send({'message': 'bare'})['message'] == 'bare'
    assert before_send({'exception': {'values': []}}) is not None
    assert before_send({'request': {}}) is not None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_monitoring_is_off_without_a_dsn(app):
    """The state of every development machine and the whole test suite."""
    assert app.config.get('SENTRY_DSN', '') == ''
    assert is_enabled() is False


@pytest.fixture()
def sentry_teardown():
    """Undo a real `sentry_sdk.init` before the next test runs.

    Necessary because `init` installs a *process-global* client. Without this,
    one test that boots with a DSN leaves every later test's `logger.error`
    being captured for delivery, and the interpreter pauses at exit trying to
    flush events to a hostname that does not resolve. The symptom appears in an
    unrelated test file, which is the worst kind of test pollution to chase.
    """
    yield
    import dough.monitoring as monitoring_module

    monitoring_module._enabled = False
    try:
        import sentry_sdk

        client = sentry_sdk.get_client()
        if client is not None:
            client.close(timeout=0.0)
        # `auto_enabling_integrations=False` here too, and for the same reason
        # `init_app` passes it: a bare `init()` re-runs the library detection
        # and re-raises the very AttributeError the test above exists to prove
        # cannot happen -- from a teardown, where it reads as an unrelated
        # failure in whatever test ran next.
        sentry_sdk.init(dsn='', auto_enabling_integrations=False)
    except ImportError:
        pass


def test_a_configured_dsn_never_stops_the_application_booting(tmp_path,
                                                              sentry_teardown):
    """An error reporter must never be why an application cannot start.

    Not hypothetical. The first run of this code raised `AttributeError` inside
    `sentry_sdk.init` and took `create_app` with it: the SDK auto-detects every
    library it knows about and patches it, `huggingface_hub` was present as
    somebody's transitive dependency, and the installed version did not have the
    attribute the integration patches. The application was down because of a
    library it does not use.

    Two fixes, both in `init_app`: `auto_enabling_integrations=False`, so only
    the two integrations we asked for are installed; and a `try/except` around
    the whole call, because the set of things that can fail in there is not
    bounded by anything in this repository.

    This test covers both paths and passes either way — with the SDK installed
    it exercises the real `init`, and without it the ImportError branch. That is
    the point: whichever the environment has, the application boots.
    """
    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'SENTRY_DSN': 'https://public@example.ingest.sentry.io/1',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'sentry.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    scheduler_module._scheduler = None

    assert application.test_client().get('/health/live').status_code == 200


def test_only_the_two_declared_integrations_are_installed(tmp_path,
                                                          sentry_teardown):
    """The regression guard for the boot failure above.

    Asserts the *cause* rather than the symptom: if `auto_enabling_integrations`
    is ever dropped, this fails immediately and by name, instead of the next
    person discovering it as an unrelated library's AttributeError during a
    deploy.
    """
    sentry_sdk = pytest.importorskip('sentry_sdk')

    import finance_sync.scheduler as scheduler_module
    from app import create_app

    scheduler_module._scheduler = None
    create_app(test_config={
        'TESTING': True,
        'SENTRY_DSN': 'https://public@example.ingest.sentry.io/1',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'integrations.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    scheduler_module._scheduler = None

    installed = set(sentry_sdk.get_client().integrations)
    # The SDK always adds its own always-on set (excepthook, dedupe, atexit and
    # friends); what must not appear is an integration for a third-party library
    # nobody asked to instrument.
    assert 'flask' in installed
    assert 'logging' in installed
    for uninvited in ('huggingface_hub', 'anthropic', 'sqlalchemy', 'requests',
                      'openai', 'celery', 'django'):
        assert uninvited not in installed, (
            f'{uninvited} was auto-enabled; auto_enabling_integrations must '
            'stay False -- see init_app for the boot failure this caused.')


def test_the_financial_key_list_extends_rather_than_replaces_the_log_redactor():
    """One list, extended — not a second copy that can drift.

    `dough/logging.py` already owns the credential names and is already what
    keeps them out of the log stream. Duplicating them here would create two
    things to maintain, and the failure mode of them drifting is that one
    surface silently stops protecting something.
    """
    from dough.logging import REDACTED_KEYS
    from dough.monitoring import _ALL_SENSITIVE

    assert set(REDACTED_KEYS) <= set(_ALL_SENSITIVE)
