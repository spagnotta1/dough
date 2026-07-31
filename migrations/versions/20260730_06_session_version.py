"""Add session_version to app_users and api_tokens.

Revision ID: 20260730_06_session_version
Revises: 20260730_05_api_tokens
Create Date: 2026-07-30

Background
----------
Phase 10.5. After Phase 10 this application had two kinds of credential — a
signed session cookie and an opaque bearer token — and no way to invalidate
either one in response to a change to the account behind it. Nothing revoked
anything when a password changed, which was survivable only because no password
change exists yet; the point of doing it now is that it stops being survivable
the moment one does.

Design
------
A generation counter on the account, and a copy of it on each credential. Raising
`app_users.session_version` invalidates every credential issued under the old
value at once: the session cookie carries the number it was signed in under, and
`api_tokens` stores the number it was issued under, so both comparisons are made
against a row that is already loaded on every request.

Two columns rather than one because there are two credential stores and the
session is not one of ours — it lives in the user's cookie, so there is no table
to add a column to and the value travels in the cookie instead.

The alternative, sweeping `api_tokens` and stamping `revoked_at` on a password
change, was rejected for a reason that is about failure rather than tidiness: a
sweep is a second write that can be lost, so a token could survive an
invalidation by having been missed. A comparison cannot be missed. The cost is
that a superseded token stays in the table looking issued, which `ApiToken.state`
answers by reporting it as `'stale'`.

Both default to 1 rather than 0, and both are NOT NULL. There is no meaningful
"no version": a credential that recorded nothing could not be compared against
anything, and the safe reading of a NULL here would have to be "refuse", which
is a state no existing row should be put into by a migration.

Existing rows therefore keep working. Every `app_users` row gets 1 and every
`api_tokens` row gets 1, so every already-issued token still matches its user.
Existing *browser sessions* do not: their cookies predate the key and
`dough.auth.current_user` refuses a session that does not carry one. That is
deliberate and it is a one-time sign-in after deploying this, not a recurring
cost — the fail-open reading ("no version recorded, so allow") would leave every
session minted before this migration permanently exempt from the mechanism.

Downgrade
---------
Drops both columns, which restores the schema and removes the invalidation. Any
password change made while the new schema was in place stops being enforced,
so credentials it invalidated become usable again — worth knowing before running
it in anger, and the reason a downgrade here is not a no-op the way most
add-column downgrades are.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260730_06_session_version'
down_revision = '20260730_05_api_tokens'
branch_labels = None
depends_on = None


def upgrade():
    # `batch_alter_table` on both, per ADR-0007: SQLite cannot ALTER a column,
    # so Alembic rebuilds the table, and the rebuild needs the constraints to
    # have names it can reproduce. `20260726_02` and `20260730_05` named them
    # for exactly this moment.
    with op.batch_alter_table('app_users') as batch:
        batch.add_column(sa.Column('session_version', sa.Integer(),
                                   nullable=False, server_default='1'))
    with op.batch_alter_table('api_tokens') as batch:
        batch.add_column(sa.Column('session_version', sa.Integer(),
                                   nullable=False, server_default='1'))


def downgrade():
    with op.batch_alter_table('api_tokens') as batch:
        batch.drop_column('session_version')
    with op.batch_alter_table('app_users') as batch:
        batch.drop_column('session_version')
