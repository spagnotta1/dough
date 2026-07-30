"""Add households, and give every tenant-scoped row one.

Revision ID: 20260726_02_multitenancy
Revises: 20260726_01_reconcile
Create Date: 2026-07-26

Background
----------
Until this revision the application had exactly one owner and the schema said so
by saying nothing: no row recorded whose money it described, and six unique
constraints were global when what they actually meant was "unique per family".

This revision introduces `households` -- the tenant -- and adds a non-nullable
`household_id` to the thirteen tables that hold somebody's private financial
data. `app_users` gets one too, as a plain foreign key rather than a scoped
column, because login has to find a user *before* a household is known.

`market_prices` is deliberately left alone. The closing price of VTI is not
private to anyone; see the note in models.py.

Why this could not have been the previous revision
--------------------------------------------------
It depends on `20260726_01_reconcile` having converged every database in
existence onto one schema. This revision rebuilds fourteen tables through
`batch_alter_table`, and a batch rebuild reproduces whatever the source table
actually is -- so running it on top of the old drift would have baked the drift
in permanently, including the 27 dangling foreign keys reconcile removed.

Design
------
Three ordered stages. The order is the whole safety argument: a column cannot be
`NOT NULL` before it has values, and it cannot be given values before it exists.

    1. create `households`, and the one row every existing table backfills to
    2. add `household_id` nullable everywhere, and backfill it
    3. rebuild each table: NOT NULL, foreign key, and the corrected indexes

Stage 3 uses `batch_alter_table(copy_from=...)` with explicitly declared frozen
`sa.Table` objects, per ADR-0007. Two things about those definitions are load
bearing and easy to misread:

- **They describe the state at the *start* of stage 3** -- that is, the schema
  reconcile produced, plus the nullable `household_id` stage 2 just added. Every
  change from there is written as an explicit batch operation below, so the diff
  is readable rather than implied.
- **Indexes survive only if declared here.** Alembic's batch rebuild is
  create-temp / copy / drop / rename; an index that is not in `copy_from` and
  not recreated afterwards is silently gone. Reconcile lost ten indexes this way
  on its first validation run.

There is one deliberate exception to the first rule. `account_balance.account_type`,
`portfolio_snapshots.snapshot_date` and `recurring_dismissals.desc_key` each
carry a column-level `UNIQUE` today, which SQLite implements as an autoindex
*inside* the CREATE TABLE and which therefore has no name to drop. Those three
columns are declared below **without** `unique=True`, and the rebuild is what
removes the constraint. The replacement composite index is created explicitly in
the same batch block, so the two halves of the swap are next to each other.

Left globally unique on purpose:

- `app_users.username` -- one namespace for logins across the installation.
  Two households cannot both have an `alice`, which is the correct trade for an
  app where the username is the login identifier.
- `idx_finacct_unique(connection_id, external_id)` and
  `idx_holding_sync_unique(account_id, ticker)` -- both lead with a column that
  already resolves to exactly one household, so adding `household_id` would
  constrain nothing that is not already constrained.
- `idx_transaction_external_unique(account_id, external_id)` -- same reasoning.

The default household
---------------------
Every pre-existing row belongs to household 1. It is created here only if there
is something to put in it: an existing `app_users` row, or any tenant-scoped
data. A genuinely empty database gets no household, because `/setup` creates the
household and its first owner together -- and a household with no owner would
violate an invariant `tools/verify_tenancy.py` checks.

Unlike `20260726_01_reconcile`, **this revision's `downgrade()` is implemented**
and round-trip tested (`tests/test_migrations.py`). It can afford to be: every
change here is additive, so the inverse loses only the tenant assignment itself,
which on a single-household database is no information at all. On a database
that has grown a second household the downgrade would merge two families' money
into one view, so it refuses to run in that case rather than doing it.
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = '20260726_02_multitenancy'
down_revision = '20260726_01_reconcile'
branch_labels = None
depends_on = None


#: The household every pre-existing row is backfilled to. Must agree with
#: `config.BaseConfig.DEFAULT_HOUSEHOLD_ID`; asserted by
#: `tests/test_migrations.py::test_default_household_id_matches_config`.
#: Hardcoded rather than imported because a migration that reads live
#: configuration means a different thing on every machine that runs it.
DEFAULT_HOUSEHOLD_ID = 1

#: Tables that get a scoped, non-nullable `household_id`. `app_users` is handled
#: separately -- it is identity, not tenant data.
SCOPED_TABLES = (
    'account_balance', 'budgets', 'chat_messages', 'connected_accounts',
    'conversations', 'financial_accounts', 'holdings', 'log_entry',
    'portfolio_snapshots', 'recurring_dismissals', 'sync_errors',
    'sync_history', 'transactions',
)


# ═══════════════════════════════════════════════════════════════════════════
# Frozen table definitions -- the schema as it stands at the start of stage 3.
#
# A snapshot, not a view of models.py. models.py will keep changing; what this
# revision rebuilds must not. Never import the models here.
# ═══════════════════════════════════════════════════════════════════════════

_pre = sa.MetaData()


def _hh():
    """A fresh nullable household_id column.

    A function, not a shared constant: a `sa.Column` instance belongs to exactly
    one Table, and reusing one across fourteen of them attaches it to the first
    and silently omits it from the rest.
    """
    return sa.Column('household_id', sa.Integer(), nullable=True)


_account_balance = sa.Table(
    'account_balance', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    # No unique=True: see the note on the three column-level UNIQUEs above.
    sa.Column('account_type', sa.String(length=50), nullable=False),
    sa.Column('starting_balance', sa.Float(), nullable=False),
    sa.Column('last_updated', sa.DateTime(), nullable=True),
    _hh(),
)

_budgets = sa.Table(
    'budgets', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('account_name', sa.String(length=50), nullable=False),
    sa.Column('monthly_limit', sa.Numeric(precision=10, scale=2), nullable=False),
    _hh(),
    sa.Index('idx_budget_unique', 'category', 'account_name', unique=True),
)

_chat_messages = sa.Table(
    'chat_messages', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    _hh(),
    sa.Index('ix_chat_messages_session_id', 'session_id'),
)

_connected_accounts = sa.Table(
    'connected_accounts', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('institution', sa.String(length=40), nullable=False),
    sa.Column('item_id', sa.String(length=80), nullable=True),
    sa.Column('display_name', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('auth_blob', sa.Text(), nullable=True),
    sa.Column('token_expires_at', sa.DateTime(), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(), nullable=True),
    sa.Column('last_sync_status', sa.String(length=20), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    _hh(),
    sa.Index('uq_institution_item', 'institution', 'item_id', unique=True),
)

_conversations = sa.Table(
    'conversations', _pre,
    sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
    sa.Column('title', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    _hh(),
)

_financial_accounts = sa.Table(
    'financial_accounts', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('connection_id', sa.Integer(),
              sa.ForeignKey('connected_accounts.id'), nullable=False),
    sa.Column('external_id', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('account_type', sa.String(length=20), nullable=False),
    sa.Column('currency', sa.String(length=10), nullable=False),
    sa.Column('mask', sa.String(length=10), nullable=True),
    sa.Column('balance', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('available_balance', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    _hh(),
    sa.Index('ix_financial_accounts_connection_id', 'connection_id'),
    sa.Index('idx_finacct_unique', 'connection_id', 'external_id', unique=True),
)

_holdings = sa.Table(
    'holdings', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('ticker', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('shares', sa.Numeric(precision=14, scale=6), nullable=False),
    sa.Column('current_value', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('asset_class', sa.String(length=20), nullable=False),
    sa.Column('account_name', sa.String(length=50), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('source', sa.String(length=10), nullable=False, server_default='manual'),
    sa.Column('account_id', sa.Integer(),
              sa.ForeignKey('financial_accounts.id'), nullable=True),
    sa.Column('external_id', sa.String(length=120), nullable=True),
    sa.Column('avg_cost', sa.Numeric(precision=14, scale=4), nullable=True),
    sa.Column('current_price', sa.Numeric(precision=14, scale=4), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    _hh(),
    sa.Index('ix_holdings_account_id', 'account_id'),
    sa.Index('idx_holding_sync_unique', 'account_id', 'ticker', unique=True),
)

_log_entry = sa.Table(
    'log_entry', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('account_type', sa.String(length=50), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('cleared', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('starting_balance', sa.Float(), nullable=False),
    sa.Column('pending_total', sa.Float(), nullable=False),
    sa.Column('cleared_balance', sa.Float(), nullable=False),
    sa.Column('available_balance', sa.Float(), nullable=False),
    _hh(),
)

_portfolio_snapshots = sa.Table(
    'portfolio_snapshots', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    # No unique=True: see the note on the three column-level UNIQUEs above.
    sa.Column('snapshot_date', sa.Date(), nullable=False),
    sa.Column('checking', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('savings', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total_cash', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('brokerage', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('crypto', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total_investments', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('net_worth', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    _hh(),
)

_recurring_dismissals = sa.Table(
    'recurring_dismissals', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    # No unique=True: see the note on the three column-level UNIQUEs above.
    sa.Column('desc_key', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    _hh(),
)

_sync_errors = sa.Table(
    'sync_errors', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('run_id', sa.Integer(), sa.ForeignKey('sync_history.id'), nullable=True),
    sa.Column('connection_id', sa.Integer(), nullable=True),
    sa.Column('institution', sa.String(length=40), nullable=True),
    sa.Column('error_type', sa.String(length=40), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('is_transient', sa.Boolean(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    _hh(),
    sa.Index('ix_sync_errors_run_id', 'run_id'),
)

_sync_history = sa.Table(
    'sync_history', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('connection_id', sa.Integer(),
              sa.ForeignKey('connected_accounts.id'), nullable=True),
    sa.Column('institution', sa.String(length=40), nullable=True),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('accounts_synced', sa.Integer(), nullable=False),
    sa.Column('balances_updated', sa.Integer(), nullable=False),
    sa.Column('holdings_synced', sa.Integer(), nullable=False),
    sa.Column('transactions_added', sa.Integer(), nullable=False),
    sa.Column('transactions_skipped', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    _hh(),
    sa.Index('ix_sync_history_connection_id', 'connection_id'),
)

_transactions = sa.Table(
    'transactions', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('account_name', sa.String(length=50), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.Column('anomaly_score', sa.Float(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('import_batch_id', sa.String(length=36), nullable=True),
    sa.Column('anomaly_reviewed', sa.Boolean(), nullable=False, server_default='0'),
    sa.Column('source', sa.String(length=10), nullable=False, server_default='csv'),
    sa.Column('account_id', sa.Integer(),
              sa.ForeignKey('financial_accounts.id'), nullable=True),
    sa.Column('external_id', sa.String(length=120), nullable=True),
    _hh(),
    sa.Index('ix_transactions_account_id', 'account_id'),
    sa.Index('idx_transaction_unique',
             'account_name', 'date', 'description', 'amount', unique=True),
    sa.Index('ix_transactions_import_batch_id', 'import_batch_id'),
    sa.Index('idx_transaction_external_unique', 'account_id', 'external_id',
             unique=True),
)

_app_users = sa.Table(
    'app_users', _pre,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('username', sa.String(length=80), nullable=False, unique=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    _hh(),
    sa.Column('role', sa.String(length=20), nullable=False, server_default='owner'),
)

_FROZEN = {t.name: t for t in _pre.tables.values()}


# ═══════════════════════════════════════════════════════════════════════════
# Upgrade
# ═══════════════════════════════════════════════════════════════════════════

def _needs_a_default_household(bind) -> bool:
    """Whether there is any pre-existing data that has to belong to somebody.

    An empty database gets no household: `/setup` creates one together with its
    first owner, and a household with no owner is a state
    `tools/verify_tenancy.py` reports as a failure.
    """
    if bind.execute(sa.text('SELECT COUNT(*) FROM app_users')).scalar():
        return True
    for table in SCOPED_TABLES:
        if bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar():
            return True
    return False


def upgrade():
    bind = op.get_bind()

    # -- stage 1: the tenant registry ---------------------------------------
    op.create_table(
        'households',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('plaid_user_id', sa.String(length=80), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )

    if _needs_a_default_household(bind):
        # Named after the existing owner when there is one, because "Household 1"
        # is what an operator sees in a support conversation and it tells them
        # nothing. Falls back for a database that predates the auth phase.
        owner = bind.execute(sa.text(
            'SELECT username FROM app_users ORDER BY id LIMIT 1')).scalar()
        name = f"{owner}'s household" if owner else 'My household'
        bind.execute(
            sa.text('INSERT INTO households (id, name, plaid_user_id, '
                    'created_at, updated_at) '
                    'VALUES (:id, :name, NULL, :now, :now)'),
            {'id': DEFAULT_HOUSEHOLD_ID, 'name': name,
             # datetime.utcnow(), not CURRENT_TIMESTAMP: SQLAlchemy's DateTime
             # round-trips Python datetimes through SQLite as ISO strings with a
             # 'T'-less space separator, and a SQL-side default writes a subtly
             # different literal that then fails to parse on read.
             'now': datetime.utcnow()})

    # -- stage 2: the column, nullable, and the backfill --------------------
    # Two plain ALTERs per table. Cheap: SQLite's ADD COLUMN does not rewrite
    # the table, so 1,192 transactions cost nothing here. The expensive rebuild
    # happens once, in stage 3.
    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column('household_id', sa.Integer(), nullable=True))
        op.execute(sa.text(
            f'UPDATE "{table}" SET household_id = {DEFAULT_HOUSEHOLD_ID}'))

    op.add_column('app_users', sa.Column('household_id', sa.Integer(), nullable=True))
    op.add_column('app_users', sa.Column(
        'role', sa.String(length=20), nullable=False, server_default='owner'))
    op.execute(sa.text(
        f'UPDATE app_users SET household_id = {DEFAULT_HOUSEHOLD_ID}'))

    # -- stage 3: NOT NULL, foreign keys, corrected uniqueness --------------
    # Every table here is rebuilt exactly once.

    with op.batch_alter_table('account_balance', copy_from=_account_balance) as batch:
        _require_household(batch, 'account_balance')
        # Replaces the column-level UNIQUE(account_type) the frozen definition
        # above deliberately omits. Left global, exactly one household in the
        # installation could record a manual checking balance.
        batch.create_index('idx_account_balance_unique',
                           ['household_id', 'account_type'], unique=True)

    with op.batch_alter_table('budgets', copy_from=_budgets) as batch:
        _require_household(batch, 'budgets')
        batch.drop_index('idx_budget_unique')
        batch.create_index('idx_budget_unique',
                           ['household_id', 'category', 'account_name'], unique=True)

    with op.batch_alter_table('chat_messages', copy_from=_chat_messages) as batch:
        _require_household(batch, 'chat_messages')

    with op.batch_alter_table('connected_accounts',
                              copy_from=_connected_accounts) as batch:
        _require_household(batch, 'connected_accounts')
        # Without household_id the second family to link Chase collides with the
        # first, and the connect flow fails with an IntegrityError that reads
        # like a Plaid problem.
        batch.drop_index('uq_institution_item')
        batch.create_index('uq_institution_item',
                           ['household_id', 'institution', 'item_id'], unique=True)

    with op.batch_alter_table('conversations', copy_from=_conversations) as batch:
        _require_household(batch, 'conversations')

    with op.batch_alter_table('financial_accounts',
                              copy_from=_financial_accounts) as batch:
        _require_household(batch, 'financial_accounts')

    with op.batch_alter_table('holdings', copy_from=_holdings) as batch:
        _require_household(batch, 'holdings')

    with op.batch_alter_table('log_entry', copy_from=_log_entry) as batch:
        _require_household(batch, 'log_entry')

    with op.batch_alter_table('portfolio_snapshots',
                              copy_from=_portfolio_snapshots) as batch:
        _require_household(batch, 'portfolio_snapshots')
        # Replaces the column-level UNIQUE(snapshot_date). Left global, the
        # first household to sync each morning is the only one that gets a
        # snapshot, and every other net-worth chart quietly stops advancing.
        batch.create_index('idx_portfolio_snapshot_unique',
                           ['household_id', 'snapshot_date'], unique=True)

    with op.batch_alter_table('recurring_dismissals',
                              copy_from=_recurring_dismissals) as batch:
        _require_household(batch, 'recurring_dismissals')
        # Replaces the column-level UNIQUE(desc_key). Left global, one household
        # dismissing "NETFLIX" makes it undismissable for every other.
        batch.create_index('idx_recurring_dismissal_unique',
                           ['household_id', 'desc_key'], unique=True)

    with op.batch_alter_table('sync_errors', copy_from=_sync_errors) as batch:
        _require_household(batch, 'sync_errors')

    with op.batch_alter_table('sync_history', copy_from=_sync_history) as batch:
        _require_household(batch, 'sync_history')

    with op.batch_alter_table('transactions', copy_from=_transactions) as batch:
        _require_household(batch, 'transactions')
        # The CSV dedupe index. repository.py imports by catching IntegrityError
        # on it inside begin_nested(), so leaving it global means the second
        # family to import a $15.99 Netflix charge on the same date silently
        # loses the row -- data loss that looks like the dedupe feature working.
        batch.drop_index('idx_transaction_unique')
        batch.create_index('idx_transaction_unique',
                           ['household_id', 'account_name', 'date', 'description',
                            'amount'], unique=True)

    with op.batch_alter_table('app_users', copy_from=_app_users) as batch:
        _require_household(batch, 'app_users')


def _require_household(batch, table):
    """The three operations every scoped table needs, in one place.

    Index name matches what `Column(index=True)` generates
    (`ix_<table>_household_id`), so the chain and `db.create_all()` produce the
    same schema -- which `tests/test_migrations.py::
    test_chain_from_empty_matches_create_all` compares directly.
    """
    batch.alter_column('household_id', existing_type=sa.Integer(), nullable=False)
    batch.create_foreign_key(f'fk_{table}_household_id', 'households',
                             ['household_id'], ['id'])
    batch.create_index(f'ix_{table}_household_id', ['household_id'])


# ═══════════════════════════════════════════════════════════════════════════
# Downgrade
#
# Written out table by table rather than as a loop over SCOPED_TABLES. A loop
# would read as the tidier inverse, but six of the fourteen tables need their
# uniqueness swapped back as well as the column dropped, and three of those need
# a *column-level* UNIQUE restored -- so the loop would have needed a lookup
# table of exceptions covering nearly half its iterations. Spelled out, each
# block is diffable against its counterpart in upgrade() directly above.
# ═══════════════════════════════════════════════════════════════════════════

#: The seven tables whose only change was gaining the column.
_COLUMN_ONLY_TABLES = (
    'chat_messages', 'conversations', 'financial_accounts', 'holdings',
    'log_entry', 'sync_errors', 'sync_history',
)


def downgrade():
    """Remove tenancy, refusing rather than merging two households into one.

    Implemented, unlike `20260726_01_reconcile.downgrade()`, because everything
    this revision does is additive: on a single-household database the inverse
    discards only the assignment of every row to the one household there is,
    which is no information at all.

    On a database with more than one household it is not additive any more --
    dropping the column would present two families' transactions as one ledger
    with no way to tell them apart afterwards, and would then fail anyway on the
    first duplicate the restored global unique indexes reject. There is no safe
    automatic answer to that, so it raises and says what the operator has to
    decide.
    """
    bind = op.get_bind()
    households = bind.execute(sa.text('SELECT COUNT(*) FROM households')).scalar()
    if households > 1:
        raise RuntimeError(
            f'Refusing to downgrade: this database has {households} households. '
            'Dropping household_id would merge their transactions, budgets and '
            'holdings into one indistinguishable ledger. Export or delete the '
            'households you do not want first, then run this again.')

    for table in _COLUMN_ONLY_TABLES:
        with op.batch_alter_table(table, copy_from=_post_table(table)) as batch:
            batch.drop_index(f'ix_{table}_household_id')
            batch.drop_column('household_id')

    # -- the three that had a column-level UNIQUE ---------------------------
    # Restored as a *named* unique constraint rather than the anonymous
    # autoindex SQLite originally produced. Same guarantee, different name, and
    # tools/schema_report.py compares uniqueness by the columns it enforces
    # precisely so that this distinction does not read as drift.

    with op.batch_alter_table('account_balance',
                              copy_from=_post_table('account_balance')) as batch:
        batch.drop_index('idx_account_balance_unique')
        batch.drop_index('ix_account_balance_household_id')
        batch.drop_column('household_id')
        batch.create_unique_constraint('uq_account_balance_account_type',
                                       ['account_type'])

    with op.batch_alter_table('portfolio_snapshots',
                              copy_from=_post_table('portfolio_snapshots')) as batch:
        batch.drop_index('idx_portfolio_snapshot_unique')
        batch.drop_index('ix_portfolio_snapshots_household_id')
        batch.drop_column('household_id')
        batch.create_unique_constraint('uq_portfolio_snapshot_date',
                                       ['snapshot_date'])

    with op.batch_alter_table('recurring_dismissals',
                              copy_from=_post_table('recurring_dismissals')) as batch:
        batch.drop_index('idx_recurring_dismissal_unique')
        batch.drop_index('ix_recurring_dismissals_household_id')
        batch.drop_column('household_id')
        batch.create_unique_constraint('uq_recurring_dismissal_desc_key',
                                       ['desc_key'])

    # -- the three whose named unique index loses its leading column --------

    with op.batch_alter_table('budgets', copy_from=_post_table('budgets')) as batch:
        batch.drop_index('idx_budget_unique')
        batch.drop_index('ix_budgets_household_id')
        batch.drop_column('household_id')
        batch.create_index('idx_budget_unique', ['category', 'account_name'],
                           unique=True)

    with op.batch_alter_table('connected_accounts',
                              copy_from=_post_table('connected_accounts')) as batch:
        batch.drop_index('uq_institution_item')
        batch.drop_index('ix_connected_accounts_household_id')
        batch.drop_column('household_id')
        batch.create_index('uq_institution_item', ['institution', 'item_id'],
                           unique=True)

    with op.batch_alter_table('transactions',
                              copy_from=_post_table('transactions')) as batch:
        batch.drop_index('idx_transaction_unique')
        batch.drop_index('ix_transactions_household_id')
        batch.drop_column('household_id')
        batch.create_index('idx_transaction_unique',
                           ['account_name', 'date', 'description', 'amount'],
                           unique=True)

    # -- identity -----------------------------------------------------------

    with op.batch_alter_table('app_users',
                              copy_from=_post_table('app_users')) as batch:
        batch.drop_index('ix_app_users_household_id')
        batch.drop_column('household_id')
        batch.drop_column('role')

    op.drop_table('households')


# ═══════════════════════════════════════════════════════════════════════════
# The post-upgrade shapes, for the rebuild back
# ═══════════════════════════════════════════════════════════════════════════

_post = sa.MetaData()

#: Unique indexes `upgrade()` replaced with a household-composite version:
#: the table each lives on, and the columns it carries afterwards.
_REPLACED_INDEXES = {
    'idx_account_balance_unique': (
        'account_balance', ('household_id', 'account_type')),
    'idx_budget_unique': (
        'budgets', ('household_id', 'category', 'account_name')),
    'uq_institution_item': (
        'connected_accounts', ('household_id', 'institution', 'item_id')),
    'idx_portfolio_snapshot_unique': (
        'portfolio_snapshots', ('household_id', 'snapshot_date')),
    'idx_recurring_dismissal_unique': (
        'recurring_dismissals', ('household_id', 'desc_key')),
    'idx_transaction_unique': (
        'transactions', ('household_id', 'account_name', 'date', 'description',
                         'amount')),
}


def _post_table(name):
    """The frozen definition as it stands *after* `upgrade()`.

    Derived from the pre-upgrade definition by applying exactly the changes
    `upgrade()` makes, rather than written out a second time. Fourteen table
    definitions maintained twice is fourteen chances to have one disagree with
    the other -- and a `copy_from` that disagrees with the real table does not
    fail loudly. It rebuilds the table into the wrong shape and copies the data
    in anyway.

    Memoised through `_post` because a `sa.Table` may be defined only once per
    MetaData, and the round-trip test asks for several of these twice.
    """
    if name in _post.tables:
        return _post.tables[name]

    source = _FROZEN[name]

    columns = []
    for col in source.columns:
        if col.name == 'household_id':
            foreign_keys = [sa.ForeignKey('households.id')]
        elif col.foreign_keys:
            foreign_keys = [sa.ForeignKey(
                next(iter(col.foreign_keys)).target_fullname)]
        else:
            foreign_keys = []
        columns.append(sa.Column(
            col.name, col.type, *foreign_keys,
            primary_key=col.primary_key,
            nullable=False if col.name == 'household_id' else col.nullable,
            unique=col.unique,
            # `.arg` rather than the DefaultClause itself: a DefaultClause is
            # bound to the column that created it, and handing the same instance
            # to a second column raises.
            server_default=(col.server_default.arg
                            if col.server_default is not None else None)))

    indexes = [sa.Index(idx.name, *[c.name for c in idx.columns], unique=idx.unique)
               for idx in source.indexes
               if idx.name not in _REPLACED_INDEXES]
    indexes += [sa.Index(index_name, *columns_after, unique=True)
                for index_name, (table, columns_after) in _REPLACED_INDEXES.items()
                if table == name]
    indexes.append(sa.Index(f'ix_{name}_household_id', 'household_id'))

    return sa.Table(name, _post, *columns, *indexes)
