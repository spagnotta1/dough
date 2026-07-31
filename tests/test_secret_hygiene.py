"""Secrets do not reach the log, the audit trail, or the repository.
[Phase 10.5, Part 8]

The three surfaces are separate because they fail separately and are protected
by different mechanisms:

- **The log** is protected by `dough/logging.py`'s formatter-level redaction,
  which applies to lines nobody remembered to think about. `tests/
  test_observability.py` covers that machinery; this file covers the *new*
  credentials Phase 10.5 introduced, which the pattern-based half of it does not
  recognise.
- **The audit trail** is protected by `dough/services/audit.py`'s redactor.
  Worth its own assertions because it is the one table nothing ever deletes
  from, so a secret written there is written permanently.
- **The repository** is protected by nothing but review, which is why it is
  checked mechanically here.

The unifying point, and the reason this file exists rather than three more tests
scattered across three suites: the credentials this phase added — a reset token,
a verification token, an API token, a Fernet key — are all high-entropy strings
that look like *nothing in particular*. `_SECRETISH` recognises `sk-…` keys,
Plaid tokens and card-length digit runs. It does not and cannot recognise
`secrets.token_urlsafe(32)`. So for these, "the redactor will catch it" is false,
and the protection has to be that the value is never handed to the logger in the
first place. That is a property of call sites, and these are the tests that hold
those call sites to it.
"""

import logging
import os
import re

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = 'hunter2boat'


@pytest.fixture()
def auth_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
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
def client(auth_app):
    return auth_app.test_client()


def _register(client, username='sal', email='sal@example.com'):
    return client.post('/register', data={
        'username': username, 'email': email,
        'password': PASSWORD, 'confirm': PASSWORD})


def _tokens_in(text):
    """Every high-entropy url-safe string in `text` that could be a credential.

    Deliberately crude and deliberately over-inclusive: the question is not
    "which of these is the token" but "is anything token-shaped here at all".
    A false positive costs a moment; a false negative is a live credential in a
    log nobody re-reads.
    """
    return set(re.findall(r'\b[A-Za-z0-9_\-]{32,}\b', text))


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

def test_a_reset_link_never_reaches_the_log(client, auth_app, caplog):
    """The specific value `_SECRETISH` cannot recognise.

    A password-reset token is `secrets.token_urlsafe(32)`. It matches none of
    the redactor's patterns, so if a call site hands it to the logger it is
    shipped to whatever aggregates the logs and sits there, valid, for the
    token's whole lifetime — and using it also locks the real owner out, which
    makes it strictly worse to leak than a session cookie.
    """
    _register(client)
    mailbox = auth_app.extensions['dough_email'].backend
    mailbox.clear()

    with caplog.at_level(logging.DEBUG):
        client.post('/forgot-password', data={'email': 'sal@example.com'})

    assert mailbox.sent, 'no reset mail was sent; the test proved nothing'
    link = re.search(r'(https?://\S+)', mailbox.sent[-1].body).group(1)
    token = link.rsplit('/', 1)[-1]

    assert token not in caplog.text
    assert link not in caplog.text


def test_a_verification_link_never_reaches_the_log(client, auth_app, caplog):
    mailbox = auth_app.extensions['dough_email'].backend
    mailbox.clear()

    with caplog.at_level(logging.DEBUG):
        _register(client)

    assert mailbox.sent
    link = re.search(r'(https?://\S+)', mailbox.sent[-1].body).group(1)
    assert link.rsplit('/', 1)[-1] not in caplog.text


def test_an_api_token_never_reaches_the_log(client, caplog):
    """Issued through the settings page, which is the surface added this phase."""
    _register(client)

    with caplog.at_level(logging.DEBUG):
        client.post('/settings/tokens', data={'name': 'Shortcuts',
                                              'scopes': ['read']})
        page = client.get('/settings')

    match = re.search(r'id="new-token"[^>]*value="(dgh_[^"]+)"',
                      page.get_data(as_text=True))
    assert match, 'no token was issued; the test proved nothing'
    assert match.group(1) not in caplog.text


def test_a_password_never_reaches_the_log(client, caplog):
    """Including on the paths that fail, which are the ones that log most."""
    with caplog.at_level(logging.DEBUG):
        _register(client)
        client.post('/login', data={'username': 'sal', 'password': PASSWORD})
        client.post('/login', data={'username': 'sal', 'password': 'wrong-one'})
        client.post('/settings/password',
                    data={'current_password': PASSWORD,
                          'password': 'newpassword123',
                          'confirm': 'newpassword123'})

    for secret in (PASSWORD, 'wrong-one', 'newpassword123'):
        assert secret not in caplog.text


def test_the_encryption_key_never_reaches_the_log(caplog):
    """A Fernet key is 44 url-safe base64 characters — `_SECRETISH` sees nothing.

    The failure mode this guards is an error message that quotes the value it
    found invalid, which is a natural thing to write and puts the key into the
    line reporting that the key was wrong.
    """
    from cryptography.fernet import Fernet

    from finance_sync.crypto import TokenCipher
    from finance_sync.exceptions import ConfigurationError

    key = Fernet.generate_key()
    blob = TokenCipher(key=key).encrypt({'access_token': 'x'})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConfigurationError) as excinfo:
            TokenCipher(key=Fernet.generate_key()).decrypt(blob)

    assert key.decode() not in str(excinfo.value)
    assert key.decode() not in caplog.text


def test_the_startup_validator_names_variables_and_never_values(monkeypatch):
    """It runs at boot and its exception goes to the log.

    A validator that quoted what it found invalid would put a real secret into
    the line reporting that a secret was wrong — the one place nobody thinks to
    look for one.
    """
    from config import ProductionConfig, get_config

    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', '', raising=False)
    monkeypatch.setattr(ProductionConfig, 'ENCRYPTION_KEY', 'a-real-looking-key',
                        raising=False)
    monkeypatch.setattr(ProductionConfig, 'PLAID_CLIENT_ID', 'client-abc123',
                        raising=False)
    monkeypatch.setattr(ProductionConfig, 'PLAID_SECRET', '', raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        get_config('production')

    message = str(excinfo.value)
    assert 'SECRET_KEY' in message                 # the name is named
    assert 'a-real-looking-key' not in message     # the values are not
    assert 'client-abc123' not in message


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------

def test_no_credential_reaches_the_audit_trail(client, auth_app):
    """The one table nothing ever deletes from.

    A secret written here is written permanently — there is no retention policy
    (OPS-0013) and the append-only hook means the application cannot remove it
    even deliberately. So the whole table is swept rather than a chosen row.
    """
    from models import AuditEvent

    _register(client)
    mailbox = auth_app.extensions['dough_email'].backend
    verification = re.search(r'(https?://\S+)', mailbox.sent[-1].body).group(1)

    mailbox.clear()
    client.post('/forgot-password', data={'email': 'sal@example.com'})
    reset = re.search(r'(https?://\S+)', mailbox.sent[-1].body).group(1)

    client.post('/settings/tokens', data={'name': 'Shortcuts', 'scopes': ['read']})
    api_token = re.search(r'id="new-token"[^>]*value="(dgh_[^"]+)"',
                          client.get('/settings').get_data(as_text=True)).group(1)

    rows = AuditEvent.query.all()
    assert rows, 'nothing was audited; the test proved nothing'
    dump = ' '.join((row.metadata_json or '') for row in rows)

    for secret in (verification.rsplit('/', 1)[-1], reset.rsplit('/', 1)[-1],
                   api_token, PASSWORD):
        assert secret not in dump


def test_the_reset_request_records_whether_the_account_existed(client):
    """The fact belongs in the audit log and not in the response.

    This is what makes somebody walking a list of addresses visible to an
    operator while telling the walker nothing — the same split
    `auth.login.failed` has used since Phase 8.
    """
    from models import AuditEvent, EVENT_PASSWORD_RESET_REQUESTED

    _register(client)
    client.post('/forgot-password', data={'email': 'sal@example.com'})
    client.post('/forgot-password', data={'email': 'nobody@example.com'})

    rows = (AuditEvent.query
            .filter(AuditEvent.event_type == EVENT_PASSWORD_RESET_REQUESTED)
            .order_by(AuditEvent.id).all())
    assert [row.to_dict()['metadata']['user_exists'] for row in rows] == [True, False]


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------

def test_no_secret_is_committed_in_the_example_environment_file():
    """`.env.example` is committed; `.env` is not.

    The failure is somebody pasting a working value in while documenting the
    variable. Every value must be a placeholder, and a placeholder is
    recognisable by being short and unshaped — the opposite of a real key.
    """
    path = os.path.join(REPO_ROOT, '.env.example')
    with open(path, encoding='utf-8') as handle:
        lines = [line.strip() for line in handle
                 if line.strip() and not line.strip().startswith('#')]

    for line in lines:
        if '=' not in line:
            continue
        name, _, value = line.partition('=')
        value = value.strip().strip('"').strip("'")
        assert not _tokens_in(value), (
            f'{name} in .env.example holds something credential-shaped')
        for marker in ('sk-ant-', 'sk-', 'AKIA', 'BEGIN PRIVATE KEY'):
            assert marker not in value, f'{name} holds a real-looking {marker} value'


def test_every_required_secret_is_documented_in_the_example_file():
    """The table, `.env.example` and the docs must not drift.

    A secret added to `REQUIRED_SECRETS` and not to `.env.example` is one an
    operator finds out about from a stack trace at deploy time.
    """
    from config import REQUIRED_SECRETS

    with open(os.path.join(REPO_ROOT, '.env.example'), encoding='utf-8') as handle:
        example = handle.read()

    missing = [s.name for s in REQUIRED_SECRETS if s.name not in example]
    assert missing == [], f'{missing} are required but absent from .env.example'


def test_the_real_env_file_is_ignored_by_git():
    """The one that holds live values must never be committable."""
    with open(os.path.join(REPO_ROOT, '.gitignore'), encoding='utf-8') as handle:
        ignored = {line.strip() for line in handle}

    assert '.env' in ignored
    # The generated key files are credentials too: `.sync_encryption_key`
    # decrypts every stored institution token.
    assert '.sync_encryption_key' in ignored
    assert '.flask_secret_key' in ignored
