"""Reconcile the schema so Alembic is the single source of truth.

Revision ID: 20260726_01_reconcile
Revises: b1c2d3e4f5a6
Create Date: 2026-07-26

Background
----------
Until now this project had two competing schema mechanisms and only one of them
ever ran. `flask db upgrade` crashed at import time (env.py called fileConfig on
a `migrations/alembic.ini` that does not exist), so the Alembic chain stopped
being applied after `a1b2c3d4e5f6`. Everything since was maintained by an inline
block in app.py: a raw `CREATE TABLE IF NOT EXISTS holdings`, thirteen
`ALTER TABLE ... ADD COLUMN` statements each wrapped in `try/except: pass`, a
hand-written `connected_accounts` rebuild, and `db.create_all()`.

That left three concrete problems this revision fixes:

1. `financial_accounts.connection_id` and `sync_history.connection_id` both
   carry a foreign key to `connected_accounts_old` -- a table that no longer
   exists. The rebuild block did `ALTER TABLE connected_accounts RENAME TO
   connected_accounts_old`, and modern SQLite rewrites foreign key references in
   *other* tables when a table is renamed. Then it dropped the renamed table.
   `PRAGMA foreign_key_check` reports 27 violations. They are inert only because
   SQLite leaves foreign key enforcement off by default; they would fail
   immediately on PostgreSQL, and they would be copied into the rebuilt tables by
   any later batch operation.

2. A fresh install and the models disagreed. Because the bootstrap created
   `holdings` with raw SQL *before* `db.create_all()`, a brand-new database got a
   `holdings` table with no foreign key, no `ix_holdings_account_id`, and server
   defaults the model does not declare.

3. Missing indexes that the models declare: `ix_transactions_account_id`,
   `ix_transactions_import_batch_id`, `ix_holdings_account_id`, and the named
   `idx_budget_unique`.

Design
------
The revision has to converge two quite different starting points onto one
target: a live database carrying every bootstrap side effect, and an empty
database that has only seen revisions 1..b1c2d3e4f5a6. So it runs in four
ordered stages, the first three inspector-driven and idempotent:

    1. create the ten tables the chain never learned about   (fresh only)
    2. add the twelve columns the bootstrap used to ALTER in (fresh only)
    3. create missing indexes                                (both)
    4. rebuild six tables to the exact target shape          (both)

Stage 4 is unconditional and uses an explicit `copy_from`, never reflection.
Reflection would faithfully reproduce whatever drift the database already has --
including the dangling foreign keys this revision exists to remove.

The table definitions below are a frozen snapshot of models.py as of this date.
They are deliberately duplicated rather than imported: a migration that imports
live models silently changes meaning every time the models change, and this one
must keep producing the 2026-07-26 shape forever.

`downgrade()` is a no-op by design; see the note on it below and ADR-0007.
"""
import sqlalchemy as sa
from alembic import op

revision = '20260726_01_reconcile'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Stage 1 -- tables the Alembic chain never created.
# ---------------------------------------------------------------------------

def _create_missing_tables(existing):
    if 'app_users' not in existing:
        op.create_table(
            'app_users',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('username', sa.String(80), nullable=False),
            sa.Column('password_hash', sa.String(255), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('username'))

    if 'conversations' not in existing:
        op.create_table(
            'conversations',
            sa.Column('id', sa.String(36), nullable=False),
            sa.Column('title', sa.String(80), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'))

    if 'chat_messages' not in existing:
        op.create_table(
            'chat_messages',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.String(36), nullable=False),
            sa.Column('role', sa.String(20), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'))
        op.create_index('ix_chat_messages_session_id', 'chat_messages',
                        ['session_id'])

    if 'recurring_dismissals' not in existing:
        op.create_table(
            'recurring_dismissals',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('desc_key', sa.String(255), nullable=False),
            sa.Column('description', sa.String(255), nullable=False),
            sa.Column('kind', sa.String(20), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('desc_key'))

    if 'connected_accounts' not in existing:
        op.create_table(
            'connected_accounts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('institution', sa.String(40), nullable=False),
            sa.Column('item_id', sa.String(80), nullable=True),
            sa.Column('display_name', sa.String(80), nullable=False),
            sa.Column('status', sa.String(20), nullable=False),
            sa.Column('auth_blob', sa.Text(), nullable=True),
            sa.Column('token_expires_at', sa.DateTime(), nullable=True),
            sa.Column('last_sync_at', sa.DateTime(), nullable=True),
            sa.Column('last_sync_status', sa.String(20), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'))
        op.create_index('uq_institution_item', 'connected_accounts',
                        ['institution', 'item_id'], unique=True)

    if 'financial_accounts' not in existing:
        op.create_table(
            'financial_accounts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=False),
            sa.Column('external_id', sa.String(120), nullable=False),
            sa.Column('name', sa.String(120), nullable=False),
            sa.Column('account_type', sa.String(20), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False),
            sa.Column('mask', sa.String(10), nullable=True),
            sa.Column('balance', sa.Numeric(14, 2), nullable=False),
            sa.Column('available_balance', sa.Numeric(14, 2), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('last_synced_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['connection_id'], ['connected_accounts.id']),
            sa.PrimaryKeyConstraint('id'))
        op.create_index('ix_financial_accounts_connection_id',
                        'financial_accounts', ['connection_id'])
        op.create_index('idx_finacct_unique', 'financial_accounts',
                        ['connection_id', 'external_id'], unique=True)

    if 'sync_history' not in existing:
        op.create_table(
            'sync_history',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=True),
            sa.Column('institution', sa.String(40), nullable=True),
            sa.Column('trigger', sa.String(20), nullable=False),
            sa.Column('status', sa.String(20), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('accounts_synced', sa.Integer(), nullable=False),
            sa.Column('balances_updated', sa.Integer(), nullable=False),
            sa.Column('holdings_synced', sa.Integer(), nullable=False),
            sa.Column('transactions_added', sa.Integer(), nullable=False),
            sa.Column('transactions_skipped', sa.Integer(), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['connection_id'], ['connected_accounts.id']),
            sa.PrimaryKeyConstraint('id'))
        op.create_index('ix_sync_history_connection_id', 'sync_history',
                        ['connection_id'])

    if 'sync_errors' not in existing:
        op.create_table(
            'sync_errors',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('run_id', sa.Integer(), nullable=True),
            sa.Column('connection_id', sa.Integer(), nullable=True),
            sa.Column('institution', sa.String(40), nullable=True),
            sa.Column('error_type', sa.String(40), nullable=False),
            sa.Column('message', sa.Text(), nullable=False),
            sa.Column('is_transient', sa.Boolean(), nullable=False),
            sa.Column('attempt', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['run_id'], ['sync_history.id']),
            sa.PrimaryKeyConstraint('id'))
        op.create_index('ix_sync_errors_run_id', 'sync_errors', ['run_id'])

    if 'portfolio_snapshots' not in existing:
        op.create_table(
            'portfolio_snapshots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('snapshot_date', sa.Date(), nullable=False),
            sa.Column('checking', sa.Numeric(14, 2), nullable=False),
            sa.Column('savings', sa.Numeric(14, 2), nullable=False),
            sa.Column('total_cash', sa.Numeric(14, 2), nullable=False),
            sa.Column('brokerage', sa.Numeric(14, 2), nullable=False),
            sa.Column('crypto', sa.Numeric(14, 2), nullable=False),
            sa.Column('total_investments', sa.Numeric(14, 2), nullable=False),
            sa.Column('net_worth', sa.Numeric(14, 2), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('snapshot_date'))

    if 'market_prices' not in existing:
        op.create_table(
            'market_prices',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('symbol', sa.String(20), nullable=False),
            sa.Column('name', sa.String(100), nullable=True),
            sa.Column('price', sa.Numeric(14, 4), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False),
            sa.Column('asset_class', sa.String(20), nullable=True),
            sa.Column('source', sa.String(40), nullable=True),
            sa.Column('as_of', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('symbol'))


# ---------------------------------------------------------------------------
# Stage 2 -- the columns the bootstrap block used to ALTER in.
# ---------------------------------------------------------------------------

# (table, column). server_default is set where models.py declares one, and left
# off where the model only has a Python-side default -- matching what
# create_all() produces, which is the target.
_ADDED_COLUMNS = [
    ('transactions', sa.Column('notes', sa.Text(), nullable=True)),
    ('transactions', sa.Column('import_batch_id', sa.String(36), nullable=True)),
    ('transactions', sa.Column('anomaly_reviewed', sa.Boolean(), nullable=False,
                               server_default='0')),
    ('transactions', sa.Column('source', sa.String(10), nullable=False,
                               server_default='csv')),
    ('transactions', sa.Column('account_id', sa.Integer(), nullable=True)),
    ('transactions', sa.Column('external_id', sa.String(120), nullable=True)),
    ('holdings', sa.Column('source', sa.String(10), nullable=False,
                           server_default='manual')),
    ('holdings', sa.Column('account_id', sa.Integer(), nullable=True)),
    ('holdings', sa.Column('external_id', sa.String(120), nullable=True)),
    ('holdings', sa.Column('avg_cost', sa.Numeric(14, 4), nullable=True)),
    ('holdings', sa.Column('current_price', sa.Numeric(14, 4), nullable=True)),
    ('holdings', sa.Column('last_synced_at', sa.DateTime(), nullable=True)),
]


def _add_missing_columns(inspector):
    for table, column in _ADDED_COLUMNS:
        if table not in inspector.get_table_names():
            continue
        present = {c['name'] for c in inspector.get_columns(table)}
        if column.name not in present:
            op.add_column(table, column)


# ---------------------------------------------------------------------------
# Stage 3 -- indexes that are safe to create in place.
# ---------------------------------------------------------------------------

# Only for tables stage 4 does NOT rebuild. An index created here on a table
# that is later rebuilt would be dropped along with the table -- batch mode
# recreates only what `copy_from` declares, so those indexes live on the
# copy_from definitions in stage 4 instead.
_INDEXES = [
    ('ix_chat_messages_session_id', 'chat_messages', ['session_id'], False),
]


def _create_missing_indexes(inspector):
    tables = set(inspector.get_table_names())
    for name, table, columns, unique in _INDEXES:
        if table not in tables:
            continue
        if name in {i['name'] for i in inspector.get_indexes(table)}:
            continue
        op.create_index(name, table, columns, unique=unique)


# ---------------------------------------------------------------------------
# Stage 4 -- rebuild to the exact target shape.
#
# Frozen snapshots of each table AS IT EXISTS BEFORE this revision, used as
# `copy_from` so Alembic knows which columns to carry across. Never reflection:
# reflecting `financial_accounts` would pick up the dangling
# connected_accounts_old foreign key and copy it straight into the new table.
# ---------------------------------------------------------------------------

_meta = sa.MetaData()

# The live table carries UNIQUE(category, account_name) as an anonymous table
# constraint, which SQLite implements as sqlite_autoindex_budgets_1 inside the
# CREATE TABLE. It cannot be dropped by name because it has none. Omitting it
# from copy_from is how the rebuild sheds it -- copy_from declares what the new
# table is built from, so what is left out does not survive. The equivalent
# named index is created immediately afterwards, so uniqueness is never actually
# unenforced outside the transaction.
_OLD_BUDGETS = sa.Table(
    'budgets', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('category', sa.String(50), nullable=False),
    sa.Column('account_name', sa.String(50), nullable=False,
              server_default='both'),
    sa.Column('monthly_limit', sa.Numeric(10, 2), nullable=False),
    sa.Index('idx_budget_unique', 'category', 'account_name', unique=True),
)

_OLD_LOG_ENTRY = sa.Table(
    'log_entry', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('account_type', sa.String(50), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('description', sa.String(200), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('cleared', sa.Boolean()),
    sa.Column('created_at', sa.DateTime()),
    sa.Column('starting_balance', sa.Float(), nullable=False, server_default='0'),
    sa.Column('pending_total', sa.Float(), nullable=False, server_default='0'),
    sa.Column('cleared_balance', sa.Float(), nullable=False, server_default='0'),
    sa.Column('available_balance', sa.Float(), nullable=False, server_default='0'),
)

_OLD_TRANSACTIONS = sa.Table(
    'transactions', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('account_name', sa.String(50), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('description', sa.String(255), nullable=False),
    sa.Column('amount', sa.Numeric(10, 2), nullable=False),
    sa.Column('category', sa.String(50), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.Column('anomaly_score', sa.Float(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('import_batch_id', sa.String(36), nullable=True),
    sa.Column('anomaly_reviewed', sa.Boolean(), nullable=False, server_default='0'),
    sa.Column('source', sa.String(10), nullable=False, server_default='csv'),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('external_id', sa.String(120), nullable=True),
    sa.Index('idx_transaction_unique', 'account_name', 'date', 'description',
             'amount', unique=True),
    sa.Index('idx_transaction_external_unique', 'account_id', 'external_id',
             unique=True),
    sa.Index('ix_transactions_account_id', 'account_id'),
    sa.Index('ix_transactions_import_batch_id', 'import_batch_id'),
)

_OLD_HOLDINGS = sa.Table(
    'holdings', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('ticker', sa.String(20), nullable=False),
    sa.Column('name', sa.String(100), nullable=False),
    sa.Column('shares', sa.Numeric(14, 6), nullable=False, server_default='0'),
    sa.Column('current_value', sa.Numeric(12, 2), nullable=False),
    sa.Column('asset_class', sa.String(20), nullable=False, server_default='Stock'),
    sa.Column('account_name', sa.String(50), nullable=False,
              server_default='Brokerage'),
    sa.Column('updated_at', sa.DateTime()),
    sa.Column('source', sa.String(10), nullable=False, server_default='manual'),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('external_id', sa.String(120), nullable=True),
    sa.Column('avg_cost', sa.Numeric(14, 4), nullable=True),
    sa.Column('current_price', sa.Numeric(14, 4), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Index('idx_holding_sync_unique', 'account_id', 'ticker', unique=True),
    sa.Index('ix_holdings_account_id', 'account_id'),
)

_OLD_FINANCIAL_ACCOUNTS = sa.Table(
    'financial_accounts', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('connection_id', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(120), nullable=False),
    sa.Column('name', sa.String(120), nullable=False),
    sa.Column('account_type', sa.String(20), nullable=False),
    sa.Column('currency', sa.String(10), nullable=False),
    sa.Column('mask', sa.String(10), nullable=True),
    sa.Column('balance', sa.Numeric(14, 2), nullable=False),
    sa.Column('available_balance', sa.Numeric(14, 2), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Index('idx_finacct_unique', 'connection_id', 'external_id', unique=True),
    sa.Index('ix_financial_accounts_connection_id', 'connection_id'),
)

_OLD_SYNC_HISTORY = sa.Table(
    'sync_history', _meta,
    sa.Column('id', sa.Integer(), primary_key=True),
    sa.Column('connection_id', sa.Integer(), nullable=True),
    sa.Column('institution', sa.String(40), nullable=True),
    sa.Column('trigger', sa.String(20), nullable=False),
    sa.Column('status', sa.String(20), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('accounts_synced', sa.Integer(), nullable=False),
    sa.Column('balances_updated', sa.Integer(), nullable=False),
    sa.Column('holdings_synced', sa.Integer(), nullable=False),
    sa.Column('transactions_added', sa.Integer(), nullable=False),
    sa.Column('transactions_skipped', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Index('ix_sync_history_connection_id', 'connection_id'),
)


def _rebuild_tables():
    # budgets: swap the anonymous UNIQUE table constraint for the named index the
    # model declares, drop the server default, make the PK explicitly NOT NULL.
    with op.batch_alter_table('budgets', copy_from=_OLD_BUDGETS) as batch:
        batch.alter_column('id', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('account_name', existing_type=sa.String(50),
                           nullable=False, server_default=None)

    # log_entry: drop server defaults the model does not declare.
    with op.batch_alter_table('log_entry', copy_from=_OLD_LOG_ENTRY) as batch:
        for column in ('starting_balance', 'pending_total', 'cleared_balance',
                       'available_balance'):
            batch.alter_column(column, existing_type=sa.Float(), nullable=False,
                               server_default=None)

    # transactions: REAL -> FLOAT on anomaly_score, and the missing foreign key.
    with op.batch_alter_table('transactions', copy_from=_OLD_TRANSACTIONS) as batch:
        batch.alter_column('id', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('anomaly_score', existing_type=sa.REAL(),
                           type_=sa.Float(), nullable=True)
        batch.create_foreign_key('fk_transactions_account_id',
                                 'financial_accounts', ['account_id'], ['id'])

    # holdings: the table the bootstrap created by hand, brought in line with the
    # model -- foreign key, index, no stray server defaults.
    with op.batch_alter_table('holdings', copy_from=_OLD_HOLDINGS) as batch:
        batch.alter_column('id', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('shares', existing_type=sa.Numeric(14, 6),
                           nullable=False, server_default=None)
        batch.alter_column('asset_class', existing_type=sa.String(20),
                           nullable=False, server_default=None)
        batch.alter_column('account_name', existing_type=sa.String(50),
                           nullable=False, server_default=None)
        batch.create_foreign_key('fk_holdings_account_id', 'financial_accounts',
                                 ['account_id'], ['id'])

    # The two tables left pointing at connected_accounts_old. copy_from omits the
    # foreign key entirely, so the rebuild drops it, and create_foreign_key adds
    # the correct one back.
    with op.batch_alter_table('financial_accounts',
                              copy_from=_OLD_FINANCIAL_ACCOUNTS) as batch:
        batch.create_foreign_key('fk_financial_accounts_connection_id',
                                 'connected_accounts', ['connection_id'], ['id'])

    with op.batch_alter_table('sync_history', copy_from=_OLD_SYNC_HISTORY) as batch:
        batch.create_foreign_key('fk_sync_history_connection_id',
                                 'connected_accounts', ['connection_id'], ['id'])


def upgrade():
    inspector = sa.inspect(op.get_bind())
    _create_missing_tables(set(inspector.get_table_names()))

    # Re-inspect between stages: stage 1 may have created tables that stages 2
    # and 3 need to see.
    _add_missing_columns(sa.inspect(op.get_bind()))
    _create_missing_indexes(sa.inspect(op.get_bind()))
    _rebuild_tables()


def downgrade():
    """Intentionally a no-op.

    This revision's whole purpose is to collapse several divergent schemas onto
    one. There is no single prior state to return to -- "before" is either a
    database with ten missing tables or one with dangling foreign keys, and the
    revision cannot know which it came from. Reversing it would also mean
    deliberately reintroducing the connected_accounts_old references, which is
    not a state any database should be put back into.

    Recovery from a bad upgrade is restoring the backup that
    `tools/backup_db.py` takes first, not `flask db downgrade`. Recorded in
    ADR-0007 so this is not mistaken for an oversight.
    """
