"""Take a verified, online backup of the application database.

The mechanics moved to `dough/services/backup.py` in Phase 10.6 and did not
change; this is now the command-line front end to them. The reason for the move
is that backups are also taken on a schedule (`BackupScheduler`), and an
operator's manual backup and the unattended one have to be the same code — a
snapshot path that only this file exercises is one nobody runs nightly.

What that module guarantees, restated because it is why you would trust the
output: the copy is made with SQLite's backup API rather than `shutil.copy`, so
a commit from the sync thread mid-copy cannot tear it; and every copy is
verified (`PRAGMA integrity_check`, plus per-table row counts against the
source) before it is kept, with a failing copy deleted rather than left to be
discovered at a restore.

Backing up the database is not backing up the deployment. `.sync_encryption_key`
holds the key to every stored institution credential and is not in this file;
restoring without it gives you an application that starts and cannot sync. See
docs/runbooks/disaster-recovery.md.

Usage:
    python tools/backup_db.py                  # back up checkbook.db
    python tools/backup_db.py --label pre-tenancy
    python tools/backup_db.py --db other.db --keep 10
    python tools/backup_db.py --verify-only backups/checkbook-....db
"""

from __future__ import annotations

import argparse
import os
import sys

# Imported from the application package, so this command and the scheduled loop
# run one implementation. Path insert rather than an installed package because
# this repository is not pip-installed; `tools/dr_drill.py` does the same. From
# `__file__` rather than `os.getcwd()` so it works from any directory.
REPO_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, REPO_ROOT)

from dough.services.backup import (  # noqa: E402  (after the path insert)
    BackupError, backup, describe, prune, verify)

DEFAULT_DB = os.path.join(REPO_ROOT, 'checkbook.db')
DEFAULT_BACKUP_DIR = os.path.join(REPO_ROOT, 'backups')

#: The CLI keeps its own retention default, unchanged from before the split.
#: `BackupScheduler` keeps 7 instead, and the difference is deliberate: this
#: number bounds a directory somebody is filling by hand, usually around a
#: migration, while the scheduler's bounds a week of dailies.
DEFAULT_KEEP = 5


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

    try:
        dest_path = backup(args.db, args.dir, label=args.label)
    except BackupError as exc:
        print(f'  FAIL  {exc}', file=sys.stderr)
        return 1

    print(f'Backed up {args.db}')
    print(f'       -> {describe(dest_path)}')
    print('   verified: integrity_check ok, row counts match source')

    stem = os.path.splitext(os.path.basename(args.db))[0]
    for name in prune(args.dir, stem, args.keep):
        print(f'    pruned: {name}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
