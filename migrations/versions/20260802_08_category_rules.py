"""Move category rules out of a shared JSON file into a household-scoped table.

Revision ID: 20260802_08_category_rules
Revises: 20260730_07_identity
Create Date: 2026-08-02

Background
----------
Category rules were stored in `category_rules.json` — a single file at the repo
root, read and written by every household in the installation. `rules.py`,
`dough/services/categorization.py` and the Rules page contained no mention of a
household between them, so this was not a leak through a missing query filter:
**the rules were never tenanted**. The second household to sign in saw the
first's rules because there was only one rule set in existence.

That is a disclosure of personal financial data. A rule set names the merchants
somebody actually pays; the file this migration reads contains
`/planet fitness|gym membership/` and `/DEPT EDUCATION STUDENT LN|IU BURSAR/`,
which tell a stranger where the account holder exercises and who services their
student loan.

The same file was writable by the test suite, which is how `BrowserTestCategory`
came to be sitting in a developer's real rules alongside the genuine ones.

Design
------
**Additive.** `CREATE TABLE` plus an `INSERT` backfill. No table is rebuilt and
no existing column changes, so this revision is outside the class of migration
that can lose rows while leaving a valid schema behind — the concern the README's
migration ceremony exists for. The ceremony was still followed.

**One row per (category, keyword)**, not a JSON blob per household. Deleting a
single keyword becomes a `DELETE` of one row rather than a read-modify-write of
a document, which is what makes the Rules page's per-keyword `×` a real
operation. `position` orders them, ascending, lower wins.

**Where the existing rules go.** All of them to the *owner's* household — the
lowest `app_users.id` with `role = 'owner'`, falling back to the lowest
`households.id`. The file's contents belong to whoever has been using the
installation, and that is the owner. Every other household starts from the
built-in defaults instead, which is the whole point: they must not inherit
somebody else's merchants.

`BrowserTestCategory` is skipped on the way in. It is test residue, not a rule
anybody wrote, and carrying it forward would preserve the pollution this
migration is partly about.

**The JSON file is left on disk, untouched.** Deleting it is not this
revision's business: `downgrade()` needs it to mean anything, and a file the
application no longer reads is harmless where an unrecoverable one is not.
"""

from __future__ import annotations

import json
import os

import sqlalchemy as sa
from alembic import op

revision = '20260802_08_category_rules'
down_revision = '20260730_07_identity'
branch_labels = None
depends_on = None

#: Written by the test suite into the developer's real rules file. Not a rule.
_TEST_RESIDUE = {'BrowserTestCategory'}

#: The same defaults `rules.py` has always created a fresh installation with.
#: Repeated here rather than imported: a migration must keep behaving the way it
#: did the day it was written, and importing application code makes it change
#: whenever that code does.
DEFAULT_RULES = {
    'Student Loan': ['First Tech FCU', 'FIRSTMARK'],
    'Investments': ['VANGUARD BUY'],
    'Credit Card': ['CAPITAL ONE', 'CHASE CREDIT CRD'],
    'Auto Loan': ['JPMorgan Chase'],
    'Income': ['TEVA PHARMA'],
}


def _rules_file():
    """`category_rules.json` at the repo root, wherever this file is run from."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)),
                        'category_rules.json')


def _load_existing():
    """The rules to migrate, or the defaults when there is no file."""
    path = _rules_file()
    if not os.path.exists(path):
        return dict(DEFAULT_RULES)
    try:
        with open(path, encoding='utf-8') as handle:
            loaded = json.load(handle)
    except (ValueError, OSError):
        # A corrupt file must not fail the upgrade -- the defaults are a
        # truthful starting point and the file is left on disk to inspect.
        return dict(DEFAULT_RULES)
    if not isinstance(loaded, dict):
        return dict(DEFAULT_RULES)
    return {category: keywords for category, keywords in loaded.items()
            if category not in _TEST_RESIDUE and isinstance(keywords, list)}


def _owner_household(connection):
    """The household the existing rules belong to.

    The owner's, because the file's contents are whoever has been using this
    installation. Falls back to the lowest household id, then to None when there
    is no household at all — a fresh database being stamped, where there is
    nothing to backfill and nothing to get wrong.
    """
    owner = connection.execute(sa.text(
        "SELECT household_id FROM app_users WHERE role = 'owner' "
        'ORDER BY id LIMIT 1')).scalar()
    if owner is not None:
        return owner
    return connection.execute(sa.text(
        'SELECT id FROM households ORDER BY id LIMIT 1')).scalar()


def upgrade():
    op.create_table(
        'category_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('household_id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('keyword', sa.String(length=200), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['household_id'], ['households.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # `household_id` on its own, in addition to leading the two composites
    # below. `TenantScopedMixin` declares the column `index=True`, so a table
    # created by `db.create_all()` — every test database — has it, and one
    # created by this migration would not. That divergence is exactly what
    # `tools/verify_tenancy.py` refuses to start against, and it caught this:
    #
    #   [FAIL] category_rules.household_id is indexed
    #
    # A leading column of a composite is not a substitute here. The tool checks
    # for the standalone index because every scoped table has one, and a table
    # that quietly differs from its model is the beginning of a schema nobody
    # can reason about.
    op.create_index(op.f('ix_category_rules_household_id'), 'category_rules',
                    ['household_id'], unique=False)
    op.create_index('idx_category_rule_unique', 'category_rules',
                    ['household_id', 'category', 'keyword'], unique=True)
    op.create_index('idx_category_rule_order', 'category_rules',
                    ['household_id', 'position'], unique=False)

    connection = op.get_bind()
    household_id = _owner_household(connection)
    if household_id is None:
        return

    rules = _load_existing()
    rows, position = [], 0
    seen = set()
    for category, keywords in rules.items():
        for keyword in keywords:
            if not isinstance(keyword, str) or not keyword.strip():
                continue
            # The unique index would reject a repeat, and a duplicate inside the
            # JSON is entirely possible -- nothing ever enforced uniqueness there.
            key = (category, keyword)
            if key in seen:
                continue
            seen.add(key)
            rows.append({'household_id': household_id, 'category': category,
                         'keyword': keyword, 'position': position})
            position += 1

    if rows:
        connection.execute(
            sa.text('INSERT INTO category_rules '
                    '(household_id, category, keyword, position, created_at) '
                    'VALUES (:household_id, :category, :keyword, :position, '
                    "CURRENT_TIMESTAMP)"),
            rows)


def downgrade():
    op.drop_index('idx_category_rule_order', table_name='category_rules')
    op.drop_index('idx_category_rule_unique', table_name='category_rules')
    op.drop_index(op.f('ix_category_rules_household_id'),
                  table_name='category_rules')
    op.drop_table('category_rules')
