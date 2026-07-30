"""Environment configuration selection and validation."""

import ast
import os

import pytest

import config as config_module
from config import (BaseConfig, DevelopmentConfig, ProductionConfig,
                    TestingConfig, _database_uri, get_config)

#: Config attributes that hold a credential. Asserted to be environment-read
#: with an empty default -- never compared by value, see
#: test_no_secret_is_hardcoded_in_the_config_module for why that matters.
SECRET_CONFIG_KEYS = {'ANTHROPIC_API_KEY', 'MAIL_PASSWORD', 'SECRET_KEY',
                      'MAIL_USERNAME', 'REDIS_URL'}


def test_get_config_selects_by_name(monkeypatch):
    assert get_config('development') is DevelopmentConfig
    assert get_config('testing') is TestingConfig
    # Production validates on selection, so it needs a key to be selectable at
    # all -- which is the point of test_production_requires_an_explicit_secret_key.
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', 'x' * 64, raising=False)
    assert get_config('production') is ProductionConfig


def test_get_config_reads_app_env(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'testing')
    assert get_config() is TestingConfig


def test_get_config_defaults_to_development(monkeypatch):
    monkeypatch.delenv('APP_ENV', raising=False)
    monkeypatch.delenv('FLASK_ENV', raising=False)
    assert get_config() is DevelopmentConfig


def test_unknown_environment_is_rejected_loudly(monkeypatch):
    """A typo'd APP_ENV must not silently fall back to development.

    Falling back is the dangerous behaviour: `APP_ENV=prod` would quietly run
    with DEBUG on and a generated secret key, and look fine.
    """
    monkeypatch.setenv('APP_ENV', 'prod')
    with pytest.raises(RuntimeError, match='Unknown APP_ENV'):
        get_config()


def test_production_requires_an_explicit_secret_key(monkeypatch):
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', '', raising=False)
    with pytest.raises(RuntimeError, match='SECRET_KEY must be set'):
        get_config('production')


def test_production_accepts_a_provided_secret_key(monkeypatch):
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', 'x' * 64, raising=False)
    assert get_config('production') is ProductionConfig


def test_production_never_generates_a_secret_key_file(monkeypatch, tmp_path):
    """Selecting production must have no filesystem side effects.

    A generated key would differ per process, so every restart -- and every
    worker in a multi-process deployment -- would reject the others' sessions
    and CSRF tokens. Failing to boot is the correct outcome; quietly writing a
    key file is not.
    """
    monkeypatch.setattr(ProductionConfig, 'BASE_DIR', str(tmp_path), raising=False)
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', '', raising=False)
    with pytest.raises(RuntimeError):
        get_config('production')
    assert not (tmp_path / '.flask_secret_key').exists()


def test_development_generates_and_reuses_a_secret_key(monkeypatch, tmp_path):
    monkeypatch.setattr(DevelopmentConfig, 'BASE_DIR', str(tmp_path), raising=False)
    monkeypatch.setattr(DevelopmentConfig, 'SECRET_KEY', '', raising=False)

    get_config('development')
    key_file = tmp_path / '.flask_secret_key'
    assert key_file.exists()
    first = key_file.read_text().strip()
    assert len(first) == 64

    # A second process must land on the same key, or restarting the app would
    # log everyone out.
    monkeypatch.setattr(DevelopmentConfig, 'SECRET_KEY', '', raising=False)
    get_config('development')
    assert key_file.read_text().strip() == first


# --- Database URL -----------------------------------------------------------

def test_database_uri_defaults_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    uri = _database_uri(str(tmp_path))
    assert uri.startswith('sqlite:///')
    assert uri.endswith('checkbook.db')


def test_postgres_scheme_is_normalised(monkeypatch, tmp_path):
    """SQLAlchemy 2 dropped the `postgres://` alias that hosting providers emit.

    Rewriting it here is what lets the SQLite -> Postgres move be a change to one
    environment variable instead of a code change.
    """
    monkeypatch.setenv('DATABASE_URL', 'postgres://u:p@host:5432/dough')
    assert _database_uri(str(tmp_path)) == 'postgresql+psycopg://u:p@host:5432/dough'


def test_explicit_driver_url_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://u:p@host/dough')
    assert _database_uri(str(tmp_path)) == 'postgresql+psycopg://u:p@host/dough'


def test_sqlite_gets_no_connection_pool_options():
    """pool_pre_ping against a local file is a wasted round trip per checkout."""
    assert TestingConfig.SQLALCHEMY_ENGINE_OPTIONS == {}


# --- Cookie hardening (see docs/security.md SEC-0001) -----------------------

def test_session_cookie_defaults_are_explicit():
    assert BaseConfig.SESSION_COOKIE_SAMESITE == 'Lax'
    assert BaseConfig.SESSION_COOKIE_HTTPONLY is True


def test_production_forces_secure_cookies():
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.PREFERRED_URL_SCHEME == 'https'


def test_debug_is_off_unless_explicitly_requested(monkeypatch):
    """The interactive debugger is RCE behind a PIN; it must be opt-in.

    app.py's __main__ block binds to APP_HOST, which is routinely a LAN address
    so a phone can reach the app. Before the config split app.config['DEBUG']
    was always False, so a default of True here would have quietly switched the
    debugger on for anyone without APP_DEBUG set.
    """
    monkeypatch.delenv('APP_DEBUG', raising=False)
    import importlib
    reloaded = importlib.reload(config_module)
    try:
        assert reloaded.DevelopmentConfig.DEBUG is False
        assert reloaded.ProductionConfig.DEBUG is False
    finally:
        importlib.reload(config_module)


def test_production_does_not_auto_migrate():
    """Two workers racing `flask db upgrade` at boot is not theoretical."""
    assert ProductionConfig.AUTO_UPGRADE_DB is False
    assert DevelopmentConfig.AUTO_UPGRADE_DB is True


# --- create_app wiring ------------------------------------------------------

def test_testing_flag_selects_testing_config(monkeypatch):
    """A stray APP_ENV in the developer's shell must not reach the test suite.

    Before the config split the suite inherited whatever APP_ENV was set, so
    running tests in a production shell would flip AUTH_ENABLED on and fail
    ~180 tests for reasons unrelated to any code change.
    """
    from app import create_app

    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', 'x' * 64, raising=False)

    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'SYNC_AUTO_ENABLED': False,
    })
    assert application.config['AUTH_ENABLED'] is False
    assert application.config['SYNC_SYNCHRONOUS'] is True


def test_test_config_overrides_the_selected_class(tmp_path):
    """test_config is applied last, so a test can override any setting.

    This is what test_auth.py relies on to turn auth back on, and what the
    session-cookie settings used to defeat by being assigned after the merge.
    """
    from app import create_app

    application = create_app(test_config={
        'TESTING': True,
        'AUTH_ENABLED': True,
        'SESSION_COOKIE_SAMESITE': 'Strict',
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'test.db'}",
        'SYNC_AUTO_ENABLED': False,
    })
    assert application.config['AUTH_ENABLED'] is True
    assert application.config['SESSION_COOKIE_SAMESITE'] == 'Strict'


def test_no_secret_is_hardcoded_in_the_config_module():
    """Every credential must come from the environment, with an empty default.

    Entirely source-level, for two reasons.

    It used to compare `BaseConfig.ANTHROPIC_API_KEY` against
    `os.environ.get('ANTHROPIC_API_KEY')`. That was wrong in both directions:

    1. **It printed the secret on failure.** pytest's assertion rewriting dumps
       both sides of a failed `==`, so a test whose entire job is protecting
       credentials would echo a live API key into the terminal, CI log, and any
       pasted output. A test that leaks the thing it guards is worse than no
       test.
    2. **It compared a frozen value against a live read.** The class attribute is
       evaluated once at import; `os.environ` can change afterwards -- and does,
       because conftest's isolation fixture deletes these vars so the suite
       cannot make live API calls. The comparison therefore failed for a reason
       that had nothing to do with a hardcoded secret.

    Checking the *source* is what the docstring always claimed and is immune to
    both. It also catches a pasted default that is never read, which the value
    comparison could not.
    """
    with open(config_module.__file__, encoding='utf-8') as fh:
        source = fh.read()
    for marker in ('sk-ant-', 'sk-', 'AKIA', 'BEGIN PRIVATE KEY'):
        assert marker not in source, f'possible hardcoded secret: {marker}'

    tree = ast.parse(source)
    checked = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in names:
            if name not in SECRET_CONFIG_KEYS:
                continue
            checked.add(name)
            call = node.value
            assert isinstance(call, ast.Call), (
                f'{name} must be read from the environment, not assigned a literal')
            func = call.func
            reader = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, 'id', '')
            assert reader in ('get', '_bool', '_int'), (
                f'{name} must come from os.environ.get or a config helper, '
                f'not {reader!r}')
            # A default argument, if present, must be an empty string. Anything
            # else is a baked-in credential even when it looks harmless.
            defaults = [a for a in call.args[1:]] + [
                kw.value for kw in call.keywords if kw.arg == 'default']
            for default in defaults:
                assert isinstance(default, ast.Constant) and default.value == '', (
                    f'{name} has a non-empty default baked into config.py')

    missing = SECRET_CONFIG_KEYS - checked
    assert not missing, (
        f'{sorted(missing)} are named as secrets but not assigned in config.py; '
        'either the name changed or the assignment moved somewhere unchecked')
