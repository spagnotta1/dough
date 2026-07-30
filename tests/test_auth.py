"""Login/setup flow. Auth is disabled by default under TESTING, so these
tests build their own app with AUTH_ENABLED forced on."""

import pytest

import finance_sync.scheduler as scheduler_module
from app import create_app
from models import db, AppUser


@pytest.fixture()
def auth_app(tmp_path):
    scheduler_module._scheduler = None
    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_SYNCHRONOUS': True,
        'SYNC_AUTO_ENABLED': False,
    })
    with application.app_context():
        yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def auth_client(auth_app):
    return auth_app.test_client()


def _create_account(client, username='sal', password='hunter2boat'):
    return client.post('/setup', data={
        'username': username, 'password': password, 'confirm': password})


def test_first_run_redirects_to_setup(auth_client):
    resp = auth_client.get('/', follow_redirects=True)
    assert b"I'm Dough" in resp.data   # the first-run introduction


def test_setup_validates_input(auth_client):
    resp = auth_client.post('/setup', data={'username': 'sal', 'password': 'short', 'confirm': 'short'})
    assert b'at least 8 characters' in resp.data
    resp = auth_client.post('/setup', data={'username': 'sal', 'password': 'longenough', 'confirm': 'different'})
    assert b'do not match' in resp.data
    assert AppUser.query.count() == 0


def test_setup_creates_account_and_signs_in(auth_client):
    resp = _create_account(auth_client)
    assert resp.status_code == 302
    user = AppUser.query.one()
    assert user.username == 'sal'
    assert user.password_hash != 'hunter2boat'  # stored hashed, not plaintext
    assert auth_client.get('/').status_code == 200


def test_setup_refused_once_account_exists(auth_client):
    _create_account(auth_client)
    resp = auth_client.post('/setup', data={
        'username': 'intruder', 'password': 'password123', 'confirm': 'password123'})
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    assert AppUser.query.count() == 1


def test_pages_and_api_require_login(auth_client):
    _create_account(auth_client)
    auth_client.post('/logout')
    resp = auth_client.get('/', follow_redirects=False)
    assert resp.status_code == 302 and '/login' in resp.headers['Location']
    assert auth_client.get('/api/sync/status').status_code == 401


def test_login_wrong_then_right(auth_client):
    _create_account(auth_client)
    auth_client.post('/logout')
    resp = auth_client.post('/login', data={'username': 'sal', 'password': 'wrong'})
    assert b'Invalid username or password' in resp.data
    resp = auth_client.post('/login', data={'username': 'sal', 'password': 'hunter2boat'})
    assert resp.status_code == 302
    assert auth_client.get('/').status_code == 200


def test_login_throttled_after_failures(auth_client):
    _create_account(auth_client)
    auth_client.post('/logout')
    for _ in range(5):
        auth_client.post('/login', data={'username': 'sal', 'password': 'wrong'})
    resp = auth_client.post('/login', data={'username': 'sal', 'password': 'hunter2boat'})
    assert b'Too many failed attempts' in resp.data


def test_open_redirect_neutralized(auth_client):
    _create_account(auth_client)
    auth_client.post('/logout')
    resp = auth_client.post('/login?next=//evil.com',
                            data={'username': 'sal', 'password': 'hunter2boat'})
    assert resp.headers['Location'].endswith('/')
