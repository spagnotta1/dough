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


def _plaid_secret():
    """The Plaid secret for the active `PLAID_ENV`, resolved as the adapter does.

    `finance_sync/adapters/plaid_adapter.py::_env_setting` prefers a per-
    environment name — `PLAID_SECRET_SANDBOX`, `PLAID_SECRET_PRODUCTION` — over
    the bare `PLAID_SECRET`, so that both can sit in one `.env` and `PLAID_ENV`
    alone picks between them. That is the shape a real installation has.

    This function exists because the validation in `ProductionConfig` has to
    agree with it. Reading only the bare name would refuse to start a deployment
    that is correctly configured under the per-environment names — reporting
    "half a Plaid credential" at a deployment holding a complete one, which is a
    startup check actively making things worse.

    The duplication is deliberate and is the smaller of two evils. The
    alternative is importing `finance_sync` into `config`, which inverts the
    dependency (the adapters read configuration, not the reverse) and drags a
    `cryptography` import into a module that must stay importable with no side
    effects.
    """
    env = (os.environ.get('PLAID_ENV', 'sandbox') or 'sandbox').strip().upper()
    return (os.environ.get(f'PLAID_SECRET_{env}')
            or os.environ.get('PLAID_SECRET', ''))


def _load_or_create_encryption_key(base_dir):
    """Persist a Fernet key for the stored institution tokens.

    The same shape as `_load_or_create_secret_key` and for the same reason, with
    one difference that matters more: regenerating *this* key does not merely
    sign everyone out, it makes the already-encrypted `auth_blob` column
    unreadable. The tokens are not recoverable from anywhere — every connection
    has to be linked again — so a key that is regenerated because a file was
    missing is data loss rather than an inconvenience.

    That is why the file is written once and read forever after, and why
    production refuses to generate one at all (`ALLOW_GENERATED_ENCRYPTION_KEY`).

    Kept compatible with `finance_sync/crypto.py`, which has written and read
    `.sync_encryption_key` since the sync package existed. Same filename, same
    format, so an existing installation's key is found by both and nothing has to
    be migrated.
    """
    from cryptography.fernet import Fernet

    path = os.path.join(base_dir, '.sync_encryption_key')
    try:
        with open(path, encoding='utf-8') as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = Fernet.generate_key().decode()
    with open(path, 'w', encoding='utf-8') as fh:
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


class Secret:
    """One secret this application may need, and what happens without it.

    Declared as data rather than as a chain of `if not X: raise` for a reason
    that is about the operator rather than about this file: a deployment missing
    three variables should be told all three at once. An `if` chain reports the
    first, they set it, restart, and are told the second — which turns one
    five-minute fix into three deploys.

    `required` means production will not start without it. `feature` means the
    application runs and one capability is off, which is reported at INFO and is
    not an error: an installation with no Plaid credentials is a perfectly valid
    installation that syncs nothing, and refusing to boot over it would make the
    CSV-only use case impossible.
    """

    __slots__ = ('name', 'required', 'feature', 'why', 'how')

    def __init__(self, name, *, required=False, feature=None, why='', how=''):
        self.name = name
        self.required = required
        self.feature = feature
        self.why = why
        self.how = how


#: Every secret named in the deployment documentation, in one table.
#:
#: This is the list `docs/security.md` and `.env.example` are written against,
#: and `tests/test_config.py` asserts the three agree — a secret added to the
#: code and not to `.env.example` is one an operator finds out about from a
#: stack trace.
#:
#: What is deliberately *not* here: `MAIL_PASSWORD`. It is a secret and it is
#: redacted like one, but it is reached through `MAIL_BACKEND=smtp`, so its
#: absence is already reported by `build_backend` at the point of use with more
#: context than a startup check could give.
REQUIRED_SECRETS = (
    Secret('DATABASE_URL',
           why='Where the data lives. Defaults to a local SQLite file, which is '
               'right for a single-machine install and wrong for anything with '
               'more than one process or an ephemeral filesystem.',
           how='postgresql+psycopg://user:pass@host/dbname'),
    Secret('SECRET_KEY', required=True,
           why='Signs the session cookie and the CSRF token. Generated and '
               'persisted to disk outside production; in production a generated '
               'one would differ per restart and per process, invalidating every '
               'session and CSRF token in flight.',
           how='python -c "import secrets; print(secrets.token_hex(32))"'),
    Secret('ENCRYPTION_KEY', required=True,
           why='Fernet key for the Plaid and Coinbase access tokens stored in '
               '`connected_accounts.auth_blob`. Losing it means every '
               'connection has to be re-linked; leaking it means the stored '
               'tokens read the accounts directly.',
           how='python -c "from cryptography.fernet import Fernet; '
               'print(Fernet.generate_key().decode())"'),
    Secret('ANTHROPIC_API_KEY', feature='Dough, the AI assistant',
           why='Every generated line in the product. Without it the AI surfaces '
               'report themselves unconfigured and the rest of the application '
               'works normally.',
           how='https://console.anthropic.com/settings/keys'),
    Secret('PLAID_CLIENT_ID', feature='bank and brokerage synchronization',
           why='Account aggregation. Without it (and PLAID_SECRET) the sync '
               'adapters run against the deterministic sandbox, which is what '
               'makes the connect flow work locally.',
           how='https://dashboard.plaid.com'),
    Secret('PLAID_SECRET', feature='bank and brokerage synchronization',
           why='The other half of the Plaid credential. Setting one without the '
               'other is refused rather than half-configured.',
           how='https://dashboard.plaid.com'),
)


class BaseConfig:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

    # --- Core ---------------------------------------------------------------
    # `SESSION_SECRET` is accepted as an alias.  [Phase 10.5] It is the name the
    # deployment documentation for this phase uses and the one several hosting
    # platforms inject automatically; `SECRET_KEY` is what every existing `.env`
    # in the wild says, and what Flask itself calls the key. Reading both means
    # neither audience has to rename anything, and `SECRET_KEY` wins when both
    # are set so an existing deployment's behaviour cannot change under it.
    SECRET_KEY = (os.environ.get('SECRET_KEY')
                  or os.environ.get('SESSION_SECRET', ''))
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
    # Env-overridable because a container's own filesystem is not storage. On
    # Railway the image is rebuilt and replaced on every deploy, so the default
    # -- a directory inside the application -- silently discards every uploaded
    # statement at the next push, with no error to notice. Point it at the
    # mounted volume (UPLOAD_FOLDER=/data/uploads) and the files outlive the
    # deploy. `create_app` makes the directory, so the volume needs no
    # preparation. The default is unchanged for local installs, where BASE_DIR
    # is a real directory on a real disk.
    UPLOAD_FOLDER = (os.environ.get('UPLOAD_FOLDER', '').strip()
                     or os.path.join(BASE_DIR, 'uploads'))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size

    # --- AI -----------------------------------------------------------------
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    AI_INSIGHT_CACHE_TTL = _int('AI_INSIGHT_CACHE_TTL', 3600)

    # --- Financial institution synchronization (finance_sync) ---------------
    # Mirrored into the config rather than left to `os.environ` alone.
    # `finance_sync/adapters/plaid_adapter.py` still reads the environment
    # directly -- it has to, since it runs on the scheduler thread with no
    # application -- so these exist for one purpose: giving
    # `ProductionConfig.validate` something to check. A value only ever read at
    # the point of use cannot be validated at boot, which is how a deployment
    # discovers a half-set credential at its first sync instead of at startup.
    PLAID_CLIENT_ID = os.environ.get('PLAID_CLIENT_ID', '')
    # Resolved through the same per-environment lookup the adapter uses -- see
    # `_plaid_secret`. Reading the bare name here would report a complete
    # credential as half a credential.
    PLAID_SECRET = _plaid_secret()
    PLAID_ENV = os.environ.get('PLAID_ENV', 'sandbox')

    SYNC_AUTO_ENABLED = _bool('SYNC_AUTO_ENABLED', True)
    SYNC_INTERVAL_HOURS = float(os.environ.get('SYNC_INTERVAL_HOURS', 12))
    # When True, manual sync API calls run inline instead of on a background
    # thread (used by the test suite for determinism).
    SYNC_SYNCHRONOUS = False

    # --- Backups  [Phase 10.6] ----------------------------------------------
    # On by default, which is the opposite of SYNC_AUTO_ENABLED's history and
    # deliberately so. An unattended sync that nobody wanted makes network calls
    # to somebody's bank; an unattended backup writes a file next to a file. The
    # failure modes are not symmetric, and the one this defaults toward is the
    # one where a deployment nobody configured still has yesterday's data.
    BACKUP_AUTO_ENABLED = _bool('BACKUP_AUTO_ENABLED', True)
    BACKUP_INTERVAL_HOURS = float(os.environ.get('BACKUP_INTERVAL_HOURS', 24))
    # A week of dailies. The bound that matters is disk: these are whole copies
    # of the database, so N snapshots is N times its size.
    BACKUP_KEEP = _int('BACKUP_KEEP', 7)
    # Empty means "beside the database file", which is what puts snapshots on
    # the mounted volume in production rather than inside the replaceable image.
    # See `dough.services.backup.backup_target`.
    BACKUP_DIR = os.environ.get('BACKUP_DIR', '')

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

    # --- Error monitoring  [Phase 10.7] -------------------------------------
    # Empty means off, which is the state of every development machine and the
    # test suite. See dough/monitoring.py for what is scrubbed before an event
    # leaves the process -- the answer is "local variables and request bodies
    # wholesale", because a stack frame here can hold somebody's bank data.
    SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
    # Performance tracing, off by default. It samples request timings, not
    # payloads, but it is a second stream of data leaving the process and
    # should be a deliberate choice rather than a default.
    SENTRY_TRACES_SAMPLE_RATE = float(
        os.environ.get('SENTRY_TRACES_SAMPLE_RATE', 0) or 0)
    # Tags each event with the deployed commit, so an error can be traced to a
    # version. Railway exposes the commit as RAILWAY_GIT_COMMIT_SHA.
    RELEASE = (os.environ.get('RELEASE')
               or os.environ.get('RAILWAY_GIT_COMMIT_SHA', ''))

    # --- Legal pages  [Phase 10.7] ------------------------------------------
    # Who is promising what, to whom, under which law. Read by /privacy and
    # /terms, and left EMPTY by default on purpose: an unset value renders as a
    # visible `[...]` marker rather than as a plausible-looking default. There
    # is no safe default for the name of the entity accepting liability, and a
    # guess is worse than a blank because a blank gets noticed.
    LEGAL_ENTITY = os.environ.get('LEGAL_ENTITY', '')
    LEGAL_CONTACT_EMAIL = os.environ.get('LEGAL_CONTACT_EMAIL', '')
    LEGAL_JURISDICTION = os.environ.get('LEGAL_JURISDICTION', '')

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
    # MAIL_DEFAULT_SENDER is accepted as a second name for MAIL_FROM because it
    # is the name `flask_mail` uses, and therefore the name every provider's
    # setup page prints -- Postmark's included. This application is not built on
    # `flask_mail`, so an operator following those instructions sets a variable
    # nothing reads, and the failure is not "no From address": it is the default
    # `dough@localhost`, which a hosted provider rejects for every message
    # because it is not a verified sender. One accepted alias is cheaper than
    # that diagnosis. MAIL_FROM wins if somehow both are set.
    MAIL_FROM = (os.environ.get('MAIL_FROM')
                 or os.environ.get('MAIL_DEFAULT_SENDER')
                 or 'dough@localhost')
    MAIL_SERVER = os.environ.get('MAIL_SERVER', '')
    MAIL_PORT = _int('MAIL_PORT', 587)
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_USE_TLS = _bool('MAIL_USE_TLS', True)
    # MAIL_BACKEND=postmark sends over HTTPS instead of SMTP, which is the only
    # thing that works on a host blocking outbound mail ports -- Railway blocks
    # 587 and 2525 alike. The token defaults to MAIL_PASSWORD because with
    # Postmark they are the same string, so switching transports is one
    # variable rather than a second copy of a credential to keep in step.
    MAIL_POSTMARK_TOKEN = os.environ.get('MAIL_POSTMARK_TOKEN', '')
    MAIL_POSTMARK_STREAM = os.environ.get('MAIL_POSTMARK_STREAM', 'outbound')

    # --- Rate limiting  [Phase 10.5, SEC-0018] ------------------------------
    # `memory` is one process and resets on restart -- see SEC-0010 and the
    # docstring of dough/services/ratelimit.py. `redis` is the declared upgrade
    # path and raises until somebody implements it, rather than falling back.
    RATELIMIT_BACKEND = os.environ.get('RATELIMIT_BACKEND', 'memory')
    REDIS_URL = os.environ.get('REDIS_URL', '')
    # A switch, not an environment variable by default, for the reason
    # CSRF_ENABLED has none: an operator one `RATELIMIT_ENABLED=0` away from an
    # unlimited deployment is a deployment that will eventually be unlimited.
    # TestingConfig turns it off so the ~890 tests that predate it keep making
    # as many requests as they like; tests/test_ratelimit.py turns it back on.
    RATELIMIT_ENABLED = True

    # --- Encryption at rest  [Phase 10.5] -----------------------------------
    # The Fernet key protecting `connected_accounts.auth_blob` -- the Plaid and
    # Coinbase access tokens, which are the credentials that read a family's
    # real bank data.
    #
    # Two names, and the older one still wins if both are set. `ENCRYPTION_KEY`
    # is what the deployment documentation now asks for; `SYNC_ENCRYPTION_KEY`
    # is what existing installations have in their `.env`, and silently
    # preferring the new name would decrypt nothing on the first machine that
    # sets both -- an outage whose symptom ("stored credentials cannot be
    # decrypted") points at the wrong thing entirely.
    ENCRYPTION_KEY = (os.environ.get('SYNC_ENCRYPTION_KEY')
                      or os.environ.get('ENCRYPTION_KEY', ''))
    # Whether a missing key may be generated and persisted next to the database.
    # Production sets this False: a generated key means the tokens encrypted
    # under the *previous* generated key can no longer be read, and the failure
    # arrives at the next sync rather than at boot.
    ALLOW_GENERATED_ENCRYPTION_KEY = True

    # --- Marketing ----------------------------------------------------------
    # Everything here is social proof, which is the one category of claim on the
    # landing page that cannot be checked by reading this repository. The
    # security and product copy is sourced from templates/privacy.html and is
    # true of the code; "trusted by thousands" is true of an install or it is
    # not, and nobody visiting can tell the difference. So all three default to
    # empty and their sections render nothing -- a fabricated number on a page
    # that is asking for a bank connection costs more trust than it buys.
    #
    #   MARKETING_TESTIMONIALS  [{'quote': ..., 'name': ...}]
    #   MARKETING_STATS         [{'value': '$18M+', 'label': 'in tracked assets'}]
    #   MARKETING_PRESS         [{'name': 'TechCrunch', 'url': ...}]   url optional
    #
    # [consumed from Phase 8; stats and press added in 10.8]
    MARKETING_TESTIMONIALS = []
    MARKETING_STATS = []
    MARKETING_PRESS = []

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
    # No backup thread under test. The suite's database is `sqlite://` or a
    # tmp_path file, so `backup_target` would decline anyway -- this says so at
    # the level of intent rather than relying on that.
    BACKUP_AUTO_ENABLED = False
    # Captures every outbound message in a list instead of printing or sending
    # it, so a test can assert a reset mail went to the right address without a
    # network or a monkeypatch. See dough/services/email.py::MemoryBackend.
    MAIL_BACKEND = 'memory'
    # Off here for the reason CSRF_ENABLED is off here: the ~890 tests that
    # predate this phase make as many requests as they like, and several make
    # more than any policy allows. tests/test_ratelimit.py turns it back on,
    # which is the only way the limiter gets exercised.  [Phase 10.5]
    RATELIMIT_ENABLED = False
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
    ALLOW_GENERATED_ENCRYPTION_KEY = False

    @classmethod
    def validate(cls):
        """Refuse to start without the secrets, naming all of them at once.

        Every message says what the variable is for and how to produce a value,
        because the alternative is an operator searching this repository at
        deploy time for what `ENCRYPTION_KEY` is supposed to contain.

        No value is ever included in a message, only names. This function runs
        at boot, its exception goes to the log, and a validator that quoted the
        thing it found invalid would put a real secret into the log line
        reporting that a secret was wrong.
        """
        problems = []
        for secret in REQUIRED_SECRETS:
            if not secret.required:
                continue
            if not getattr(cls, secret.name, ''):
                problems.append(
                    f'  {secret.name}\n'
                    f'      {secret.why}\n'
                    f'      Set it with: {secret.how}')

        # Both or neither. One half of a Plaid credential is not a partly
        # configured integration -- it is one that authenticates against the
        # live API and fails, in a deployment whose operator believes it is
        # configured. The sandbox fallback (no credentials at all) is a working
        # state; this is not.
        if bool(cls.PLAID_CLIENT_ID) != bool(cls.PLAID_SECRET):
            problems.append(
                '  PLAID_CLIENT_ID / PLAID_SECRET\n'
                '      Exactly one of the pair is set. Set both to use Plaid, '
                'or neither to run the sync adapters in sandbox mode.')

        if problems:
            raise RuntimeError(
                'This deployment cannot start. APP_ENV=production requires the '
                'following, and none of it can be safely defaulted:\n\n'
                + '\n\n'.join(problems)
                + '\n\nSee docs/security.md, "Secrets", and .env.example.')

    @classmethod
    def warnings(cls):
        """Configuration that is legal, deployable, and probably not intended.

        Separate from `validate` because these must not stop a deployment. Each
        one has a real use — a demo instance genuinely may want console mail —
        and refusing to boot over a judgement call is how operators learn to set
        an override variable that then hides the checks that mattered.

        Returned rather than logged so the caller owns the logger; `create_app`
        emits them at WARNING once the logging configuration exists.
        """
        notes = []
        if (cls.MAIL_BACKEND or '').lower() == 'console':
            notes.append(
                'MAIL_BACKEND=console in production: verification and password '
                'reset links are printed to this process\'s stdout instead of '
                'being sent. Nobody who is locked out can reach them. Set '
                'MAIL_BACKEND=smtp and MAIL_SERVER.')
        if (cls.RATELIMIT_BACKEND or '').lower() == 'memory':
            notes.append(
                'RATELIMIT_BACKEND=memory: limits are per process and reset on '
                'restart (SEC-0010). Correct only for the single-worker '
                'deployment this application documents (OPS-0012).')
        if not cls.PUBLIC_BASE_URL:
            notes.append(
                'PUBLIC_BASE_URL is unset: links in outbound mail are built '
                'from the incoming request\'s Host header, which is '
                'client-controlled. Set it to this deployment\'s canonical URL.')
        # A warning rather than a refusal, on the same principle as the rest of
        # this list -- but the loudest one here, because it is the only entry
        # whose symptom is visible to the public. An unset value renders a
        # literal `[OPERATING ENTITY - set LEGAL_ENTITY]` on a live /privacy
        # page, which is worse than an operator seeing a log line.
        #
        # Not in `validate`, because an internal or demo deployment with no
        # outside users is a real state and should not be unable to boot over
        # it. `docs/runbooks/launch-checklist.md` is what makes this a gate on
        # opening registration.
        missing_legal = [name for name, value in (
            ('LEGAL_ENTITY', cls.LEGAL_ENTITY),
            ('LEGAL_CONTACT_EMAIL', cls.LEGAL_CONTACT_EMAIL),
            ('LEGAL_JURISDICTION', cls.LEGAL_JURISDICTION)) if not value]
        if missing_legal:
            notes.append(
                f'{", ".join(missing_legal)} unset: /privacy and /terms are '
                'serving visible placeholder markers to the public. Set these '
                'before anyone outside your household can reach this '
                'deployment. See docs/runbooks/launch-checklist.md.')
        if not cls.SENTRY_DSN:
            notes.append(
                'SENTRY_DSN is unset: unhandled exceptions are written to this '
                'process\'s logs and reported nowhere. You will learn about a '
                '500 when a user tells you.')
        return notes


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
    if not cls.ENCRYPTION_KEY and cls.ALLOW_GENERATED_ENCRYPTION_KEY:
        cls.ENCRYPTION_KEY = _load_or_create_encryption_key(cls.BASE_DIR)

    cls.validate()
    return cls


# Backwards compatibility: `from config import Config` still resolves, so any
# script or notebook outside the app package keeps working.
Config = DevelopmentConfig
