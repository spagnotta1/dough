"""The migration chain is the schema authority, so it gets tested like one.

Before Phase 2 this repo had two competing authorities: the Alembic chain and a
~90-line inline bootstrap in `create_app` that ran raw DDL on every boot. They
diverged, and nobody noticed because `flask db upgrade` crashed inside
`migrations/env.py` before running a single revision -- so the chain was never
exercised and the bootstrap silently became the real schema.

These tests close that hole permanently:

  * the chain and `db.metadata` must agree, which is what lets conftest keep
    using `create_all()` for speed without that shortcut hiding drift;
  * the reconciliation revision must be idempotent, because it is inspector-
    driven and will be re-run against databases in several different states;
  * `app._upgrade_database` must refuse -- loudly and with the fix in the
    message -- rather than emit "table holdings already exists" when it meets a
    database the old bootstrap built.

Every test builds its own throwaway SQLite file. Nothing here can touch
`checkbook.db`.
"""

import os
import sqlite3
import sys

import pytest
import sqlalchemy as sa
from flask import Flask
from flask_migrate import Migrate, stamp, upgrade

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

import schema_report  # noqa: E402  (needs the tools/ path above)
from app import _ADOPTION_REVISION, _upgrade_database  # noqa: E402
from models import db  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(REPO_ROOT, 'migrations')

HEAD = '20260802_09_goals'
#: Revisions that several tests below target directly rather than through
#: `head`. They exist to characterise *that* revision, and running the chain
#: past it would mean asserting on a schema further along than the property
#: under test.
RECONCILE = '20260726_01_reconcile'
MULTITENANCY = '20260726_02_multitenancy'
LEGACY_REVISION = 'a1b2c3d4e5f6'


def _app(path):
    """The smallest app that can run migrations.

    Deliberately not `create_app`: this suite has to observe what the migration
    chain alone produces, and `create_app` runs `create_all()` under TESTING.
    An absolute `directory` keeps it independent of the working directory
    pytest happened to be launched from.
    """
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + path.replace('\\', '/')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    Migrate(app, db, directory=MIGRATIONS_DIR)
    return app


def _migrated(path, revision='head', stamp_to=None):
    """Build `path` by running the chain. Returns the path."""
    app = _app(path)
    with app.app_context():
        try:
            if stamp_to:
                stamp(revision=stamp_to)
            upgrade(revision=revision)
        finally:
            # Windows will not let tmp_path be cleaned up while the engine holds
            # the file open, and a leaked engine also breaks a later connect.
            db.engine.dispose()
    return path


def _created(path):
    """Build `path` from `db.metadata` alone -- what models.py declares."""
    engine = sa.create_engine('sqlite:///' + path.replace('\\', '/'))
    try:
        db.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


def _current_revision(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='alembic_version'").fetchall()
        if not rows:
            return None
        got = conn.execute('SELECT version_num FROM alembic_version').fetchall()
        return got[0][0] if got else None
    finally:
        conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")} - {'alembic_version'}
    finally:
        conn.close()


def _fk_violations(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        return conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close()


def _schema(path):
    schema = schema_report.read_schema(path)
    schema.pop('alembic_version', None)
    return schema


# ---------------------------------------------------------------------------
# The chain agrees with the models
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_chain_from_empty_matches_create_all(tmp_path):
    """The whole reason conftest can use create_all(): the two agree exactly.

    If this fails, the fast test fixture is testing a schema the real database
    will never have.
    """
    chain = _migrated(str(tmp_path / 'chain.db'))
    models = _created(str(tmp_path / 'models.db'))

    problems = schema_report.diff(_schema(chain), _schema(models),
                                  'migration-chain', 'models.py')
    assert problems == [], '\n'.join(problems)


@pytest.mark.slow
def test_chain_from_empty_reaches_head(tmp_path):
    chain = _migrated(str(tmp_path / 'chain.db'))
    assert _current_revision(chain) == HEAD
    # Anti-vacuity: a chain that created nothing would also produce no diff.
    assert len(_tables(chain)) >= 15


@pytest.mark.slow
def test_chain_leaves_no_dangling_foreign_keys(tmp_path):
    """The bug this phase fixed, pinned so batch mode cannot reintroduce it.

    `op.batch_alter_table` renames tables, and SQLite rewrites FK references in
    *other* tables when a table is renamed. That is how the old bootstrap left
    27 foreign keys pointing at a `connected_accounts_old` that no longer
    existed. A rebuild that names its temp table carelessly does it again.
    """
    chain = _migrated(str(tmp_path / 'chain.db'))
    assert _fk_violations(chain) == []


def test_chain_has_exactly_one_head():
    """Two heads mean `upgrade` picks arbitrarily or refuses. Cheap to check."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(os.path.join(MIGRATIONS_DIR, 'alembic.ini'))
    config.set_main_option('script_location', MIGRATIONS_DIR)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]


# ---------------------------------------------------------------------------
# The reconciliation revision is idempotent and reversible-by-policy
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_reconcile_is_idempotent(tmp_path):
    """Re-running it against an already-reconciled database changes nothing.

    This is the property the whole revision is built around: it is inspector-
    driven precisely so it can meet a fresh database, a live database that the
    bootstrap half-built, and an already-migrated one, and converge all three.
    Re-stamping to the previous revision and upgrading again exercises that
    through the real Alembic machinery rather than by calling upgrade() by hand.
    """
    path = _migrated(str(tmp_path / 'chain.db'), revision=RECONCILE)
    before = _schema(path)

    _migrated(path, revision=RECONCILE, stamp_to=_ADOPTION_REVISION)

    assert _current_revision(path) == RECONCILE
    problems = schema_report.diff(before, _schema(path), 'first-run', 'second-run')
    assert problems == [], '\n'.join(problems)
    assert _fk_violations(path) == []


@pytest.mark.slow
def test_reconcile_downgrade_is_a_deliberate_no_op(tmp_path):
    """Documented in ADR-0007: recovery is a backup restore, not a downgrade.

    The revision drops nothing and rebuilds tables in place, so a mechanical
    inverse would either be a lie (it cannot restore the 27 broken foreign keys,
    which is the point) or destructive. It therefore does nothing, and this test
    exists so that "does nothing" reads as a decision rather than an unfinished
    stub -- and so that anyone who later writes a real downgrade has to come
    here and say so.
    """
    from flask_migrate import downgrade

    path = _migrated(str(tmp_path / 'chain.db'), revision=RECONCILE)
    before = _schema(path)

    app = _app(path)
    with app.app_context():
        try:
            downgrade(revision=_ADOPTION_REVISION)
        finally:
            db.engine.dispose()

    assert _current_revision(path) == _ADOPTION_REVISION
    problems = schema_report.diff(before, _schema(path), 'before', 'after-downgrade')
    assert problems == [], (
        'downgrade() is documented as a no-op but changed the schema:\n'
        + '\n'.join(problems))


# ---------------------------------------------------------------------------
# The multi-tenancy revision, which -- unlike reconcile -- has a real downgrade
# ---------------------------------------------------------------------------

def test_default_household_id_matches_config():
    """The migration hardcodes it; config declares it. They must not drift.

    The migration cannot import config -- a revision that reads live
    configuration means a different thing on every machine that runs it -- so
    the two are separate constants and this is what keeps them equal. Drift
    would backfill every existing row to a household the application then never
    looks in, and the symptom is an account that appears completely empty.
    """
    import importlib.util

    from config import BaseConfig

    spec = importlib.util.spec_from_file_location(
        'multitenancy_revision',
        os.path.join(MIGRATIONS_DIR, 'versions', '20260726_02_multitenancy.py'))
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    assert revision.DEFAULT_HOUSEHOLD_ID == BaseConfig.DEFAULT_HOUSEHOLD_ID


@pytest.mark.slow
def test_multitenancy_round_trip_restores_the_schema_exactly(tmp_path):
    """down then up must land on the same schema it started from.

    ADR-0007 promises this revision is reversible, which is a claim about
    fourteen `batch_alter_table` rebuilds in each direction. Batch mode drops
    any index not declared in `copy_from`, and it does so silently -- the
    downgrade would look like it worked and the next upgrade would rebuild from
    a table quietly missing its uniqueness. Comparing the schema across a full
    round trip is what catches that.
    """
    from flask_migrate import downgrade

    path = _migrated(str(tmp_path / 'chain.db'))
    before = _schema(path)

    app = _app(path)
    with app.app_context():
        try:
            downgrade(revision=RECONCILE)
            assert _current_revision(path) == RECONCILE
            # The column is really gone, not merely nullable again.
            after_down = _schema(path)
            assert 'households' not in after_down
            assert 'household_id' not in after_down['transactions']['columns']
            upgrade(revision=HEAD)
        finally:
            db.engine.dispose()

    assert _current_revision(path) == HEAD
    problems = schema_report.diff(before, _schema(path), 'before', 'round-tripped')
    assert problems == [], '\n'.join(problems)
    assert _fk_violations(path) == []


def test_invitations_table_is_scoped_like_every_other_tenant_table(tmp_path):
    """`household_invites` must carry the same three things as its neighbours.

    A NOT NULL household_id, a foreign key that names it, and an index. The
    table is new rather than rebuilt, so none of `20260726_02`'s batch
    machinery ran here -- which is exactly why it is worth checking rather than
    assuming: a table added by hand is the one that quietly skips a step, and a
    missing NOT NULL would let an invitation exist in no household at all.
    """
    path = _migrated(str(tmp_path / 'chain.db'))
    schema = _schema(path)

    assert 'household_invites' in schema
    columns = schema['household_invites']['columns']
    assert 'household_id' in columns
    assert not columns['household_id']['nullable'], 'household_id must be NOT NULL'

    conn = sqlite3.connect(path)
    try:
        fks = conn.execute(
            "PRAGMA foreign_key_list('household_invites')").fetchall()
        assert any(row[2] == 'households' and row[3] == 'household_id'
                   for row in fks), 'no foreign key from household_id'
        indexes = [row[1] for row in
                   conn.execute("PRAGMA index_list('household_invites')")]
        assert 'ix_household_invites_household_id' in indexes
        # Unique installation-wide: the redemption lookup happens before any
        # household is known, so a per-household constraint could not be
        # enforced at the moment it matters.
        unique = [row[1] for row in
                  conn.execute("PRAGMA index_list('household_invites')") if row[2]]
        assert 'ix_household_invites_token_hash' in unique
    finally:
        conn.close()
    assert _fk_violations(path) == []


@pytest.mark.slow
def test_multitenancy_downgrade_refuses_to_merge_two_households(tmp_path, caplog):
    """Two households cannot be flattened into one, so it declines to try.

    Dropping `household_id` with more than one household present would present
    two families' transactions as a single ledger with nothing left to tell them
    apart -- and would then fail partway through anyway, when the restored global
    unique index met the first pair of rows that only differed by household. A
    partial rebuild is a worse outcome than a refusal.
    """
    from flask_migrate import downgrade

    # Built to MULTITENANCY rather than head: this characterises *that*
    # revision's refusal, and a multi-step downgrade from head would first
    # apply 03's downgrade successfully before meeting the refusal at 02 --
    # so the "left exactly where it was" assertion below would be measuring
    # the wrong thing.
    path = _migrated(str(tmp_path / 'chain.db'), revision=MULTITENANCY)

    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO households (id, name, created_at, updated_at) "
                 "VALUES (1, 'A', '2026-07-26 00:00:00', '2026-07-26 00:00:00')")
    conn.execute("INSERT INTO households (id, name, created_at, updated_at) "
                 "VALUES (2, 'B', '2026-07-26 00:00:00', '2026-07-26 00:00:00')")
    conn.commit()
    conn.close()

    app = _app(path)
    with app.app_context():
        try:
            # Flask-Migrate's `catch_errors` turns a RuntimeError out of a
            # revision into a logged message and a non-zero exit, which is what
            # an operator running `flask db downgrade` actually experiences --
            # so that is what this asserts, rather than the exception type the
            # revision happens to raise underneath.
            with pytest.raises(SystemExit):
                downgrade(revision=RECONCILE)
        finally:
            db.engine.dispose()

    assert '2 households' in caplog.text
    assert 'Export or delete' in caplog.text, 'the refusal must say what to do next'
    # Refusing must leave the database exactly where it was.
    assert _current_revision(path) == MULTITENANCY


@pytest.mark.slow
def test_migration_leaves_no_household_blind_uniqueness(tmp_path):
    """The six constraints that had to gain a household column, checked at the DB.

    Asserted here as well as in `tools/verify_tenancy.py` because they fail
    differently: the tool checks the operator's real database after a migration,
    this checks the revision itself on every CI run, before anyone's data is
    involved.
    """
    path = _migrated(str(tmp_path / 'chain.db'))
    schema = _schema(path)

    expected = {
        'account_balance': ('household_id', 'account_type'),
        'budgets': ('household_id', 'account_name', 'category'),
        'connected_accounts': ('household_id', 'institution', 'item_id'),
        'portfolio_snapshots': ('household_id', 'snapshot_date'),
        'recurring_dismissals': ('desc_key', 'household_id'),
        'transactions': ('account_name', 'amount', 'date', 'description',
                         'household_id'),
    }
    for table, columns in expected.items():
        signatures = schema_report._unique_index_signatures(schema[table])
        assert tuple(sorted(columns)) in signatures, (
            f'{table} does not enforce UNIQUE{list(columns)}; has {signatures}')
        # And the household-blind original must be gone, not just joined.
        stale = {s for s in signatures
                 if 'household_id' not in s and set(s) <= set(columns)}
        assert not stale, f'{table} still enforces household-blind {stale}'


# ---------------------------------------------------------------------------
# _upgrade_database: adopting databases the old bootstrap built
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_upgrade_database_brings_an_empty_file_to_head(tmp_path):
    path = str(tmp_path / 'fresh.db')
    app = _app(path)
    with app.app_context():
        try:
            _upgrade_database(app)
        finally:
            db.engine.dispose()
    assert _current_revision(path) == HEAD


@pytest.mark.slow
def test_upgrade_database_is_a_no_op_at_head(tmp_path):
    path = _migrated(str(tmp_path / 'chain.db'))
    before = _schema(path)
    app = _app(path)
    with app.app_context():
        try:
            _upgrade_database(app)
        finally:
            db.engine.dispose()
    assert _current_revision(path) == HEAD
    assert schema_report.diff(before, _schema(path), 'before', 'after') == []


@pytest.mark.slow
def test_upgrade_database_refuses_the_legacy_bootstrap_state(tmp_path):
    """The live database's exact shape: stamped a1b2c3d4e5f6, `holdings` present.

    The next revision's only job is `create_table('holdings')`, so upgrade()
    would die with "table holdings already exists" -- true, useless, and
    indistinguishable from a corrupt database to whoever is reading the log.
    """
    path = str(tmp_path / 'legacy.db')
    _migrated(path, revision=LEGACY_REVISION)

    # What the old bootstrap did: raw DDL, no revision recorded.
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE holdings (id INTEGER PRIMARY KEY, symbol VARCHAR(20))')
    conn.commit()
    conn.close()

    app = _app(path)
    with app.app_context():
        try:
            with pytest.raises(RuntimeError) as excinfo:
                _upgrade_database(app)
        finally:
            db.engine.dispose()

    message = str(excinfo.value)
    assert f'flask db stamp {_ADOPTION_REVISION}' in message
    assert 'tools/backup_db.py' in message
    # Refusing must not have half-migrated anything.
    assert _current_revision(path) == LEGACY_REVISION


@pytest.mark.slow
def test_upgrade_database_refuses_tables_with_no_alembic_version(tmp_path):
    """A database built purely by `create_all` or the bootstrap, never stamped.

    Running the chain from scratch against it fails on the first CREATE TABLE,
    so say "stamp it" rather than letting revision one die.
    """
    path = _created(str(tmp_path / 'unstamped.db'))
    assert _current_revision(path) is None

    app = _app(path)
    with app.app_context():
        try:
            with pytest.raises(RuntimeError) as excinfo:
                _upgrade_database(app)
        finally:
            db.engine.dispose()

    message = str(excinfo.value)
    assert f'flask db stamp {_ADOPTION_REVISION}' in message
    assert _current_revision(path) is None


# ---------------------------------------------------------------------------
# The bootstrap really is gone
# ---------------------------------------------------------------------------

_DDL_KEYWORDS = ('CREATE TABLE', 'ALTER TABLE', 'CREATE INDEX',
                 'CREATE UNIQUE INDEX', 'DROP TABLE', 'DROP INDEX')


def test_app_module_executes_no_raw_ddl():
    """The inline bootstrap must not creep back in one convenient ALTER at a time.

    Each of those thirteen `try: ALTER TABLE ... except: pass` lines was added
    for a good local reason. Collectively they replaced the migration chain.

    Matched via the AST rather than by grepping lines, so the prose above -- and
    the error messages in `_upgrade_database`, which have to name the DDL they
    are warning about -- don't trip it. Only strings actually handed to
    `execute()` or `text()` count.
    """
    import ast

    with open(os.path.join(REPO_ROOT, 'app.py'), encoding='utf-8') as handle:
        tree = ast.parse(handle.read())

    executed = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
        if name not in ('execute', 'executescript', 'text'):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                executed.append(arg.value)

    offenders = [sql for sql in executed
                 if any(kw in sql.upper() for kw in _DDL_KEYWORDS)]
    assert offenders == [], f'raw DDL executed from app.py: {offenders}'


def test_create_all_shortcut_is_confined_to_testing():
    """create_all() outside TESTING would silently re-establish a second authority."""
    with open(os.path.join(REPO_ROOT, 'app.py'), encoding='utf-8') as handle:
        lines = handle.read().splitlines()

    hits = [i for i, line in enumerate(lines)
            if 'create_all()' in line and not line.strip().startswith('#')]
    assert len(hits) == 1, f'expected one create_all() call, found {len(hits)}'

    # The guard is the `if app.config.get('TESTING')` immediately above it.
    context = '\n'.join(lines[max(0, hits[0] - 6):hits[0]])
    assert "TESTING" in context
