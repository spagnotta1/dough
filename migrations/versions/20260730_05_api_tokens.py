"""Add api_tokens — the credential a non-browser client authenticates with.

Revision ID: 20260730_05_api_tokens
Revises: 20260727_04_audit
Create Date: 2026-07-30

Background
----------
Phase 10. Everything before this authenticated with a signed session cookie and
a CSRF token bound to it. Both are browser mechanisms: a native client holding a
cookie jar and scraping a token out of an HTML page is a browser emulator, not
an API client. `/api/v1` needs a credential a device can hold, and — more to the
point — one that can be withdrawn from a single device without signing the rest
of the household out.

Design
------
A pure `create_table` plus its indexes. Nothing already holding data is touched,
so this carries none of the ceremony `20260726_02` needed; see `20260727_04`
for why that distinction is kept visible rather than dressed up in the same
runbook.

`household_id` is NOT NULL here, unlike `audit_events`. There is no anonymous
case to accommodate: a token that belongs to no household could not be used for
anything, so nullable would only make an impossible state representable.

What it does *not* have is `TenantScopedMixin`, and that is the same exception
`app_users` takes. The lookup runs before any household is bound — the token is
what says which household the request is for — so routing this table through the
ORM backstop would make authenticating impossible in exactly the way scoping
`app_users` would make signing in impossible. The isolation guarantee is
therefore `dough/services/api_tokens.py`, which is the only module that reads
the table and states the household predicate itself every time.
`tools/verify_tenancy.py` records the exception with that reasoning attached.

`token_hash` is globally unique rather than unique per household, for the reason
`household_invites.token_hash` is: the lookup that needs the constraint happens
before a household is known, so a scoped constraint could not be enforced at the
one point it matters. It also means two households can never be issued colliding
credentials, which a per-household constraint would permit.

Both foreign keys are named explicitly. SQLite accepts them unnamed and a later
`batch_alter_table` on this table would then be unable to reproduce them —
ADR-0007's rule applied on the way in rather than discovered on the way out.

Neither cascades, and `user_id` not cascading is the one worth stating. Removing
a member does not silently delete their tokens; it leaves rows whose owner is
gone, which `api_tokens.authenticate` refuses because it re-reads the user on
every request. A cascade would make the credential disappear from the audit
surface at the moment somebody most wants to see it.

Downgrade
---------
Works, and drops the table. Every issued token stops working — which for this
table is the correct and only possible meaning of a downgrade, since the
plaintext was never stored and cannot be reissued.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260730_05_api_tokens'
down_revision = '20260727_04_audit'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=20), nullable=False),
        sa.Column('scopes', sa.String(length=120), nullable=False,
                  server_default='read'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_api_tokens'),
        sa.ForeignKeyConstraint(['household_id'], ['households.id'],
                                name='fk_api_tokens_household_id'),
        sa.ForeignKeyConstraint(['user_id'], ['app_users.id'],
                                name='fk_api_tokens_user_id'),
    )
    # Unique and global -- see the module docstring. This is the index every
    # authenticated API request reads, so it is the one that has to exist.
    #
    # Named `ix_`, not `uq_`, despite being a unique index. The model declares
    # `unique=True, index=True` on the column, and SQLAlchemy's naming
    # convention for that is `ix_<table>_<column>`. `tests/test_migrations.py`
    # compares the schema `create_all()` produces against this chain's, so a
    # nicer name here would be a permanent, unfixable diff between the two.
    op.create_index('ix_api_tokens_token_hash', 'api_tokens', ['token_hash'],
                    unique=True)
    op.create_index('ix_api_tokens_household_id', 'api_tokens', ['household_id'])
    op.create_index('ix_api_tokens_user_id', 'api_tokens', ['user_id'])


def downgrade():
    op.drop_index('ix_api_tokens_user_id', table_name='api_tokens')
    op.drop_index('ix_api_tokens_household_id', table_name='api_tokens')
    op.drop_index('ix_api_tokens_token_hash', table_name='api_tokens')
    op.drop_table('api_tokens')
