"""Remove the seeded DEFAULT_RULES rows that Phase 11A.2 left behind.

Revision ID: 20260803_10_unseed
Revises: 20260802_09_goals
Create Date: 2026-08-03

Background
----------
Until Phase 11A.2, `rules.DEFAULT_RULES` held five categories that read as a
generic starter set and were in fact one person's financial life — a credit
union, a student-loan servicer, a broker, two card issuers, an auto lender and
an employer. `seed_defaults()` copied all seven rules into every household on
first read of `/rules`, so a second account signed in and read the first
account's banks.

Phase 11A.2 fixed the *source*: `DEFAULT_RULES` is now `{}` and `seed_defaults`
is gone, so no new household receives them. It did not fix the *data*. Every
household seeded before that commit still holds its copy, and
`docs/rule-engine.md` says exactly why the code fix was not enough:

    No amount of correct filtering fixes seed data that should never have been
    written.

This is that cleanup. Reported from the running app: a household's Rules page
listed `Student Loan → First Tech FCU, FIRSTMARK` and `Investments → VANGUARD
BUY` — merchants that account has never transacted with — because it had been
seeded and never re-derived.

What it deletes, and what it deliberately does not
--------------------------------------------------
Only households whose **entire** rule set is a subset of the seven seeded pairs
are cleared. That is the test for "this household never wrote a rule of its
own": it received the seed and nothing else, so every row it holds is somebody
else's data and none of it can be a rule it authored.

A household that holds a seeded pair *alongside* rules of its own is left
completely untouched, even though it is still carrying the seed. That is the
conservative direction on purpose. The developer's own household legitimately
banks with First Tech FCU and holds ~107 rules including those pairs; deleting
by pair alone would silently destroy real, hand-written rules to tidy up data
that is — for that one household — correct. A cleanup that damages good rules
to remove bad ones is worse than the bug, so the ambiguous case is left for a
person to resolve on the Rules page, where "Clear all rules" now does exactly
this on demand.

Categories are not touched
--------------------------
`Transaction.category` is left exactly as it is. Re-deriving here would need the
matching engine (`rules.CategoryRules`) inside a migration, and a migration that
imports application code breaks the moment that code moves — the failure mode
`20260726_02` was written to avoid. The re-derivation happens in the
application instead, on the next rule edit or AI analysis, both of which call
`_recategorize()` over the whole ledger.

The visible consequence in between is transactions still labelled `Student Loan`
with no rule behind them. That is honest rather than tidy: the label is stale
either way, and the Rules page's Transactions column now counts *rule matches*
rather than labels, so a category with no rules left reads 0 and says so.

Interaction with `20260802_08_category_rules`
---------------------------------------------
That revision backfills the rules table from `category_rules.json` at the repo
root, and falls back to `DEFAULT_RULES` when the file is absent — which it now
is, since the file was deleted once its contents had been backfilled (it is
gitignored, so a fresh checkout never had one either).

So on a chain-from-empty run, 08 inserts the seven seeded pairs into the owner
household and this revision deletes them again a moment later. That reads like
waste and is worth leaving alone: 08 must keep its fallback for installations
whose file still exists, and the net result here is the correct one — a fresh
database reaches head with no rules, which is exactly what a new household
should have. The two revisions disagreeing about the seed is the *point*; this
one is the later opinion.

Reversibility
-------------
`downgrade()` is a no-op and cannot be otherwise. The rows carry no marker
saying they came from the seed, so there is nothing to restore them from and no
way to tell which households had been seeded before this ran. Re-inserting the
seed into every household would recreate the disclosure this deletes. A
downgrade that does nothing is the correct answer here, not a missing one.
"""

import sqlalchemy as sa
from alembic import op

revision = '20260803_10_unseed'
down_revision = '20260802_09_goals'
branch_labels = None
depends_on = None


#: The exact contents of `rules.DEFAULT_RULES` as of the commit before it was
#: emptied (`26720e6~1`). Copied here as a literal rather than imported: the
#: constant is `{}` in the application today, so importing it would delete
#: nothing, and a migration must describe the world as it was when the rows
#: were written.
SEEDED_PAIRS = frozenset({
    ('Student Loan', 'First Tech FCU'),
    ('Student Loan', 'FIRSTMARK'),
    ('Investments', 'VANGUARD BUY'),
    ('Credit Card', 'CAPITAL ONE'),
    ('Credit Card', 'CHASE CREDIT CRD'),
    ('Auto Loan', 'JPMorgan Chase'),
    ('Income', 'TEVA PHARMA'),
})


def rows_to_delete(rows):
    """Which `category_rules.id`s to remove, given `(id, household, cat, kw)`.

    Split out from `upgrade()` so the decision can be tested directly against
    the cases that matter — a seeded-only household, a household that wrote its
    own rules, and one holding both — without standing up an Alembic context to
    do it. `tests/test_rules_tenancy.py` exercises exactly those three.
    """
    by_household = {}
    for row_id, household_id, category, keyword in rows:
        by_household.setdefault(household_id, []).append(
            (row_id, (category, keyword)))

    doomed = []
    for entries in by_household.values():
        pairs = {pair for _, pair in entries}
        # Subset, not intersection — see the module docstring. A household
        # holding anything outside the seed authored rules of its own and is
        # left alone entirely.
        if pairs and pairs <= SEEDED_PAIRS:
            doomed.extend(row_id for row_id, _ in entries)
    return doomed


def upgrade():
    bind = op.get_bind()
    rules = sa.table('category_rules',
                     sa.column('id', sa.Integer),
                     sa.column('household_id', sa.Integer),
                     sa.column('category', sa.String),
                     sa.column('keyword', sa.String))

    rows = bind.execute(sa.select(
        rules.c.id, rules.c.household_id,
        rules.c.category, rules.c.keyword)).fetchall()

    doomed = rows_to_delete(rows)
    if doomed:
        bind.execute(rules.delete().where(rules.c.id.in_(doomed)))


def downgrade():
    """Deliberately empty — see "Reversibility" in the module docstring."""
