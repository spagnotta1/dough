"""Application configuration, selected per environment.

One class per environment, all inheriting from `BaseConfig`, chosen by
`get_config()` from `APP_ENV`. Flask reads these with `config.from_object`,
which copies uppercase class attributes and never instantiates the class --
which is why validation lives in a `validate()` classmethod rather than
`__init__`, where it would silently never run.

The split exists mainly so production can be strict about things development
must be lenient about: a generated session key is a convenience locally and a
liveness bug in production, where it would be regenerated on every restart and
invalidate every session and CSRF token in flight.
"""

import os
import secrets


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _load_or_create_secret_key(base_dir):
    """Persist a random Flask session-signing key next to the database.

    A hardcoded key would let anyone forge session cookies; a purely in-memory
    one would log everyone out on each restart. Development and testing only --
    production requires SECRET_KEY to be set explicitly, see
    ProductionConfig.validate().
    """
    path = os.path.join(base_dir, '.flask_secret_key')
    try:
        with open(path) as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    with open(path, 'w') as fh:
        fh.write(key)
    return key


def _database_uri(base_dir):
    """Resolve the database URL, normalising the Postgres scheme.

    `postgres://` is what Heroku-style providers hand out and what SQLAlchemy 2
    refuses to load, having dropped the alias. Rewriting it here means moving off
    SQLite is a change to one environment variable rather than a code change --
    which is the whole point of routing every query through the ORM.
    """
    uri = os.environ.get('DATABASE_URL', '').strip()
    if not uri:
        return f'sqlite:///{os.path.join(base_dir, "checkbook.db")}'
    if uri.startswith('postgres://'):
        uri = uri.replace('postgres://', 'postgresql+psycopg://', 1)
    return uri


class BaseConfig:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

    # --- Core ---------------------------------------------------------------
    SECRET_KEY = os.environ.get('SECRET_KEY', '')
    # Whether a missing SECRET_KEY may be generated and persisted to disk.
    # Production sets this False; see get_config().
    ALLOW_GENERATED_SECRET_KEY = True

    # --- Database -----------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = _database_uri(BASE_DIR)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # pool_pre_ping costs a round trip per checkout and is pointless against a
    # local file, but it is what keeps a networked database from handing out a
    # connection the server closed hours ago.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {} if SQLALCHEMY_DATABASE_URI.startswith('sqlite')
        else {'pool_pre_ping': True, 'pool_recycle': 1800})
    # Run `flask db upgrade` at startup. Convenient in development; in
    # production migrations are a deliberate deploy step, not a side effect of
    # a process starting -- two workers racing the same migration is not a
    # theoretical problem. Env-overridable so `AUTO_UPGRADE_DB=0 flask db upgrade`
    # runs the chain once from the CLI rather than twice -- once as a side effect
    # of the app booting to serve the command, once for the command itself.
    # [consumed from Phase 2]
    AUTO_UPGRADE_DB = _bool('AUTO_UPGRADE_DB', True)

    # --- Observability  [Phase 8] -------------------------------------------
    # JSON one-object-per-line, unless DEBUG, where a human is reading the
    # terminal. `LOG_JSON` overrides in both directions so the production format
    # can be reproduced locally when the question is about the logs themselves.
    # Left as None rather than False so "unset" is distinguishable from "off".
    LOG_JSON = _bool('LOG_JSON', None) if os.environ.get('LOG_JSON') else None
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    # How many audit events the household activity view shows. Not a retention
    # policy -- nothing is deleted; see OPS-0013 in docs/security.md.
    AUDIT_PAGE_SIZE = _int('AUDIT_PAGE_SIZE', 100)

    # --- Uploads ------------------------------------------------------------
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size

    # --- AI -----------------------------------------------------------------
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    AI_INSIGHT_CACHE_TTL = _int('AI_INSIGHT_CACHE_TTL', 3600)

    # --- Financial institution synchronization (finance_sync) ---------------
    SYNC_AUTO_ENABLED = _bool('SYNC_AUTO_ENABLED', True)
    SYNC_INTERVAL_HOURS = float(os.environ.get('SYNC_INTERVAL_HOURS', 12))
    # When True, manual sync API calls run inline instead of on a background
    # thread (used by the test suite for determinism).
    SYNC_SYNCHRONOUS = False

    # --- Session cookie -----------------------------------------------------
    # Assigned, never setdefault'd: Flask ships these keys already present, so
    # setdefault is a silent no-op. See docs/security.md SEC-0001.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _bool('APP_HTTPS', False)

    # --- Authentication -----------------------------------------------------
    AUTH_ENABLED = True
    # Whether strangers may create an account. Off by default: this app fronts
    # real bank data and is routinely exposed on a LAN.  [consumed from Phase 6]
    ALLOW_REGISTRATION = _bool('ALLOW_REGISTRATION', False)
    REQUIRE_EMAIL_VERIFICATION = _bool('REQUIRE_EMAIL_VERIFICATION', False)
    PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')

    # Session lifetimes, seconds.  [Phase 6]
    SESSION_IDLE_SECONDS = _int('SESSION_IDLE_SECONDS', 12 * 3600)
    SESSION_ABSOLUTE_SECONDS = _int('SESSION_ABSOLUTE_SECONDS', 7 * 86400)
    SESSION_REMEMBER_SECONDS = _int('SESSION_REMEMBER_SECONDS', 30 * 86400)

    # --- CSRF ---------------------------------------------------------------
    # On everywhere except where a config explicitly says otherwise. There is no
    # environment variable: an operator who can turn CSRF off from the
    # environment is one `CSRF_ENABLED=0` away from a silently unprotected
    # deployment, and nothing about a self-hosted install needs it disabled.
    # TestingConfig sets it False so the ~180 tests that predate this phase can
    # keep posting bare forms; tests/test_auth.py turns it back on.  [Phase 6]
    CSRF_ENABLED = True

    # How many reverse-proxy hops in front of this app are ours. 0 means read
    # the peer address directly and ignore X-Forwarded-For entirely -- that
    # header is attacker-controlled, and believing it makes the login throttle
    # a no-op, since a fresh forged address per attempt is a fresh bucket per
    # attempt. Set this only to the number of proxies you actually run.
    TRUSTED_PROXIES = _int('TRUSTED_PROXIES', 0)

    # How long an invitation link stays usable, hours.  [Phase 6]
    INVITE_TTL_HOURS = _int('INVITE_TTL_HOURS', 72)

    # --- Multi-tenancy ------------------------------------------------------
    # The household every pre-existing row is backfilled to.  [Phase 5]
    DEFAULT_HOUSEHOLD_ID = 1

    # --- Email --------------------------------------------------------------
    # console prints verification and reset links to the terminal, which is the
    # right default for a self-hosted instance with no mail server.  [Phase 6]
    MAIL_BACKEND = os.environ.get('MAIL_BACKEND', 'console')
    MAIL_FROM = os.environ.get('MAIL_FROM', 'dough@localhost')
    MAIL_SERVER = os.environ.get('MAIL_SERVER', '')
    MAIL_PORT = _int('MAIL_PORT', 587)
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_USE_TLS = _bool('MAIL_USE_TLS', True)

    # --- Rate limiting ------------------------------------------------------
    RATELIMIT_BACKEND = os.environ.get('RATELIMIT_BACKEND', 'memory')
    REDIS_URL = os.environ.get('REDIS_URL', '')

    # --- Marketing ----------------------------------------------------------
    # Renders nothing when empty -- placeholder testimonials on a finance
    # landing page are worse than no testimonials.  [consumed from Phase 8]
    MARKETING_TESTIMONIALS = []

    @classmethod
    def validate(cls):
        """Raise if the configuration cannot safely run. Called by get_config."""


class DevelopmentConfig(BaseConfig):
    # Defaults off, and deliberately so. Werkzeug's interactive debugger is a
    # remote code execution primitive guarded only by a console PIN, and app.py's
    # __main__ block binds to APP_HOST, which is routinely a LAN address so the
    # phone can reach it. Before the config split app.config['DEBUG'] was always
    # False -- only the dev-server call read APP_DEBUG -- so defaulting to True
    # here would have silently switched the debugger on for anyone whose
    # environment did not happen to set the variable.
    DEBUG = _bool('APP_DEBUG', False)


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    # In memory, and not inherited. BaseConfig resolves to the real
    # checkbook.db when DATABASE_URL is unset, so a script that did nothing
    # worse than `APP_ENV=testing python -c 'create_app()'` would open the
    # live database -- and `db.create_all()` would write to it. The test suite
    # never noticed because conftest.py overrides this key with a tmp_path
    # file; nothing outside the suite got that protection. Set DATABASE_URL
    # explicitly to test against something else.
    SQLALCHEMY_DATABASE_URI = (os.environ.get('DATABASE_URL', '').strip()
                               or 'sqlite://')
    # Auth is off by default under test so the ~180 tests that predate it do not
    # each need a login. tests/test_auth.py and tests/test_route_guard.py turn it
    # back on explicitly, which is the only way those flows get exercised.
    AUTH_ENABLED = False
    # See CSRF_ENABLED above. Off here, on in tests/test_auth.py and
    # tests/test_route_guard.py, which are the suites that exercise the flow.
    CSRF_ENABLED = False
    AUTO_UPGRADE_DB = False
    SYNC_AUTO_ENABLED = False
    SYNC_SYNCHRONOUS = True
    MAIL_BACKEND = 'memory'
    SESSION_COOKIE_SECURE = False
    # Human-readable, because a failing test's captured output is read by a
    # person. Tests that assert on the JSON shape set LOG_JSON=True themselves.
    LOG_JSON = False
    SQLALCHEMY_ENGINE_OPTIONS = {}


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = 'https'
    AUTO_UPGRADE_DB = False
    ALLOW_GENERATED_SECRET_KEY = False

    @classmethod
    def validate(cls):
        if not cls.SECRET_KEY:
            raise RuntimeError(
                'SECRET_KEY must be set when APP_ENV=production. Generating one '
                'would produce a different key on every restart, invalidating '
                'every session and CSRF token. Generate a durable one with:\n'
                '    python -c "import secrets; print(secrets.token_hex(32))"')


_CONFIGS = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
}


def get_config(name=None):
    """Return the config class for `name`, or APP_ENV, defaulting to development.

    Resolving SECRET_KEY here rather than in a class body means importing this
    module has no side effects: selecting production must not write a
    `.flask_secret_key` file to disk as a byproduct of an import.
    """
    name = (name or os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV')
            or 'development').strip().lower()
    cls = _CONFIGS.get(name)
    if cls is None:
        raise RuntimeError(
            f'Unknown APP_ENV {name!r}; expected one of {sorted(_CONFIGS)}')

    if not cls.SECRET_KEY and cls.ALLOW_GENERATED_SECRET_KEY:
        cls.SECRET_KEY = _load_or_create_secret_key(cls.BASE_DIR)

    cls.validate()
    return cls


# Backwards compatibility: `from config import Config` still resolves, so any
# script or notebook outside the app package keeps working.
Config = DevelopmentConfig
