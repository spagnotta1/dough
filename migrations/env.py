import configparser
import logging
import os
import sys
from logging.config import fileConfig

from flask import current_app

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


def _configure_logging():
    """Apply the ini's logging config, but never fail because of it.

    Flask-Migrate points Alembic at `migrations/alembic.ini`, which this project
    does not have -- the ini lives at the repo root instead, for the `alembic`
    CLI. `fileConfig` on a missing file raises KeyError: 'formatters', which
    happens at env.py import time, so `flask db upgrade` died before running a
    single revision. That is why the schema drifted: the migration chain was
    never actually runnable, and the inline bootstrap in app.py was the only
    thing maintaining the schema.

    Logging setup is not worth failing a migration over, so it is best-effort.
    When there is no usable ini, fall back to putting Alembic's own logger on
    stderr at INFO. Without that, `flask db upgrade` prints *nothing at all* --
    success and no-op are indistinguishable, and a schema change scrolls by
    invisibly, which is a large part of how the schema drifted unnoticed in the
    first place.
    """
    path = config.config_file_name
    parser = configparser.ConfigParser()
    if path and os.path.exists(path):
        try:
            parser.read(path)
        except configparser.Error:
            pass
        if parser.has_section('formatters'):
            fileConfig(path)
            return

    alembic_logger = logging.getLogger('alembic')
    # Alembic attaches a NullHandler to its own logger as a library, so an
    # `if not .handlers` test passes and then nothing is ever printed. Look for a
    # handler that actually emits somewhere.
    emits = any(not isinstance(h, logging.NullHandler)
                for h in alembic_logger.handlers)
    if not emits:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter('%(levelname)-5.5s [%(name)s] %(message)s'))
        alembic_logger.addHandler(handler)
    alembic_logger.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger('alembic.env')


def get_engine():
    db = current_app.extensions['migrate'].db
    try:
        # Flask-SQLAlchemy >= 3. Tried first: on 3.1 the legacy get_engine()
        # below still works but emits a DeprecationWarning on every revision,
        # which buried the real output of a migration run in noise.
        return db.engine
    except (TypeError, AttributeError):
        # Flask-SQLAlchemy < 3 and Alchemical.
        return db.get_engine()


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = current_app.extensions['migrate'].configure_args
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    # SQLite cannot ALTER a constraint, a column type, or a foreign key. Batch
    # mode rewrites those as create-temp / copy / drop / rename. Required before
    # any revision using op.batch_alter_table(). setdefault rather than a direct
    # pass: Flask-Migrate already injects this key, so passing it as an explicit
    # keyword below raises "got multiple values for keyword argument".
    conf_args.setdefault("render_as_batch", True)

    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
