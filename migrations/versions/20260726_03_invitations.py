"""Add household_invites — the table that lets a household have a second person.

Revision ID: 20260726_03_invitations
Revises: 20260726_02_multitenancy
Create Date: 2026-07-27

Background
----------
`20260726_02_multitenancy` made a household the unit of isolation but left it
with exactly one way to gain a member: `/setup`, which only runs once, on an
empty installation. Phase 6 adds invitations, and this is the only schema they
need.

Design
------
A pure `create_table`. No `batch_alter_table`, no rebuilds, nothing touched that
already holds data -- which is why this revision is short and why it does not
carry the row-count argument `02` had to. The risk profile of "add one empty
table" is not the risk profile of "rebuild fourteen populated ones", and
pretending otherwise by wrapping it in the same ceremony would make the ceremony
look routine.

Two columns are worth reading twice:

- `token_hash` is unique across the *installation*, not per household. The
  redemption lookup happens before any household is known -- whoever follows an
  invitation link is anonymous -- so a per-household constraint could not be
  enforced at the moment it matters, and two households issuing colliding links
  has to be impossible rather than merely unlikely.

- `household_id` is `NOT NULL` with a foreign key and an index, like every other
  tenant-scoped table. `tools/verify_tenancy.py` checks all three by name, so a
  column added here that skipped one would be reported rather than assumed.

`created_by_id` and `accepted_by_id` both point at `app_users`, so the
constraints are named explicitly. SQLite would accept them unnamed, and a later
`batch_alter_table` on this table would then be unable to reproduce them --
ADR-0007's rule, applied on the way in rather than discovered on the way out.

Downgrade
---------
Drops the table. Unlike `02` this loses nothing that cannot be recreated: a
pending invitation is a link somebody can issue again, and an accepted one has
already done its work -- the member it created is a row in `app_users` and is
not touched here.
"""

import sqlalchemy as sa
from alembic import op

revision = '20260726_03_invitations'
down_revision = '20260726_02_multitenancy'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'household_invites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False,
                  server_default='member'),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.Column('accepted_by_id', sa.Integer(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_household_invites'),
        sa.ForeignKeyConstraint(['household_id'], ['households.id'],
                                name='fk_household_invites_household_id'),
        sa.ForeignKeyConstraint(['created_by_id'], ['app_users.id'],
                                name='fk_household_invites_created_by_id'),
        sa.ForeignKeyConstraint(['accepted_by_id'], ['app_users.id'],
                                name='fk_household_invites_accepted_by_id'),
    )
    op.create_index('ix_household_invites_token_hash', 'household_invites',
                    ['token_hash'], unique=True)
    op.create_index('ix_household_invites_household_id', 'household_invites',
                    ['household_id'], unique=False)


def downgrade():
    op.drop_index('ix_household_invites_household_id',
                  table_name='household_invites')
    op.drop_index('ix_household_invites_token_hash',
                  table_name='household_invites')
    op.drop_table('household_invites')
