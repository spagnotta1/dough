"""The application factory, and nothing that answers a URL.

What is left here after Phase 7 is the wiring that applies to the whole
application: configuration, the database, the request hooks for authentication,
CSRF and tenancy, the error handlers, and the template filters. Every route
lives in `dough/blueprints/`.

The import list below is the readable measure of that. Phase 3 took the query
helpers out (numpy, scikit-learn and SQLAlchemy's and_/or_ are the services'
dependencies now). Phase 4 took the model provider out -- `import anthropic`
exists only in `dough/ai/anthropic_adapter.py`. Phase 7 took pandas, the
markdown renderer and every model class out, because the code that read them
went with the routes.

The AI caches that used to be module globals here (`_insight_cache`,
`_brief_cache`, `_wealth_cache`) are gone for a different reason: they were
process-global and keyed only by time, which is a cross-tenant leak. Caching
goes through `dough/ai/cache.py`, whose keys carry a household scope.
"""

import os
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
from flask import (Flask, render_template, request, jsonify, g, session)
from flask_migrate import Migrate

from config import get_config
# Phase 6. Authentication, authorization and CSRF live in dough/auth.py for the
# same reason tenancy lives in dough/tenancy.py: the rules have to be readable
# in one place, and `tests/test_route_guard.py` enumerates the markers off the
# view functions rather than trusting a list kept somewhere else.
from dough.auth import (CSRFError, SAFE_METHODS, csrf_field, csrf_token,
                        is_csrf_exempt, is_public, unauthorized_response,
                        validate_csrf, wants_json)
from dough.ai import catalog
from dough.ai.service import AIService
from dough.blueprints import register as register_blueprints
from dough.logging import configure_logging, current_trace_id
# Imported here rather than left to whichever blueprint happens to need it
# first: importing this module is what installs the before_flush hook that makes
# audit rows append-only, and a guarantee that depends on import order is not a
# guarantee. `create_app` puts it on `app.extensions` so the dependency is a
# fact about the application rather than a comment about an unused import.
from dough.services import audit
from dough.services.cache import household_scope
from dough.services.finance_context import build_finance_context
from dough.services.networth import wealth_snapshot
from dough.tenancy import TenantContextMissing, tenant_scope, unscoped
from models import AppUser, Household, db
from finance_sync.routes import sync_bp
from finance_sync.scheduler import init_scheduler

# The one-off stamp that adopts a database built by the old inline bootstrap.
# `holdings` was created by raw DDL at boot, so revision b1c2d3e4f5a6 -- whose
# whole job is `op.create_table('holdings')` -- can never run against it.
_ADOPTION_REVISION = 'b1c2d3e4f5a6'


def _upgrade_database(app):
    """Run `flask db upgrade` at boot, refusing to guess when it cannot.

    Called only when AUTO_UPGRADE_DB is set (true in development, false in
    production, where migrations are a deliberate deploy step -- see ADR-0007).

    The two guards below exist because this app spent its life with two
    competing schema authorities: the Alembic chain and a ~90-line inline
    bootstrap in create_app. A database maintained by the bootstrap has tables
    the chain thinks it still has to create, so `upgrade()` dies with
    "table holdings already exists" -- a message that tells the operator
    nothing about what to do next. Detect those states and say the fix out loud
    instead.
    """
    from alembic.migration import MigrationContext
    from flask_migrate import upgrade
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    tables = {t for t in inspector.get_table_names() if not t.startswith('sqlite_')}
    app_tables = tables - {'alembic_version'}
    with db.engine.connect() as connection:
        stamped = MigrationContext.configure(connection).get_current_revision()

    if app_tables and stamped is None:
        raise RuntimeError(
            'This database has tables ({n} of them) but no Alembic version, so '
            'it was built by the pre-migration bootstrap. Running the chain '
            'from scratch would fail on the first CREATE TABLE. Back it up and '
            'adopt it:\n'
            '    python tools/backup_db.py\n'
            '    flask db stamp {rev}\n'
            'then start the app again.'.format(n=len(app_tables),
                                               rev=_ADOPTION_REVISION))

    if stamped == 'a1b2c3d4e5f6' and 'holdings' in tables:
        raise RuntimeError(
            'This database is stamped a1b2c3d4e5f6 but already has a `holdings` '
            'table, which the next revision ({rev}) would try to create. The '
            'old inline bootstrap made it without recording the revision. Back '
            'up and re-stamp past it:\n'
            '    python tools/backup_db.py\n'
            '    flask db stamp {rev}\n'
            'then start the app again. {rev} is a no-op against this database '
            'by inspection -- `holdings` is the only thing it creates.'.format(
                rev=_ADOPTION_REVISION))

    upgrade()


def _seed_default_household(app):
    """Create the default household the test suite runs as.

    The testing shortcut above builds the schema but no rows, and TestingConfig
    turns auth off — so there is no user to resolve a household from and no
    migration to create one. Without this every test in the suite would raise
    TenantContextMissing on its first query.

    Only ever called under TESTING. The production path gets its household from
    `20260726_02_multitenancy` (existing installs) or from `/setup` (new ones),
    and creating one implicitly at boot would paper over a failed migration.
    """
    household_id = app.config['DEFAULT_HOUSEHOLD_ID']
    with unscoped():   # Household is the tenant; it has no household of its own
        if db.session.get(Household, household_id) is None:
            db.session.add(Household(id=household_id, name='Test household'))
            db.session.commit()


def create_app(test_config=None, config_name=None):
    app = Flask(__name__)
    # A test_config carrying TESTING selects TestingConfig as the base, rather
    # than layering test overrides on top of whatever APP_ENV happens to be in
    # the developer's shell. Without this, a shell with APP_ENV=production set
    # would run the suite against production defaults -- auth on, secure-only
    # cookies -- and fail in ways that have nothing to do with the code.
    if config_name is None and test_config and test_config.get('TESTING'):
        config_name = 'testing'
    app.config.from_object(get_config(config_name))
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    db.init_app(app)
    Migrate(app, db)
    # One AI service per app, on app.extensions rather than a module global, so
    # the suite's many create_app() calls cannot share a key or a cache. Reached
    # from a route via current_ai(). A test_config may pass AI_ADAPTER to swap
    # in EchoAdapter and exercise the real route bodies with no network.
    # scope_provider is what closes SEC-0003: every AI cache key is namespaced
    # by household, so the generated paragraph about one family's spending is
    # not reachable from another family's dashboard. Phase 4 built the seam and
    # filled it with a constant; this is the line that was waiting for
    # households to exist.
    AIService.init_app(app, adapter=app.config.get('AI_ADAPTER'),
                       scope_provider=household_scope)

    # Logging first, and before any other hook, so that anything the rest of
    # this function or the request chain logs already carries a trace id. It
    # registers a before_request of its own, and Flask runs those in
    # registration order -- installed after the auth guard, the 401 a rejected
    # request receives would have no trace id to quote back.  [Phase 8]
    configure_logging(app)
    # The append-only guard was installed by importing the module above; this
    # records that the application depends on it having happened.  [Phase 8]
    app.extensions['dough_audit'] = audit

    @app.context_processor
    def _inject_ai_catalog():
        """The model list, for the two pickers.

        Previously chat.html and rules.html each hardcoded the same three ids
        with their own labels and their own default, and disagreed about both.
        """
        return {'ai_models': catalog.all_models(),
                'ai_default_model': catalog.resolve().provider_id}

    @app.template_filter('money')
    def _money_filter(value, decimals=0):
        """Format a number the way people write money: -$341, not $-341.

        Every currency figure in a template should go through this. Doing it
        inline with format() put the minus sign in the wrong place wherever a
        value could go negative, which on a finance dashboard is most of them.
        """
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return value
        sign = '-' if amount < 0 else ''
        return f'{sign}${abs(amount):,.{decimals}f}'

    @app.context_processor
    def _inject_current_user():
        uid = session.get('user_id')
        user = AppUser.query.get(uid) if uid else None
        return {'current_username': user.username if user else None}

    # ---------------------------------------------------------------------------
    # Login — a single owner account with a hashed password, created on first
    # run via /setup. Always on outside the test suite: this app fronts real
    # financial data, from the PC browser and the phone WebView alike.
    # ---------------------------------------------------------------------------
    # Session cookie hardening (SameSite/Secure/HttpOnly) now lives in
    # config.py, so it is visible in one place and can be overridden per
    # environment and by test_config. It used to be assigned here, after the
    # test_config merge, which meant a test could not override it.
    # See docs/security.md SEC-0001 for why setdefault was wrong.
    auth_enabled = app.config['AUTH_ENABLED']
    if auth_enabled:
        def _enforce_session_lifetime():
            """Validate a session that carries a user id. None means proceed.

            Three ways a session with a `user_id` in it is still not usable:

            1. **The user is gone.** Before this phase nothing deleted an
               AppUser, so this was unreachable. "Remove member" makes it
               reachable, and the old code's behaviour would have been a 500:
               `_require_login` let the request through on the strength of the
               id alone, `_household_for_request` then found no user and bound
               no household, and the first scoped query raised
               TenantContextMissing. What actually happened to that person is
               that they were removed, so they get a clean sign-out.

            2. **Idle too long.** SESSION_IDLE_SECONDS since the last request.

            3. **Open too long.** SESSION_ABSOLUTE_SECONDS since sign-in,
               regardless of activity. The idle timer alone can be held open
               forever by a tab that polls, which several pages here do.

            The two limits were declared in `config.py` in Phase 1 and nothing
            has read them until now.
            """
            from dough.auth import current_user as _lookup_user

            now = int(time.time())
            if _lookup_user() is None:
                session.clear()
                return unauthorized_response()

            started = session.get('signed_in_at')
            seen = session.get('seen_at')
            absolute = app.config['SESSION_ABSOLUTE_SECONDS']
            idle = app.config['SESSION_IDLE_SECONDS']
            if started and absolute and now - started > absolute:
                session.clear()
                return unauthorized_response()
            if seen and idle and now - seen > idle:
                session.clear()
                return unauthorized_response()

            # Written on every request, so the idle window slides. Flask only
            # re-sends the cookie when the session is modified, and this
            # modifies it -- acceptable here because the session is a signed
            # cookie with nothing to write to on the server.
            session['seen_at'] = now
            return None

        @app.before_request
        def _require_login():
            """Default-deny. A view runs anonymously only if it says `@public`.

            The endpoint-name allowlist this replaces was fail-open: a route
            added without anyone thinking about auth inherited whatever the
            allowlist happened to say, and the allowlist lived in a different
            file from the route. Now the answer travels with the view.

            An unknown endpoint (404) is treated as protected, so probing for
            routes does not distinguish "does not exist" from "needs a login".
            """
            view = app.view_functions.get(request.endpoint)
            if view is not None and (is_public(view) or request.endpoint == 'static'):
                return None
            if session.get('user_id'):
                return _enforce_session_lifetime()
            return unauthorized_response()

    # ---------------------------------------------------------------------------
    # CSRF  [Phase 6]
    #
    # Registered *after* the auth block and *outside* the `if auth_enabled`, and
    # both halves of that are deliberate.
    #
    # After, because ordering decides what an anonymous POST is told. Checking
    # CSRF first would answer 403 "could not be verified" to a caller whose
    # actual problem is an expired session — misleading, and it would undo the
    # 401 negotiation directly above. A request with no session has no session
    # to forge, so authentication is the earlier question.
    #
    # Outside, because a CSRF layer that only exists when authentication is on
    # is a layer that most of this suite never exercises. `CSRF_ENABLED` is
    # False under TestingConfig so the ~180 tests predating this phase keep
    # posting bare forms, but the markers and the registration stay live in
    # every configuration, and tests/test_auth.py switches it back on.
    #
    # A `before_request` rather than a decorator per route: there are 34 unsafe
    # routes, and the one that matters is whichever gets added next without the
    # decorator. Default-deny makes forgetting a 403 rather than a hole.
    # ---------------------------------------------------------------------------

    app.jinja_env.globals['csrf_token'] = csrf_token
    app.jinja_env.globals['csrf_field'] = csrf_field

    @app.before_request
    def _verify_csrf():
        if request.method in SAFE_METHODS:
            return None
        view = app.view_functions.get(request.endpoint)
        if view is not None and is_csrf_exempt(view):
            return None
        if not app.config.get('CSRF_ENABLED', True):
            return None
        validate_csrf()
        return None

    @app.after_request
    def _no_store_for_authenticated_responses(response):
        """SEC-0008. Keep signed-in pages out of the browser's disk cache.

        The failure this prevents is a shared or borrowed machine: after
        logging out, the back button restores the dashboard from bfcache with
        real balances on it, because nothing told the browser not to keep it.
        `no-store` is the only directive that covers bfcache; `no-cache` and
        `private` do not.

        Applied by session, not by route. Deciding per route means every route
        added later is a decision somebody has to remember to make, and the
        cost of getting it wrong is silent. Static files are excluded because
        they are the same bytes for everyone and are the one thing here worth
        caching -- and `/health` because a probe has no session anyway.
        """
        if request.endpoint == 'static':
            return response
        if session.get('user_id') or not app.config['AUTH_ENABLED']:
            response.headers.setdefault(
                'Cache-Control', 'no-store, no-cache, must-revalidate, private')
            response.headers.setdefault('Pragma', 'no-cache')
        return response

    @app.errorhandler(CSRFError)
    def _csrf_failed(error):
        """403, and say why in the log rather than in the body.

        Rendering the login page is not a redirect on purpose: a rejected POST
        must not be replayed, and a 303 to a GET would lose the fact that
        anything was refused. The status code is what a `fetch` caller acts on.
        """
        app.logger.warning('CSRF rejection: %s %s origin=%r sec-fetch-site=%r',
                           request.method, request.path,
                           request.headers.get('Origin'),
                           request.headers.get('Sec-Fetch-Site'))
        if wants_json():
            return jsonify({'error': 'csrf verification failed'}), 403
        return render_template('login.html', error=CSRFError.description), 403

    # ---------------------------------------------------------------------------
    # Tenancy — bind the household for the duration of the request  [Phase 5]
    #
    # Registered *after* the auth block on purpose. Flask runs before_request
    # handlers in registration order, and `_require_login` short-circuits an
    # anonymous request with a redirect; putting this first would mean resolving
    # a household for requests that are about to be turned away.
    #
    # The pair is before_request/teardown_request rather than a WSGI middleware
    # because teardown_request runs even when a handler raises, and a ContextVar
    # that is set and not reset outlives the request — the next request served
    # on that worker thread would inherit the previous caller's household. That
    # is the one failure mode of this design and it is why the reset is in a
    # teardown rather than at the end of the view.
    # ---------------------------------------------------------------------------

    def _household_for_request():
        """Which household this request runs as, or None for no tenant data.

        None is returned for anonymous requests to /login, /setup and /static.
        It is not a wildcard: nothing downstream treats it as "all households",
        and any scoped query reached without a household raises.
        """
        uid = session.get('user_id')
        if uid:
            user = AppUser.query.get(uid)
            if user is not None:
                return user.household_id
        if not app.config['AUTH_ENABLED']:
            # The suite runs with auth off (see TestingConfig) and ~180 tests
            # predate tenancy. They get the default household, which
            # create_app seeds below, so those tests keep exercising real route
            # bodies rather than a login flow they were not written for.
            return app.config['DEFAULT_HOUSEHOLD_ID']
        return None

    @app.before_request
    def _bind_tenant():
        household_id = _household_for_request()
        if household_id is None:
            return None
        scope = tenant_scope(household_id)
        scope.__enter__()
        g._tenant_scope = scope
        return None

    @app.teardown_request
    def _release_tenant(exception=None):
        scope = g.pop('_tenant_scope', None)
        if scope is not None:
            scope.__exit__(None, None, None)

    @app.errorhandler(TenantContextMissing)
    def _no_tenant(error):
        """Fail closed at the edge, too.

        Reaching here means a code path touched tenant data outside any
        household — a bug, not a user error, so it is a 500 and it is logged.
        The response deliberately says nothing about households: the client
        cannot act on it and the detail belongs in the log.
        """
        app.logger.error('Tenant context missing: %s', error)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'internal error'}), 500
        return render_template('base.html'), 500

    @app.template_filter('dict_update')
    def dict_update_filter(d, updates):
        """Jinja2 filter: return a copy of dict d merged with the updates dict."""
        result = dict(d)
        result.update(updates)
        return result

    # ---------------------------------------------------------------------------
    # Errors  [Phase 8]
    #
    # One shape for every failure: a message written for the person reading it,
    # the diagnosis in the log, and a trace id joining the two. Before this, an
    # unhandled exception produced Flask's default page -- no traceback with
    # DEBUG off, so not a disclosure, but nothing a user could quote and nothing
    # tying their report to a log line.
    #
    # The handlers below are registered *after* the CSRF and tenancy handlers so
    # those keep their specific responses; Flask prefers the most specific
    # registration, and both of those say something this one cannot.
    # ---------------------------------------------------------------------------

    def _error_response(status, title, message, expression='concerned'):
        trace = current_trace_id()
        if wants_json():
            body = {'error': message, 'status': status}
            if trace:
                body['trace_id'] = trace
            return jsonify(body), status
        return render_template('error.html', title=title, message=message,
                               expression=expression, trace_id=trace), status

    @app.errorhandler(404)
    def _not_found(error):
        return _error_response(
            404, 'Page not found',
            "That page doesn't exist. It may have moved, or the link may be "
            'out of date.', expression='searching')

    @app.errorhandler(403)
    def _forbidden(error):
        # `Forbidden` is raised with a written reason by @owner_required, and
        # that reason is for the user -- "Only a household owner can do that."
        # tells them something actionable. A generic message here would throw
        # that away.
        message = getattr(error, 'description', None) or \
            'You do not have permission to do that.'
        return _error_response(403, 'Not allowed', message)

    @app.errorhandler(413)
    def _too_large(error):
        return _error_response(
            413, 'That file is too large',
            'The upload exceeded the size limit. Try splitting the export into '
            'smaller files.')

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _unhandled(error):
        """Anything not handled above.

        Werkzeug's own HTTP exceptions are re-raised so a 405 stays a 405 --
        registering on `Exception` catches those too, and swallowing them would
        turn every wrong-method request into a 500.

        The exception is logged with `exc_info`, which the JSON formatter
        reduces to a type and a message: the frames are useful in development,
        where Flask's own handler still prints them, and are the most common way
        a local variable holding a secret reaches a log aggregator.
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return error

        app.logger.error('unhandled exception', exc_info=error)
        return _error_response(
            500, 'Something went wrong',
            'We hit an unexpected problem and nothing was changed. Try again in '
            'a moment — if it keeps happening, the reference below will tell us '
            'exactly what failed.', expression='concerned')

    # ---------------------------------------------------------------------------
    # Routes  [Phase 7]
    #
    # Registered last of the request machinery, and that is the only ordering
    # that matters here: `before_request` handlers run in registration order, so
    # authentication, CSRF and tenancy are all in place before any view can run.
    # Blueprint registration order among themselves is irrelevant — no two claim
    # the same rule, which `tests/test_url_map_snapshot.py` would catch.
    #
    # `auth` and `household` are inside the AUTH_ENABLED branch, which is why
    # this call is here rather than beside `sync_bp` below.
    # ---------------------------------------------------------------------------
    register_blueprints(app)

    # ---------------------------------------------------------------------------
    # Schema management — Alembic is the single source of truth (ADR-0007).
    #
    # This used to be ~90 lines of raw DDL: a CREATE TABLE IF NOT EXISTS, thirteen
    # ALTER TABLE ADD COLUMNs each swallowing its own exception, a hand-written
    # table rebuild, and db.create_all(). It ran on every boot and silently
    # diverged from both models.py and the migration chain — the rebuild's
    # `RENAME TO connected_accounts_old` left 27 dangling foreign keys behind,
    # because SQLite rewrites FK references in other tables when a table is
    # renamed. Revision 20260726_01_reconcile repairs that and takes over the job.
    # ---------------------------------------------------------------------------
    with app.app_context():
        if app.config.get('TESTING'):
            # Tests build a throwaway database per test; running eight revisions
            # each time would dominate the suite's runtime. tests/test_migrations.py
            # asserts create_all() and the migration chain produce identical
            # schemas, which is what keeps this shortcut honest.
            db.create_all()
            _seed_default_household(app)
        elif app.config.get('AUTO_UPGRADE_DB'):
            _upgrade_database(app)

    # ---------------------------------------------------------------------------
    # Financial institution synchronization (finance_sync)
    # ---------------------------------------------------------------------------
    app.register_blueprint(sync_bp)

    # Test seams. Both are snapshots the AI routes are built on, and those routes
    # need an API key and a network; the arithmetic is much better asserted
    # directly than through a rendered page. See tests/test_services.py.
    app.build_finance_context = build_finance_context
    app.wealth_snapshot = wealth_snapshot

    if app.config.get('SYNC_AUTO_ENABLED', True) and not app.config.get('TESTING'):
        # Start the background scheduler lazily on the first request so it only
        # runs in the serving process (never in the werkzeug reloader parent).
        @app.before_request
        def _ensure_sync_scheduler():
            init_scheduler(app, interval_hours=app.config.get('SYNC_INTERVAL_HOURS', 12))
    else:
        # Tests still need a scheduler object for the manual-refresh API,
        # but without the periodic background thread.
        init_scheduler(app, interval_hours=app.config.get('SYNC_INTERVAL_HOURS', 12),
                       autostart=False)

    return app

def _ensure_dev_cert(base_dir):
    """Create (once) and return a self-signed localhost certificate pair.

    Plaid requires https for OAuth redirect URIs even in sandbox, so local
    testing needs TLS. The cert covers localhost and this machine's LAN IP;
    browsers will show a one-time "not trusted" warning — that's expected
    for a self-signed cert and fine for development.
    """
    import ipaddress
    import socket
    from datetime import timezone

    cert_path = os.path.join(base_dir, '.dev-cert.pem')
    key_path = os.path.join(base_dir, '.dev-key.pem')
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Best-effort LAN IP so other devices on the network can use https too.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    sans = [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]
    if lan_ip:
        sans.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(key_path, 'wb') as fh:
        fh.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    with open(cert_path, 'wb') as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _truthy(name, default=''):
    return os.environ.get(name, default).lower() in ('1', 'true', 'yes')


if __name__ == '__main__':
    app = create_app()
    ssl_context = None
    if _truthy('APP_HTTPS'):
        ssl_context = _ensure_dev_cert(os.path.dirname(os.path.abspath(__file__)))

    # Login is always required (owner account created at /setup on first run),
    # so LAN exposure via APP_HOST=0.0.0.0 is acceptable; default stays
    # loopback-only regardless.
    host = os.environ.get('APP_HOST', '127.0.0.1')

    # An operational safeguard, not a style preference.  [Phase 8]
    #
    # Werkzeug's debugger is an interactive Python console attached to any
    # traceback. On loopback that is a development tool; on 0.0.0.0 it is remote
    # code execution for anyone who can reach the port and provoke an error --
    # and the PIN is not a security boundary. The combination was previously
    # prevented by a comment in `.env`, which is not a mechanism, and the
    # default in this very call is `'1'`.
    #
    # Refusing is the right response rather than silently dropping to debug=False:
    # somebody who asked for the debugger and did not get it will spend the
    # afternoon wondering why their breakpoints do nothing.
    debug = _truthy('APP_DEBUG', '1')
    if debug and host not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit(
            f'Refusing to start: APP_DEBUG is on and APP_HOST is {host!r}.\n'
            "Werkzeug's debugger executes arbitrary Python for anyone who can\n"
            'reach the port and trigger an error. Set APP_DEBUG=0 to expose the\n'
            'app on the network, or APP_HOST=127.0.0.1 to keep the debugger.')

    app.run(host=host, port=int(os.environ.get('APP_PORT', '5000')),
            debug=debug, ssl_context=ssl_context)
