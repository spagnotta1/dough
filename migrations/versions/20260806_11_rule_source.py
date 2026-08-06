"""Auto-categorization: `category_rules.source` and the household opt-out.

Revision ID: 20260806_11_rulesrc
Revises: 20260803_10_unseed
Create Date: 2026-08-06

Background
----------
Until now every row in `category_rules` got there the same way: somebody typed
it on the Rules page, or somebody read a suggestion card and pressed Accept.
The table needed no provenance because there was only one provenance.

Auto-categorization breaks that. After a sync brings in new transactions,
Dough analyzes them and writes the rules itself — no card, no click, nobody
necessarily awake. Those rules are indistinguishable from hand-written ones in
the old schema, and that is the problem rather than an inconvenience: a user
scanning their Rules page cannot tell what they agreed to from what was decided
for them, and "undo what Dough did on its own" is not expressible as a query.

`source` is that distinction. `'user'` for typed or accepted, `'ai'` for
written unprompted.

Why the backfill is `'user'` and not NULL
-----------------------------------------
Every row that exists at the moment this runs predates auto-categorization, so
every one of them was written or accepted by a person. `server_default='user'`
states that as a fact about the existing data rather than leaving a nullable
column that every read has to defend against. New rows get their value from the
application; the server default is here for the rows already in the table.

The column stays non-nullable for the same reason: there is no such thing as a
rule that came from nowhere, so there is no third state to represent.

`households.auto_categorize_enabled`
------------------------------------
The off switch, defaulting to on. It exists because deleting the rules Dough
wrote is not an undo by itself: the next sync would derive the same rules from
the same descriptions and put them straight back. An undo the application
silently reverses is worse than no undo, so clearing the auto-written rules
also clears this flag, and the Rules page is where it goes back on.

Existing households backfill to on. They have not been asked, but the feature
is the product's default behaviour and is announced on the Rules page with a
one-click way out — the same position a new household is in.

Downgrade
---------
Drops both columns, which loses the distinction and the preference. That is
acceptable and worth being explicit about: going back means the app no longer
auto-writes rules, so there is nothing left that needs telling apart and no
behaviour left to opt out of. The rules themselves survive; only the label for
who wrote them is lost.
"""

import sqlalchemy as sa
from alembic import op

revision = '20260806_11_rulesrc'
down_revision = '20260803_10_unseed'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table because SQLite cannot ALTER a column into existence
    # with a NOT NULL constraint in place — Alembic rebuilds the table.
    with op.batch_alter_table('category_rules') as batch:
        batch.add_column(sa.Column('source', sa.String(10), nullable=False,
                                   server_default='user'))
    with op.batch_alter_table('households') as batch:
        batch.add_column(sa.Column('auto_categorize_enabled', sa.Boolean(),
                                   nullable=False, server_default='1'))


def downgrade():
    with op.batch_alter_table('households') as batch:
        batch.drop_column('auto_categorize_enabled')
    with op.batch_alter_table('category_rules') as batch:
        batch.drop_column('source')
