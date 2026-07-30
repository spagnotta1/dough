"""Take a verified, online backup of the application database.

Uses SQLite's backup API rather than copying the file. A plain `shutil.copy`
of a live SQLite database can capture a torn page set if the sync scheduler
thread (finance_sync/scheduler.py) commits mid-copy; `Connection.backup()`
holds the right locks and produces a consistent snapshot without stopping the
app.

Every backup is verified before it is kept:

  1. `PRAGMA integrity_check` on the copy must return "ok".
  2. Row counts for every table in the source must match the copy.

A backup that fails either check is deleted rather than left on disk, because
a corrupt file that *looks* like a backup is worse than no backup at all --
it is the one you reach for at the worst possible moment.

Usage:
    python tools/backup_db.py                  # back up checkbook.db
    python tools/backup_db.py --label pre-tenancy
    python tools/backup_db.py --db other.db --keep 10
    python tools/backup_db.py --verify-only backups/checkbook-....db
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
DEFAULT_DB = os.path.join(REPO_ROOT, 'checkbook.db')
DEFAULT_BACKUP_DIR = os.path.join(REPO_ROOT, 'backups')
DEFAULT_KEEP = 5


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    # Table names come from sqlite_master, not user input, so the f-string
    # here cannot carry injected SQL. Identifiers can't be parameterised.
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in _table_names(conn)}


def verify(backup_path: str, expected_counts: dict[str, int] | None = None) -> list[str]:
    """Return a list of problems; empty means the backup is sound."""
    problems: list[str] = []
    conn = sqlite3.connect(f'file:{backup_path}?mode=ro', uri=True)
    try:
        result = conn.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            problems.append(f'integrity_check returned {result!r}')

        if expected_counts is not None:
            actual = _row_counts(conn)
            for table, expected in sorted(expected_counts.items()):
                if table not in actual:
                    problems.append(f'table {table!r} missing from backup')
                elif actual[table] != expected:
                    problems.append(
                        f'table {table!r}: {actual[table]} rows in backup, '
                        f'{expected} in source')
            for table in sorted(set(actual) - set(expected_counts)):
                problems.append(f'table {table!r} present in backup but not source')
    finally:
        conn.close()
    return problems


def prune(backup_dir: str, stem: str, keep: int) -> list[str]:
    """Delete all but the `keep` newest backups. Returns what was removed."""
    if keep <= 0:
        return []
    candidates = sorted(
        (e for e in os.scandir(backup_dir)
         if e.is_file() and e.name.startswith(stem + '-') and e.name.endswith('.db')),
        key=lambda e: e.stat().st_mtime,
        reverse=True)
    removed = []
    for entry in candidates[keep:]:
        os.remove(entry.path)
        removed.append(entry.name)
    return removed


def backup(db_path: str, backup_dir: str, *, label: str | None = None,
           keep: int = DEFAULT_KEEP) -> str:
    if not os.path.exists(db_path):
        raise SystemExit(f'No database at {db_path}')

    os.makedirs(backup_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(db_path))[0]
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    suffix = f'-{label}' if label else ''
    dest_path = os.path.join(backup_dir, f'{stem}-{stamp}{suffix}.db')

    source = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        expected = _row_counts(source)
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    problems = verify(dest_path, expected)
    if problems:
        os.remove(dest_path)
        for problem in problems:
            print(f'  FAIL  {problem}', file=sys.stderr)
        raise SystemExit(f'Backup verification failed; {dest_path} deleted.')

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    total = sum(expected.values())
    print(f'Backed up {db_path}')
    print(f'       -> {dest_path}  ({size_mb:.2f} MB)')
    print(f'   verified: integrity_check ok, {len(expected)} tables, {total} rows')

    for name in prune(backup_dir, stem, keep):
        print(f'    pruned: {name}')

    return dest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=DEFAULT_DB, help='database to back up')
    parser.add_argument('--dir', default=DEFAULT_BACKUP_DIR, help='backup directory')
    parser.add_argument('--label', default=None,
                        help='suffix for the filename, e.g. pre-tenancy')
    parser.add_argument('--keep', type=int, default=DEFAULT_KEEP,
                        help=f'how many backups to retain (default {DEFAULT_KEEP}; 0 = all)')
    parser.add_argument('--verify-only', metavar='PATH', default=None,
                        help='verify an existing backup instead of taking one')
    args = parser.parse_args(argv)

    if args.verify_only:
        problems = verify(args.verify_only)
        if problems:
            for problem in problems:
                print(f'  FAIL  {problem}', file=sys.stderr)
            return 1
        print(f'{args.verify_only}: integrity_check ok')
        return 0

    backup(args.db, args.dir, label=args.label, keep=args.keep)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
