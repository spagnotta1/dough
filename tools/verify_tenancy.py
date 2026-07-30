"""A database health check dedicated to tenancy.

Run immediately after `20260726_02_multitenancy` — against a copy first, then
against the real database once it has been migrated — and before the
application starts serving requests.

    python tools/verify_tenancy.py checkbook.db
    python tools/verify_tenancy.py checkbook.db --baseline before.json
    python tools/verify_tenancy.py checkbook.db --emit-baseline before.json

Exit status is 0 when every invariant holds and 1 when any fails, so it drops
into a deploy script without anyone having to read the output.

## What this is for, and what it is not for

`tests/test_tenancy_boundary.py` proves the *code* isolates households. This
proves the *data* is in a state where that isolation means something. They fail
in different ways and neither substitutes for the other: a perfect ORM filter
over rows with a NULL household still serves nothing to nobody, and perfectly
partitioned data behind a route that forgot its household check still leaks.

The checks are deliberately written as SQL against the file rather than through
the ORM. The ORM is the thing under test — asking it whether the data is
correctly partitioned means asking the tenant filter whether the tenant filter
works, and it will always say yes.

## Why a row-count baseline

Structural checks catch a migration that produced the wrong *shape*. They do not
catch one that produced the right shape with rows missing, which is the specific
failure mode of a `batch_alter_table` rebuild: create-temp / copy / drop /
rename, where a bad copy step silently loses rows and leaves a perfectly valid
schema behind. So capture counts before, compare after.

    python tools/verify_tenancy.py before.db --emit-baseline counts.json
    flask db upgrade
    python tools/verify_tenancy.py after.db --baseline counts.json

`households` and `app_users.role` are expected to differ — the identity tables
the migration introduces — and the comparison accounts for that.

## Two comparison modes, because there are two things worth checking

A migration must preserve row counts **exactly**: it is not supposed to create
or destroy anything, so any change at all is a defect. That is the default.

A *sync* is supposed to insert rows, and the useful question becomes whether it
inserted them in the right households and only in the tables a sync writes.
`--may-grow` expresses that:

    python tools/verify_tenancy.py checkbook.db --emit-baseline counts.json
    # ... run one sync ...
    python tools/verify_tenancy.py checkbook.db --baseline counts.json \\
        --may-grow sync

Named tables may gain rows and must not lose any; every other table must still
match exactly. `sync` is shorthand for the tables a sync is allowed to touch
(`SYNC_WRITTEN_TABLES` below) — a sync that inserted a `budgets` row would fail
this check, which is the point. Nothing may ever shrink in either mode.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

#: Tables carrying a non-nullable `household_id`. Kept as a literal list rather
#: than derived from the models, on purpose: this script's job is to check the
#: database against what tenancy is *supposed* to cover, and deriving the
#: expectation from the same source as the implementation would make a model
#: that forgot the mixin verify clean.
SCOPED_TABLES = (
    'account_balance', 'budgets', 'chat_messages', 'connected_accounts',
    'conversations', 'financial_accounts', 'holdings', 'household_invites',
    'log_entry', 'portfolio_snapshots', 'recurring_dismissals', 'sync_errors',
    'sync_history', 'transactions',
)

#: The one table that carries `household_id` and is allowed to leave it NULL,
#: with the reason. Listed positively rather than merely left out of
#: SCOPED_TABLES: "absent from a list" is indistinguishable from "somebody
#: forgot", and the whole value of this script is that the exceptions are
#: written down where a reviewer will read them.
#:
#: This is the second such exception, after `household_invites.token_hash`
#: above, and it costs the same thing: `audit_events` is outside the ORM tenant
#: backstop, so its isolation is `dough/services/audit.py::recent()` and nothing
#: else. Every read of this table goes through that one function.
EXPECTED_NULLABLE_HOUSEHOLD = {
    'audit_events': 'a failed login belongs to no household -- there is no '
                    'tenant yet, and inventing one would attribute an '
                    'anonymous attempt to a real household',
}

#: Tables a migration creates. Excluded from the before/after row-count
#: comparison because they have no "before". `households` came from
#: 20260726_02, `household_invites` from 20260726_03, `audit_events` from
#: 20260727_04.
NEW_TABLES = ('households', 'household_invites', 'audit_events')

#: The only tables a synchronization run may add rows to. Shorthand for
#: `--may-grow sync`. Deliberately a short list: a sync that inserted a budget
#: or a chat message is doing something nobody asked it to, and the point of
#: naming the permitted tables is to notice that rather than to wave it through
#: because *some* growth was expected.
SYNC_WRITTEN_TABLES = (
    'transactions', 'financial_accounts', 'holdings', 'account_balance',
    'portfolio_snapshots', 'sync_history', 'sync_errors', 'market_prices',
    'connected_accounts',
)

#: Composite unique constraints that must lead with `household_id`, and the
#: full column tuple each must enforce afterwards.
EXPECTED_UNIQUE = {
    'account_balance': ('household_id', 'account_type'),
    'budgets': ('household_id', 'category', 'account_name'),
    'connected_accounts': ('household_id', 'institution', 'item_id'),
    'portfolio_snapshots': ('household_id', 'snapshot_date'),
    'recurring_dismissals': ('household_id', 'desc_key'),
    'transactions': ('household_id', 'account_name', 'date', 'description',
                     'amount'),
}

#: Uniqueness that must NOT have gained a household column, with the reason.
#: Checked as explicitly as the ones that must change: an over-eager migration
#: that scoped `app_users.username` would let two households register the same
#: login, which is a defect nothing else here would notice.
EXPECTED_STILL_GLOBAL = {
    'app_users': (('username',),
                  'one login namespace across the installation'),
    'market_prices': (('symbol',),
                      'public market data, shared cache, not tenant data'),
    'household_invites': (('token_hash',),
                          'the redemption lookup runs before any household is '
                          'known, so this cannot be scoped'),
}


class Report:
    """Accumulates results so a run reports every failure, not just the first."""

    def __init__(self, verbose=False):
        self.checks = []
        self.verbose = verbose

    def record(self, name, ok, detail=''):
        self.checks.append((name, ok, detail))
        if not ok or self.verbose:
            mark = 'ok  ' if ok else 'FAIL'
            print(f'  [{mark}] {name}' + (f' — {detail}' if detail else ''))
        return ok

    @property
    def failures(self):
        return [c for c in self.checks if not c[1]]

    def summary(self):
        failed = len(self.failures)
        total = len(self.checks)
        if failed:
            print(f'\n{failed} of {total} invariants FAILED. '
                  f'Do not start the application against this database.')
        else:
            print(f'\nAll {total} tenancy invariants hold.')
        return 1 if failed else 0


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


def _columns(conn, table):
    return {r[1]: {'type': r[2], 'notnull': bool(r[3]), 'pk': bool(r[5])}
            for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _unique_signatures(conn, table):
    """Every uniqueness constraint on a table, as a set of column tuples.

    Compared by the columns enforced rather than by index name, because the same
    guarantee arrives with three different names depending on how it was
    declared — `sqlite_autoindex_x_1` for a column constraint, the given name
    for an explicit Index, and a third for a named UniqueConstraint.
    """
    signatures = set()
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        name, unique = row[1], bool(row[2])
        if not unique:
            continue
        signatures.add(tuple(r[2] for r in conn.execute(
            f'PRAGMA index_info("{name}")')))
    return signatures


def _indexed_columns(conn, table):
    return {tuple(r[2] for r in conn.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in conn.execute(f'PRAGMA index_list("{table}")')}


def row_counts(conn):
    return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(_tables(conn))
            if table != 'alembic_version'}


# ---------------------------------------------------------------------------
# The invariants
# ---------------------------------------------------------------------------

def check_structure(conn, report):
    """The schema is shaped the way tenancy requires."""
    print('\nStructure')
    tables = _tables(conn)

    report.record('households table exists', 'households' in tables)
    if 'households' not in tables:
        return  # everything below is meaningless without it

    for table in SCOPED_TABLES:
        if not report.record(f'{table} exists', table in tables):
            continue
        cols = _columns(conn, table)
        if not report.record(f'{table}.household_id exists',
                             'household_id' in cols):
            continue
        # NOT NULL is the structural half of the guarantee. Without it the
        # column is advisory: a future INSERT that omits it produces a row
        # belonging to nobody, and a filter comparing NULL to an id matches
        # nothing, so the row becomes invisible rather than loudly wrong.
        report.record(f'{table}.household_id is NOT NULL',
                      cols['household_id']['notnull'])
        report.record(
            f'{table}.household_id is indexed',
            ('household_id',) in _indexed_columns(conn, table),
            'every query in the app filters on it')

    for table, reason in sorted(EXPECTED_NULLABLE_HOUSEHOLD.items()):
        if not report.record(f'{table} exists', table in tables):
            continue
        cols = _columns(conn, table)
        if not report.record(f'{table}.household_id exists',
                             'household_id' in cols):
            continue
        # Asserted nullable, not merely tolerated. If a later migration made
        # this NOT NULL the exception would have quietly stopped being one, and
        # the first symptom would be a failed login that cannot be recorded.
        report.record(f'{table}.household_id is nullable by design',
                      not cols['household_id']['notnull'], reason)
        report.record(f'{table}.household_id is indexed',
                      ('household_id',) in _indexed_columns(conn, table),
                      'audit.recent() filters on it and it is the only filter')

    cols = _columns(conn, 'app_users')
    report.record('app_users.household_id exists and is NOT NULL',
                  'household_id' in cols and cols['household_id']['notnull'])
    report.record('app_users.role exists', 'role' in cols)


def check_foreign_keys(conn, report):
    """Every household_id points at a household that exists."""
    print('\nForeign keys')

    # PRAGMA foreign_key_check walks every declared FK in the database, not
    # only the tenancy ones. That is deliberate — a batch rebuild is exactly
    # the operation that broke 27 foreign keys the last time this schema was
    # restructured (ADR-0007), and it broke them in tables nobody was editing.
    violations = list(conn.execute('PRAGMA foreign_key_check'))
    report.record('no foreign key violations anywhere in the database',
                  not violations,
                  f'{len(violations)} violation(s): {violations[:5]}')

    for table in SCOPED_TABLES + ('app_users',):
        declared = {r[3]: r[2] for r in conn.execute(
            f'PRAGMA foreign_key_list("{table}")')}
        report.record(f'{table}.household_id declares a FK to households',
                      declared.get('household_id') == 'households',
                      f'points at {declared.get("household_id")!r}')

        orphans = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" t LEFT JOIN households h '
            f'ON t.household_id = h.id WHERE h.id IS NULL').fetchone()[0]
        report.record(f'{table}: every household_id resolves', orphans == 0,
                      f'{orphans} row(s) reference a missing household')


def check_no_orphan_rows(conn, report):
    """No tenant-scoped row belongs to nobody."""
    print('\nOrphaned rows')
    for table in SCOPED_TABLES:
        nulls = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE household_id IS NULL'
        ).fetchone()[0]
        report.record(f'{table}: zero NULL household_id', nulls == 0,
                      f'{nulls} row(s) belong to no household')


def check_membership(conn, report):
    """Identity and tenancy agree about who is where."""
    print('\nMembership')

    households = conn.execute('SELECT COUNT(*) FROM households').fetchone()[0]
    users = conn.execute('SELECT COUNT(*) FROM app_users').fetchone()[0]

    # A household with no owner is one nobody can administer, and after Phase 6
    # adds invitations it is one nobody can ever be added to.
    ownerless = [r[0] for r in conn.execute(
        "SELECT h.id FROM households h WHERE NOT EXISTS ("
        "  SELECT 1 FROM app_users u "
        "  WHERE u.household_id = h.id AND u.role = 'owner')")]
    report.record('every household has at least one owner', not ownerless,
                  f'household(s) with no owner: {ownerless}')

    # Not a tautology even though household_id is a NOT NULL scalar: this
    # catches a value that is present and points nowhere, which is a different
    # failure from NULL and is what a bad backfill actually produces.
    homeless = conn.execute(
        'SELECT COUNT(*) FROM app_users u LEFT JOIN households h '
        'ON u.household_id = h.id WHERE h.id IS NULL').fetchone()[0]
    report.record('every user belongs to exactly one existing household',
                  homeless == 0, f'{homeless} user(s) point at no household')

    report.record('roles are recognised values',
                  not list(conn.execute(
                      "SELECT id FROM app_users "
                      "WHERE role NOT IN ('owner', 'member')")))

    if households and not users:
        report.record('households exist without any users', False,
                      f'{households} household(s), 0 users — nobody can sign in')
    else:
        report.record('households exist without any users', True)


def check_connections(conn, report):
    """Plaid connections, and everything hanging off them, stay in one household.

    The nested checks matter more than they look. `financial_accounts`,
    `transactions` and `holdings` each carry both their own `household_id` and a
    foreign key to a parent that carries one too. Nothing in the schema forces
    those to agree, so a sync running under the wrong tenant context would write
    rows that are individually valid and collectively wrong — one household's
    transactions attached to another household's bank account.
    """
    print('\nConnection ownership')

    mismatched = conn.execute(
        'SELECT COUNT(*) FROM financial_accounts a '
        'JOIN connected_accounts c ON a.connection_id = c.id '
        'WHERE a.household_id != c.household_id').fetchone()[0]
    report.record('every financial_account matches its connection\'s household',
                  mismatched == 0, f'{mismatched} mismatched')

    for child, parent, fk in (('transactions', 'financial_accounts', 'account_id'),
                              ('holdings', 'financial_accounts', 'account_id'),
                              ('sync_history', 'connected_accounts', 'connection_id'),
                              ('sync_errors', 'sync_history', 'run_id')):
        mismatched = conn.execute(
            f'SELECT COUNT(*) FROM "{child}" c '
            f'JOIN "{parent}" p ON c.{fk} = p.id '
            f'WHERE c.household_id != p.household_id').fetchone()[0]
        report.record(
            f'every {child} row matches its {parent} household',
            mismatched == 0, f'{mismatched} mismatched')

    duplicated = list(conn.execute(
        'SELECT item_id, COUNT(DISTINCT household_id) n FROM connected_accounts '
        'WHERE item_id IS NOT NULL GROUP BY item_id HAVING n > 1'))
    report.record('no Plaid item is shared between households', not duplicated,
                  f'shared item_id(s): {duplicated}')


def check_uniqueness(conn, report):
    """Uniqueness means "unique within a household" wherever it should."""
    print('\nUniqueness')

    for table, expected in EXPECTED_UNIQUE.items():
        signatures = _unique_signatures(conn, table)
        report.record(f'{table} enforces UNIQUE{list(expected)}',
                      expected in signatures,
                      f'found {sorted(signatures)}')
        # The old global constraint must be *gone*, not merely joined by a
        # composite one. Left in place it still rejects the second household's
        # identical row, and the composite index makes it look fixed.
        stale = {s for s in signatures
                 if 'household_id' not in s and set(s) <= set(expected)}
        report.record(f'{table} has no leftover household-blind UNIQUE',
                      not stale, f'still enforcing {sorted(stale)}')

    for table, (expected, why) in EXPECTED_STILL_GLOBAL.items():
        signatures = _unique_signatures(conn, table)
        report.record(f'{table} keeps UNIQUE{list(expected)} global',
                      expected in signatures, why)


def check_row_counts(conn, report, baseline, may_grow=()):
    """Nothing was lost in fourteen table rebuilds.

    `may_grow` names tables permitted to have gained rows — see the module
    docstring for why a sync needs that and a migration must not have it.
    Shrinking is a failure in both modes: no operation covered by this tool
    deletes rows, so a table that lost some has lost them to a bug.
    """
    print('\nRow counts')
    after = row_counts(conn)
    may_grow = set(may_grow)

    for table, before in sorted(baseline.items()):
        now = after.get(table)
        if now is None:
            report.record(f'{table}: still present', False, 'table is gone')
        elif table in may_grow:
            report.record(f'{table}: {before} -> {now}, none lost', now >= before,
                          f'lost {before - now} row(s)')
        else:
            report.record(f'{table}: {before} rows preserved', now == before,
                          f'was {before}, now {now}')

    unexpected = set(after) - set(baseline) - set(NEW_TABLES)
    report.record('no unexplained new tables', not unexpected,
                  f'appeared: {sorted(unexpected)}')

    # A leftover `_alembic_tmp_*` is the fingerprint of a batch rebuild that
    # died between copy and rename. The schema can look entirely correct with
    # one of these sitting next to it.
    leftovers = [t for t in _tables(conn) if t.startswith('_alembic_tmp')]
    report.record('no abandoned batch-migration temp tables', not leftovers,
                  f'found: {leftovers}')


# ---------------------------------------------------------------------------

def resolve_may_grow(raw):
    """Expand the `--may-grow` argument, including the `sync` shorthand."""
    names = [n.strip() for n in (raw or '').split(',') if n.strip()]
    resolved = set()
    for name in names:
        if name == 'sync':
            resolved.update(SYNC_WRITTEN_TABLES)
        else:
            resolved.add(name)
    return resolved


def verify(path, baseline=None, verbose=False, may_grow=()):
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        report = Report(verbose=verbose)
        print(f'Verifying tenancy invariants in {path}')

        check_structure(conn, report)
        # Every check below reads household_id and would raise rather than fail
        # if the column is not there. A structural failure is already fatal.
        if report.failures:
            print('\nStructural checks failed; skipping the data checks, which '
                  'assume the schema is in place.')
            return report.summary()

        check_no_orphan_rows(conn, report)
        check_foreign_keys(conn, report)
        check_membership(conn, report)
        check_connections(conn, report)
        check_uniqueness(conn, report)
        if baseline is not None:
            check_row_counts(conn, report, baseline, may_grow=may_grow)
        else:
            print('\nRow counts\n  (skipped — no --baseline given)')

        return report.summary()
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('database')
    parser.add_argument('--baseline', metavar='FILE',
                        help='row counts captured before the migration')
    parser.add_argument('--emit-baseline', metavar='FILE',
                        help='write current row counts and exit, for use as '
                             '--baseline after the migration')
    parser.add_argument('--may-grow', metavar='TABLES', default='',
                        help='comma-separated tables allowed to have gained '
                             'rows since the baseline; "sync" expands to the '
                             'tables a sync writes. Everything else must still '
                             'match exactly, and nothing may shrink.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='print passing checks too')
    args = parser.parse_args(argv)

    if args.emit_baseline:
        conn = sqlite3.connect(f'file:{args.database}?mode=ro', uri=True)
        try:
            counts = row_counts(conn)
        finally:
            conn.close()
        with open(args.emit_baseline, 'w', encoding='utf-8') as fh:
            json.dump(counts, fh, indent=1, sort_keys=True)
        print(f'Wrote row counts for {len(counts)} tables to {args.emit_baseline}')
        return 0

    baseline = None
    if args.baseline:
        with open(args.baseline, encoding='utf-8') as fh:
            baseline = json.load(fh)

    return verify(args.database, baseline=baseline, verbose=args.verbose,
                  may_grow=resolve_may_grow(args.may_grow))


if __name__ == '__main__':
    sys.exit(main())
