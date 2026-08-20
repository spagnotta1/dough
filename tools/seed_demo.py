"""Fill the demo household with a complete, believable financial life.

The generator is `dough/services/demo_seed.py`; this is the command-line front
end to it, and it exists separately for the same reason `tools/backup_db.py`
does — the dangerous part of a destructive operation is the target, not the
algorithm, so resolving the target is kept where an operator can read it.

    python tools/seed_demo.py --household RankParsley --dry-run
    python tools/seed_demo.py --household RankParsley --yes

On Railway, where the database is on the volume at /data/checkbook.db:

    railway ssh -- python tools/seed_demo.py --household RankParsley --yes

## Why a username and not an id

An id is a number, and a number is a typo away from another household. A
username has to *exist*, has to be in `demo_seed.DEMO_USERNAMES`, and has to be
the only member of the household it names — all three are checked before the
first row is deleted. `--household` also accepts an id for the case where an
operator is looking at the database rather than the login page, and the same
three checks then run against whatever it resolves to.

Re-running is the intended way to use this. Every date is computed from today,
so a demo that has gone stale is fixed by running it again rather than by
editing anything.

Exit status 0 on success, 1 on a refused or failed run.
"""

from __future__ import annotations

import argparse
import os
import sys

# Path insert rather than an installed package, matching tools/backup_db.py and
# tools/dr_drill.py. From `__file__` so this works from any directory.
REPO_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, REPO_ROOT)

from app import create_app  # noqa: E402  (after the path insert)
from dough.services import demo_seed  # noqa: E402
from dough.tenancy import unscoped  # noqa: E402
from models import AppUser, db  # noqa: E402


def resolve_household(target):
    """`target` as a household id, from a username or a bare id.

    Returns (household_id, description). Raises SystemExit with a message an
    operator can act on rather than a traceback — every failure here is a
    mistyped argument or a database that does not have the account yet.
    """
    with unscoped():
        if target.isdigit():
            user = AppUser.query.filter_by(household_id=int(target)).first()
            if user is None:
                raise SystemExit(f'No household with id {target}.')
            return int(target), f'household {target} ({user.username})'

        user = AppUser.query.filter_by(username=target).first()
        if user is None:
            known = sorted(u.username for u in AppUser.query.all())
            raise SystemExit(
                f'No user named {target!r}. Accounts in this database: '
                f'{known or "(none)"}')
        if user.household_id is None:
            raise SystemExit(
                f'{target!r} exists but belongs to no household, so there is '
                'nothing to seed. Sign in as that account once to create one.')
        return user.household_id, f'{user.username} (household {user.household_id})'


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--household', default='RankParsley',
                        help='demo account username, or a household id '
                             '(default: RankParsley)')
    parser.add_argument('--yes', action='store_true',
                        help='confirm that the household may be wiped and regenerated')
    parser.add_argument('--dry-run', action='store_true',
                        help='check the target and report what is there now, '
                             'without deleting or writing anything')
    parser.add_argument('--months', type=int, default=demo_seed.HISTORY_MONTHS,
                        help=f'months of history (default: {demo_seed.HISTORY_MONTHS})')
    parser.add_argument('--seed', type=int, default=demo_seed.DEFAULT_SEED,
                        help='RNG seed; the same seed on the same day gives the '
                             'same household')
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        household_id, described = resolve_household(args.household)

        try:
            household = demo_seed.assert_is_demo_household(household_id)
        except demo_seed.NotADemoHousehold as refusal:
            print(f'REFUSED: {refusal}', file=sys.stderr)
            return 1

        print(f'Target: {described} - {household.name!r}')

        if args.dry_run:
            _report_current(household_id)
            print('\nDry run: nothing was deleted or written.')
            return 0

        if not args.yes:
            print('\nThis DELETES every row belonging to that household and '
                  'regenerates it.\nRe-run with --yes to proceed, or --dry-run '
                  'to look first.', file=sys.stderr)
            return 1

        result = demo_seed.seed_demo_household(
            household_id, seed=args.seed, history_months=args.months)

    print(f'\nRemoved {result.pop("removed")} existing rows.')
    for label, count in result.items():
        print(f'  {label:<18} {count}')
    print('\nDone. Sign in as the demo account to see it.')
    return 0


def _report_current(household_id):
    """What the household holds right now — the thing --yes would delete."""
    from dough.tenancy import tenant_scope, tenant_scoped_models

    with tenant_scope(household_id):
        print('\nCurrently in this household:')
        empty = True
        for model in tenant_scoped_models():
            count = (db.session.query(model)
                     .filter(model.household_id == household_id).count())
            if count:
                empty = False
                print(f'  {model.__tablename__:<22} {count}')
        if empty:
            print('  (nothing)')


if __name__ == '__main__':
    sys.exit(main())
