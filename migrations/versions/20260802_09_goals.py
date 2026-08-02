"""Add the goals and goal_contributions tables.

Revision ID: 20260802_09_goals
Revises: 20260802_08_category_rules
Create Date: 2026-08-02

Background
----------
Phase 11B. Everything in 11A was *derived* — a trend, a health score and an
anomaly are all opinions about transactions, recomputable from the ledger and
owning no state, which is why that phase needed no schema change at all.

A goal is the opposite. It is a statement of intent that no amount of
transaction history can infer: nobody's spending reveals whether they are saving
for a wedding or a deposit, and guessing would be exactly the fabrication the
rest of Phase 11 exists to prevent. So it has to be stored, and this is the
migration that stores it.

Design
------
**Two tables, not one.** `goals` holds the target and the running total;
`goal_contributions` holds the deposits behind that total. A single column would
answer "how much have I saved" and neither of the two questions that make goal
tracking worth having — "how much did I put aside last month" and "is my
momentum improving".

**`saved_amount` is stored rather than derived from an account balance.** People
save for several goals in one account, so a balance cannot be divided between
them; deriving it would make every goal show the same number the moment a second
one existed.

**Both tables are tenant-scoped**, with `household_id` NOT NULL, a foreign key,
a standalone index (which `TenantScopedMixin` declares and
`tools/verify_tenancy.py` requires — see 20260802_08, where its absence was
caught), and a unique index on goal name that leads with the household.

**Additive.** `CREATE TABLE` only; no existing table is touched or rebuilt, and
there is nothing to backfill — a household with no goals correctly has no rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '20260802_09_goals'
down_revision = '20260802_08_category_rules'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'goals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False,
                  server_default='custom'),
        sa.Column('target_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('saved_amount', sa.Numeric(12, 2), nullable=False,
                  server_default='0'),
        sa.Column('target_date', sa.Date(), nullable=True),
        sa.Column('monthly_target', sa.Numeric(12, 2), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='active'),
        sa.Column('note', sa.String(length=280), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('achieved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_goals_household_id'), 'goals', ['household_id'],
                    unique=False)
    op.create_index('idx_goal_unique_name', 'goals', ['household_id', 'name'],
                    unique=True)
    op.create_index('idx_goal_status', 'goals', ['household_id', 'status'],
                    unique=False)

    op.create_table(
        'goal_contributions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('goal_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('occurred_on', sa.Date(), nullable=False),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.ForeignKeyConstraint(['goal_id'], ['goals.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_goal_contributions_household_id'),
                    'goal_contributions', ['household_id'], unique=False)
    op.create_index(op.f('ix_goal_contributions_goal_id'),
                    'goal_contributions', ['goal_id'], unique=False)
    op.create_index('idx_goal_contribution_when', 'goal_contributions',
                    ['household_id', 'goal_id', 'occurred_on'], unique=False)


def downgrade():
    op.drop_index('idx_goal_contribution_when', table_name='goal_contributions')
    op.drop_index(op.f('ix_goal_contributions_goal_id'),
                  table_name='goal_contributions')
    op.drop_index(op.f('ix_goal_contributions_household_id'),
                  table_name='goal_contributions')
    op.drop_table('goal_contributions')

    op.drop_index('idx_goal_status', table_name='goals')
    op.drop_index('idx_goal_unique_name', table_name='goals')
    op.drop_index(op.f('ix_goals_household_id'), table_name='goals')
    op.drop_table('goals')
