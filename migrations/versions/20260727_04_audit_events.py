"""Add audit_events — an append-only record of what happened and who did it.

Revision ID: 20260727_04_audit
Revises: 20260726_03_invitations
Create Date: 2026-07-27

Background
----------
Phase 8. Until now the only record that anything happened was `sync_history`
(machine events) and the application log (unstructured, rotated, and gone). Who
invited whom, who was removed, who changed a role, and which sign-ins failed
were nowhere at all — the questions an incident actually starts with.

Design
------
A pure `create_table` plus three indexes. Nothing that already holds data is
touched, so this carries none of the ceremony `20260726_02` needed: the risk
profile of "add one empty table" is not the risk profile of "rebuild fourteen
populated ones", and dressing it in the same runbook would make that runbook
look routine.

`household_id` is NULLABLE, which is the one thing here that deserves argument.
Every other tenant table declares it NOT NULL and the ORM backstop filters on
it. This table cannot, because it has to record failed logins, and a failed
login for a username that does not exist has no user and therefore no household
— there is nothing truthful to write. A sentinel household was rejected for the
same reason `_household_for_request` returns None rather than a wildcard: a fake
tenant that a scoped query could match is worse than no tenant at all.

So NULL means exactly one thing — *no tenant existed when this happened* — and
the isolation the mixin would have provided is replaced by a single read
function in `dough/services/audit.py` that always filters on the caller's
household. `tools/verify_tenancy.py` lists this table as deliberately unscoped
with that reason attached, so the exception is reviewed rather than assumed.

Neither foreign key cascades. An audit row has to outlive the thing it
describes: the record that a member was removed is worthless if removing them
deletes it. That is also why `entity_type`/`entity_id` are a loose pair rather
than a relationship — the row they name may already be gone.

Both constraints are named explicitly. SQLite would accept them unnamed and a
later `batch_alter_table` on this table would then be unable to reproduce them
— ADR-0007's rule applied on the way in rather than discovered on the way out.

Downgrade
---------
Works, and drops the table. Worth being clear about what that means: it deletes
the audit trail, which is the one kind of data that cannot be reconstructed
afterwards. Take a backup first — the same instruction as every other downgrade
here, for a stronger reason.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260727_04_audit'
down_revision = '20260726_03_invitations'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), nullable=False),
        # Nullable on purpose -- see the module docstring.
        sa.Column('household_id', sa.Integer(), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('event_type', sa.String(length=60), nullable=False),
        sa.Column('entity_type', sa.String(length=40), nullable=True),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_audit_events'),
        sa.ForeignKeyConstraint(['household_id'], ['households.id'],
                                name='fk_audit_events_household_id'),
        sa.ForeignKeyConstraint(['actor_user_id'], ['app_users.id'],
                                name='fk_audit_events_actor_user_id'),
    )
    op.create_index('ix_audit_events_household_id', 'audit_events',
                    ['household_id'])
    op.create_index('ix_audit_events_actor_user_id', 'audit_events',
                    ['actor_user_id'])
    op.create_index('ix_audit_events_event_type', 'audit_events',
                    ['event_type'])
    op.create_index('ix_audit_events_created_at', 'audit_events',
                    ['created_at'])
    # The one query this table exists to answer quickly: "what happened in this
    # household, most recent first". A composite index rather than relying on
    # the two single-column ones, because the sort is half the cost.
    op.create_index('ix_audit_events_household_created', 'audit_events',
                    ['household_id', 'created_at'])


def downgrade():
    op.drop_index('ix_audit_events_household_created', table_name='audit_events')
    op.drop_index('ix_audit_events_created_at', table_name='audit_events')
    op.drop_index('ix_audit_events_event_type', table_name='audit_events')
    op.drop_index('ix_audit_events_actor_user_id', table_name='audit_events')
    op.drop_index('ix_audit_events_household_id', table_name='audit_events')
    op.drop_table('audit_events')
