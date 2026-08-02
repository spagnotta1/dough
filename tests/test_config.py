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
                      'MAIL_USERNAME', 'REDIS_URL',
                      # Phase 10.5. ENCRYPTION_KEY is the highest-value one in
                      # this set: it decrypts the stored Plaid and Coinbase
                      # access tokens, so a literal here would be a repository
                      # that reads every connected household's bank data.
                      'ENCRYPTION_KEY', 'PLAID_CLIENT_ID', 'PLAID_SECRET'}


def _production_ready(monkeypatch):
    """Give ProductionConfig every secret it requires.

    A helper since Phase 10.5, when the required set grew past one. Without it,
    each test that merely needs production to be *selectable* would carry a
    growing list of unrelated monkeypatches, and adding the next required secret
    would fail every one of them for a reason none of them is about.
    """
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', 'x' * 64, raising=False)
    monkeypatch.setattr(ProductionConfig, 'ENCRYPTION_KEY', 'y' * 44,
                        raising=False)
    # Both Plaid values cleared explicitly, which is a *valid* configuration --
    # no credentials means the sync adapters run against the sandbox.
    #
    # Set here rather than left alone because these are class attributes
    # evaluated at import, so conftest's `_isolate_live_credentials` fixture
    # cannot reach them: it deletes the environment variables, and the values
    # were already read. A developer whose real `.env` holds a Plaid client id
    # would otherwise have these tests pass or fail depending on what is in it,
    # which is the exact failure mode the docstring of
    # `test_no_secret_is_hardcoded_in_the_config_module` describes.
    monkeypatch.setattr(ProductionConfig, 'PLAID_CLIENT_ID', '', raising=False)
    monkeypatch.setattr(ProductionConfig, 'PLAID_SECRET', '', raising=False)


def test_get_config_selects_by_name(monkeypatch):
    assert get_config('development') is DevelopmentConfig
    assert get_config('testing') is TestingConfig
    # Production validates on selection, so it needs its secrets to be
    # selectable at all -- which is the point of the two tests below.
    _production_ready(monkeypatch)
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
    _production_ready(monkeypatch)
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', '', raising=False)
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        get_config('production')


def test_production_requires_an_explicit_encryption_key(monkeypatch, tmp_path):
    """Phase 10.5. The key that decrypts the stored institution tokens.

    Production must refuse rather than generate one, and the reason is worse
    than the secret-key case. A generated session key signs everyone out; a
    generated *encryption* key makes every already-stored Plaid and Coinbase
    token unreadable, and the failure does not arrive at boot — it arrives at
    the next sync, reported as an encryption error rather than as the missing
    variable that caused it.

    `BASE_DIR` is redirected at a tmp_path so that a regression which *did*
    generate a key would be caught by the file assertion rather than quietly
    writing into the repository.
    """
    _production_ready(monkeypatch)
    monkeypatch.setattr(ProductionConfig, 'BASE_DIR', str(tmp_path), raising=False)
    monkeypatch.setattr(ProductionConfig, 'ENCRYPTION_KEY', '', raising=False)
    with pytest.raises(RuntimeError, match='ENCRYPTION_KEY'):
        get_config('production')
    assert not (tmp_path / '.sync_encryption_key').exists()


def test_production_reports_every_missing_secret_at_once(monkeypatch):
    """One deploy, one list — not one variable per restart.

    The failure this prevents is an operator setting SECRET_KEY, redeploying,
    being told about ENCRYPTION_KEY, setting that, redeploying again. Reporting
    them together is the whole reason `REQUIRED_SECRETS` is a table rather than
    a chain of `if not X: raise`.
    """
    monkeypatch.setattr(ProductionConfig, 'SECRET_KEY', '', raising=False)
    monkeypatch.setattr(ProductionConfig, 'ENCRYPTION_KEY', '', raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        get_config('production')
    message = str(excinfo.value)
    assert 'SECRET_KEY' in message and 'ENCRYPTION_KEY' in message
    # Every entry says how to produce a value. An error naming a variable an
    # operator has never heard of, without saying what goes in it, sends them to
    # this repository at deploy time.
    assert 'secrets.token_hex' in message
    assert 'Fernet.generate_key' in message


def test_production_refuses_half_a_plaid_credential(monkeypatch):
    """Both or neither. One half is a live-API deployment that fails.

    No credentials at all is a working state — the sync adapters run against the
    deterministic sandbox. Exactly one is not: the adapter switches to the real
    API and authenticates with an incomplete credential, in a deployment whose
    operator believes Plaid is configured.
    """
    _production_ready(monkeypatch)
    monkeypatch.setattr(ProductionConfig, 'PLAID_CLIENT_ID', 'abc', raising=False)
    monkeypatch.setattr(ProductionConfig, 'PLAID_SECRET', '', raising=False)
    with pytest.raises(RuntimeError, match='PLAID'):
        get_config('production')


def test_production_accepts_a_fully_configured_deployment(monkeypatch):
    _production_ready(monkeypatch)
    assert get_config('production') is ProductionConfig


def test_production_warns_about_configuration_it_will_still_run(monkeypatch):
    """Warnings must not be errors, and must not be silent either.

    Each of these has a real use — a demo instance genuinely may want console
    mail — so refusing to boot over one would teach operators to set an override
    variable, which then hides the checks that were not judgement calls.
    """
    _production_ready(monkeypatch)
    monkeypatch.setattr(ProductionConfig, 'MAIL_BACKEND', 'console', raising=False)
    monkeypatch.setattr(ProductionConfig, 'PUBLIC_BASE_URL', '', raising=False)

    # Still selectable: these are warnings, not failures.
    assert get_config('production') is ProductionConfig

    notes = ' '.join(ProductionConfig.warnings())
    assert 'MAIL_BACKEND=console' in notes
    assert 'PUBLIC_BASE_URL' in notes


def test_production_warns_when_the_legal_pages_are_unconfigured(monkeypatch):
    """The only warning here whose symptom is visible to the public.

    Unset renders a literal `[OPERATING ENTITY - set LEGAL_ENTITY]` marker on a
    live `/privacy`, which is worse than a log line nobody reads — so it is
    worth pinning that the log line at least exists.

    A warning and not a `validate` failure: an internal or demo deployment with
    no outside users is a real state, and refusing to boot over it is how
    operators learn to set override variables.
    """
    _production_ready(monkeypatch)
    for name in ('LEGAL_ENTITY', 'LEGAL_CONTACT_EMAIL', 'LEGAL_JURISDICTION'):
        monkeypatch.setattr(ProductionConfig, name, '', raising=False)

    assert get_config('production') is ProductionConfig

    notes = ' '.join(ProductionConfig.warnings())
    assert 'LEGAL_ENTITY' in notes
    assert 'placeholder' in notes


def test_production_warns_when_nothing_is_watching_for_errors(monkeypatch):
    """An unset DSN means a 500 is reported nowhere at all."""
    _production_ready(monkeypatch)
    monkeypatch.setattr(ProductionConfig, 'SENTRY_DSN', '', raising=False)

    notes = ' '.join(ProductionConfig.warnings())
    assert 'SENTRY_DSN' in notes


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


# --- The sender address -----------------------------------------------------

def _mail_from(monkeypatch, **environ):
    """Read MAIL_FROM as the class body computes it.

    `config.from_object` copies class attributes, so these are evaluated once at
    import and `monkeypatch.setenv` alone would assert nothing. Same reload
    dance as the APP_DEBUG test below.
    """
    import importlib

    for name in ('MAIL_FROM', 'MAIL_DEFAULT_SENDER'):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    try:
        return importlib.reload(config_module).BaseConfig.MAIL_FROM
    finally:
        importlib.reload(config_module)


def test_the_sender_address_comes_from_MAIL_FROM(monkeypatch):
    assert _mail_from(monkeypatch,
                      MAIL_FROM='no-reply@dough.example') == 'no-reply@dough.example'


def test_MAIL_DEFAULT_SENDER_is_accepted_as_the_same_thing(monkeypatch):
    """The name `flask_mail` uses, and so the name every provider's setup page
    prints — Postmark's included.

    This application is not built on `flask_mail`, so without the alias an
    operator who follows those instructions sets a variable nothing reads. The
    failure is not a missing From header, which would be obvious: it is the
    `dough@localhost` default, which a hosted relay rejects per message for not
    being a verified sender. Every send fails at the last step with the
    transport working perfectly, which is an expensive thing to diagnose.
    """
    assert _mail_from(monkeypatch,
                      MAIL_DEFAULT_SENDER='no-reply@dough.example') == 'no-reply@dough.example'


def test_MAIL_FROM_wins_when_both_are_set(monkeypatch):
    """Two names for one setting need a stated winner, or the answer is
    whichever line `os.environ` happened to be asked for first."""
    assert _mail_from(monkeypatch, MAIL_FROM='wins@dough.example',
                      MAIL_DEFAULT_SENDER='loses@dough.example') == 'wins@dough.example'


def test_an_empty_MAIL_FROM_falls_through_to_the_alias(monkeypatch):
    """`.env.example` ships both names with one of them blank, and a blank
    From address is not a choice anybody made."""
    assert _mail_from(monkeypatch, MAIL_FROM='',
                      MAIL_DEFAULT_SENDER='alias@dough.example') == 'alias@dough.example'


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
            # Every Call anywhere in the assigned expression, rather than
            # requiring the value to *be* one.  [Phase 10.5] Two secrets are now
            # read under either of two names --
            #
            #     SECRET_KEY = os.environ.get('SECRET_KEY') or os.environ.get('SESSION_SECRET', '')
            #
            # -- which is a BoolOp whose operands are the reads. The property
            # being checked has not moved: every read must come from the
            # environment and none may carry a non-empty default. Requiring a
            # bare Call would have forced the alias into a helper function
            # purely to satisfy the shape of a test.
            #
            # The assertion that there is at least one Call is what keeps this
            # from passing vacuously on `SECRET_KEY = 'hunter2'`, which is the
            # literal this test exists to reject.
            calls = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)]
            assert calls, (
                f'{name} must be read from the environment, not assigned a literal')
            for call in calls:
                func = call.func
                reader = func.attr if isinstance(func, ast.Attribute) else getattr(
                    func, 'id', '')
                # `_plaid_secret` joins the helper list in Phase 10.5. It reads
                # PLAID_SECRET_<PLAID_ENV> with a fallback to PLAID_SECRET,
                # matching how the adapter resolves it -- see the function's
                # docstring for why config has to agree with the adapter here.
                assert reader in ('get', '_bool', '_int', '_plaid_secret'), (
                    f'{name} must come from os.environ.get or a config helper, '
                    f'not {reader!r}')
                # A default argument, if present, must be an empty string.
                # Anything else is a baked-in credential even when it looks
                # harmless.
                defaults = list(call.args[1:]) + [
                    kw.value for kw in call.keywords if kw.arg == 'default']
                for default in defaults:
                    assert isinstance(default, ast.Constant) and default.value == '', (
                        f'{name} has a non-empty default baked into config.py')

    missing = SECRET_CONFIG_KEYS - checked
    assert not missing, (
        f'{sorted(missing)} are named as secrets but not assigned in config.py; '
        'either the name changed or the assignment moved somewhere unchecked')
