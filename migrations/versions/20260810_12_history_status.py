"""`connected_accounts.history_status`: how much of the backfill has landed.

Revision ID: 20260810_12_histstat
Revises: 20260806_11_rulesrc
Create Date: 2026-08-10

Background
----------
UAT round 1: two testers linked the same institution through Plaid. One got two
years of transactions, the other got one month. Nothing was broken at either
end — Plaid keeps backfilling an Item for minutes to hours after Link closes,
and whichever tester's first sync happened to run before that finished saw only
the initial ~30-day pull.

The schema had no way to say so. `status` was `connected` and
`last_sync_status` was `success` for both, because both were true: the
connection works and the sync worked. "The sync worked and the data is not all
here yet" was simply not expressible, so the app could not act on it and could
not tell the user about it either.

`history_status` is that missing state. `importing` while the provider is still
backfilling, `complete` once the requested window has arrived, `partial` if we
stopped waiting without ever confirming it.

Why nullable, and why no backfill
---------------------------------
NULL means "no claim made", and that is the honest value for every row that
exists when this runs. We do not know how much history those Items hold: the
truncated tester's connection and the complete one are indistinguishable in the
database, which is the whole problem. Writing `complete` across the table would
assert the thing we cannot check, and hide exactly the connections most likely
to be short.

They resolve themselves rather than staying NULL forever: the backfill watcher
adopts a Plaid connection with no history status on its next pass, marks it
`importing`, and lets the normal machinery answer the question.

The column is deliberately not an enum. SQLite has no native enum, and the set
is likely to grow (a `blocked` state for an Item stuck in re-auth is an obvious
next one); a CHECK constraint would make that a table rebuild.

Downgrade
---------
Drops the column. Nothing else reads it, and the connections it describes are
unaffected — the app returns to being unable to tell a full history from a
truncated one, which is where it was before.
"""

import sqlalchemy as sa
from alembic import op

revision = '20260810_12_histstat'
down_revision = '20260806_11_rulesrc'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('connected_accounts') as batch:
        batch.add_column(sa.Column('history_status', sa.String(12), nullable=True))


def downgrade():
    with op.batch_alter_table('connected_accounts') as batch:
        batch.drop_column('history_status')
