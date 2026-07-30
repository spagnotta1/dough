"""Liveness and readiness, for whatever is supervising this process.

Two endpoints, and the distinction between them is the whole point:

- `/health/live` answers "is this process running?" It touches nothing. A
  supervisor that restarts on failure reads this one, so it must not fail for
  any reason a restart would not fix. A liveness probe that also checked the
  database would restart a perfectly healthy application every time the
  database blinked -- taking down the thing that was still serving cached pages
  in order to punish it for a dependency's outage.

- `/health/ready` answers "should traffic be sent here?" It checks the database,
  the migration state and the required configuration, and returns 503 when any
  of them is not satisfied.

Both are `@public`, which is deliberate and worth stating: a health check behind
a login is not a health check. That makes the response body an unauthenticated
disclosure surface, so it carries check *names* and booleans and nothing else --
no revision identifiers, no configuration keys, no versions, no error text. An
operator who needs the detail has the log line, which carries the same trace id.
"""

from flask import Blueprint, current_app, jsonify

from dough.auth import public

bp = Blueprint('health', __name__, url_prefix='/health')

#: Configuration that must be set and must not still be the development
#: default. Only the *names* appear here and only the names are ever reported;
#: a health endpoint that echoed a value would be the disclosure it exists to
#: prevent.
REQUIRED_CONFIG = ('SECRET_KEY', 'SQLALCHEMY_DATABASE_URI')

#: Values that mean "nobody set this".
_PLACEHOLDERS = frozenset({'', 'dev', 'development', 'change-me', 'changeme',
                           'secret', 'please-change'})


@bp.route('/live')
@public
def live():
    return jsonify({'status': 'ok'}), 200


@bp.route('/ready')
@public
def ready():
    checks = {
        'database': _database_reachable(),
        'migrations': _migrations_current(),
        'configuration': _configuration_present(),
    }
    ok = all(checks.values())
    if not ok:
        current_app.logger.warning(
            'readiness check failed',
            extra={'failed': sorted(k for k, v in checks.items() if not v)})
    return jsonify({'status': 'ok' if ok else 'unavailable',
                    'checks': checks}), (200 if ok else 503)


def _database_reachable():
    from sqlalchemy import text

    from models import db
    try:
        db.session.execute(text('SELECT 1'))
        return True
    except Exception:
        current_app.logger.exception('readiness: database unreachable')
        return False


def _migrations_current():
    """Is the schema at the head of the migration chain?

    Under TESTING the schema comes from `db.create_all()` and there is no
    `alembic_version` row to compare, so this reports True rather than failing
    every test that touches the endpoint. That shortcut is only honest because
    `tests/test_migrations.py` asserts create_all() and the migration chain
    produce identical schemas -- without that test this check would be
    meaningless in exactly the environment that runs it most.
    """
    if current_app.config.get('TESTING'):
        return True
    try:
        import os

        from alembic.script import ScriptDirectory
        from sqlalchemy import text

        from models import db

        row = db.session.execute(
            text('SELECT version_num FROM alembic_version')).fetchone()
        if row is None:
            return False
        # Flask-Migrate stores the directory as configured, which is the
        # relative 'migrations' by default -- resolving it against the app root
        # rather than the process working directory is what makes this work
        # under a supervisor that starts the app from somewhere else.
        directory = current_app.extensions['migrate'].directory
        if not os.path.isabs(directory):
            directory = os.path.join(current_app.root_path, directory)
        return row[0] in set(ScriptDirectory(directory).get_heads())
    except Exception:
        current_app.logger.exception('readiness: migration state unknown')
        return False


def _configuration_present():
    for name in REQUIRED_CONFIG:
        value = current_app.config.get(name)
        if value is None:
            return False
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS:
            return False
    return True
