"""Add email identity to app_users and the email_verifications table.

Revision ID: 20260730_07_identity
Revises: 20260730_06_session_version
Create Date: 2026-07-30

Background
----------
Phase 10.5. Until now the only way an account could come into existence was
`/setup` (the first one) or `/join` (an invitation), and neither asked for an
email address because neither needed one: there was nowhere to send anything and
no recovery path to send it for. `/register`, `/forgot-password` and
`/reset-password/<token>` all need one, so the column has to exist before they
can.

Design
------
`app_users.email` is **nullable**, and that is the load-bearing decision in this
revision. Every row that predates it was created without an address and there is
nothing truthful to backfill. The tempting fillers are all worse than NULL:

- A synthetic address (`user7@localhost`) is a *delivery address for a password
  reset*. If it ever became routable — a real `localhost` alias, a domain
  somebody later registers — it is a takeover of every account holding one.
- The username is not an address, and `/forgot-password` would then accept a
  username where it documents an address, which is an enumeration oracle for
  usernames rather than the addresses the flow is careful about.

So `NULL` means exactly what it says: this account has no recovery address.
`/forgot-password` finds nothing for it (indistinguishable, from outside, from an
address that matches no account — which is the whole point of that route's
response), and the account settings page is where its owner adds one.

`UNIQUE` on a nullable column is deliberate and is not a contradiction: SQL
treats NULLs as distinct from one another, so every address-less row coexists
happily while two accounts still cannot share a real address. That matters
because the address is a *lookup key* for reset — two rows sharing one would make
"which account did you mean" unanswerable at the exact moment nobody is signed in
to disambiguate it.

`email_verifications` carries both the verification and the reset token, told
apart by `purpose`. The reasoning for one table rather than two is at the model;
the schema consequence is that `token_hash` is unique installation-wide, like
`household_invites.token_hash` and for the same reason — the lookup runs before
any household is known, so a per-household constraint could not be enforced where
it matters.

Tenancy
-------
Neither object gets a `household_id`, and `tools/verify_tenancy.py` is updated in
the same commit to list `email_verifications` among the deliberately unscoped
tables with its reason attached. Whoever follows a reset link cannot sign in by
definition, so no household can be bound and a scoped query would find nothing.
That is the same exemption `app_users` and `api_tokens` already hold.

Downgrade
---------
Drops the table and both columns. Any address collected through `/register` is
lost, and so is every issued-but-unspent token — which is the correct outcome for
the tokens (an unredeemable reset link is a link that has stopped working, and
that is the safe direction) and a real data loss for the addresses. Take a backup
first; the addresses are not recoverable from anywhere else.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260730_07_identity'
down_revision = '20260730_06_session_version'
branch_labels = None
depends_on = None


def upgrade():
    # `batch_alter_table` per ADR-0007: SQLite cannot ALTER a column, so Alembic
    # rebuilds the table, and a rebuild needs every constraint to have a name it
    # can reproduce. `20260726_02` named them for exactly this.
    #
    # The unique constraint is created *through the batch context* rather than as
    # a separate `op.create_unique_constraint`, so it lands in the same rebuild.
    # Two rebuilds of app_users in one revision is twice the opportunity for the
    # table to be left half-copied if the migration dies in between.
    with op.batch_alter_table('app_users') as batch:
        batch.add_column(sa.Column('email', sa.String(length=255),
                                   nullable=True))
        batch.add_column(sa.Column('email_verified_at', sa.DateTime(),
                                   nullable=True))
        batch.create_unique_constraint('uq_app_users_email', ['email'])

    op.create_table(
        'email_verifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False,
                  server_default='verify_email'),
        sa.Column('sent_to', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        # Named, like every constraint added since 20260726_02, so a later
        # `batch_alter_table` on this table can reproduce it. An unnamed
        # constraint is one that a future SQLite rebuild silently drops.
        sa.ForeignKeyConstraint(['user_id'], ['app_users.id'],
                                name='fk_email_verifications_user_id'),
        sa.PrimaryKeyConstraint('id', name='pk_email_verifications'),
    )
    # Unique *and* indexed: the uniqueness is the guarantee that two tokens
    # cannot collide, and the index is what makes redemption an O(1) digest
    # lookup rather than a scan -- the same shape as `api_tokens.token_hash`.
    op.create_index('ix_email_verifications_token_hash', 'email_verifications',
                    ['token_hash'], unique=True)
    op.create_index('ix_email_verifications_user_id', 'email_verifications',
                    ['user_id'])


def downgrade():
    op.drop_index('ix_email_verifications_user_id',
                  table_name='email_verifications')
    op.drop_index('ix_email_verifications_token_hash',
                  table_name='email_verifications')
    op.drop_table('email_verifications')
    with op.batch_alter_table('app_users') as batch:
        batch.drop_constraint('uq_app_users_email', type_='unique')
        batch.drop_column('email_verified_at')
        batch.drop_column('email')
