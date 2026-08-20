"""Fill the demo household with a complete, believable financial life.

The generator is `dough/services/demo_seed.py`; this is the command-line front
end to it, and it exists separately for the same reason `tools/backup_db.py`
does — the dangerous part of a destructive operation is the target, not the
algorithm, so resolving the target is kept where an operator can read it.

    python tools/seed_demo.py --household RankParsley --dry-run
    python tools/seed_demo.py --household RankParsley --yes

On Railway, where the database is on the volume at /data/checkbook.db:

    railway ssh -- python tools/seed_demo.py --household RankParsley --yes

On an installation that has never had the demo account at all, create it in the
same run. The password is read from $DEMO_ACCOUNT_PASSWORD, never a flag:

    DEMO_ACCOUNT_PASSWORD=... python tools/seed_demo.py --create-account --yes

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

## Why this can create an account

The seeder refuses to run against a household that does not exist, which left a
gap nothing in the repository closed: on a fresh installation there was no demo
account to point it at, and no documented way to make one. `--create-account`
closes it, under the same constraint the rest of this tool works under -- it
will only ever create a name that is already in `demo_seed.DEMO_USERNAMES`, so
it cannot be turned into a way to mint an arbitrary user in production.

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


#: Where the demo account's mail would go, if anything ever sent it any. The
#: `.invalid` TLD is reserved by RFC 2606 so that it can never resolve, which is
#: exactly the property wanted: the account must own an address, and that address
#: must never be one a real person could receive.
DEMO_EMAIL_DOMAIN = 'demo.invalid'

#: The password is read from the environment rather than taken as a flag. A
#: password in a flag is a password in shell history, in `ps` output, and -- on
#: Railway, where this runs over `railway ssh` -- in a process log somebody else
#: can read.
PASSWORD_ENV = 'DEMO_ACCOUNT_PASSWORD'


def create_demo_account(username):
    """Register `username` as a new demo account. Returns its household id.

    The guard runs before anything is written and is the same one the seeder
    itself applies: only a name already in `demo_seed.DEMO_USERNAMES` may be
    created here. That is what keeps this from being a general-purpose "add a
    user" command aimed at a production database -- the set is a module
    constant, so extending it is a change that shows up in a diff.

    `register_account` is used rather than an `AppUser(...)` built here on
    purpose. It is the same call the sign-up form makes, so the demo account
    gets the household, the owner role and the password hashing that every
    other account gets, and it cannot drift into being a differently-shaped
    user that only this tool knows how to make.
    """
    from dough.services.identity import IdentityError, register_account

    if username not in demo_seed.DEMO_USERNAMES:
        raise SystemExit(
            f'REFUSED: {username!r} is not a demo account, so it will not be '
            f'created. Demo accounts: {sorted(demo_seed.DEMO_USERNAMES)}.')

    password = os.environ.get(PASSWORD_ENV, '')
    if not password:
        raise SystemExit(
            f'--create-account needs a password and reads it from '
            f'${PASSWORD_ENV}, which is unset. It is not a flag so that it '
            f'stays out of shell history and `ps`.')

    try:
        user = register_account(
            username=username,
            email=f'{username.lower()}@{DEMO_EMAIL_DOMAIN}',
            password=password,
            household_name=f'{username} demo household')
    except IdentityError as refusal:
        # IdentityError is already a sentence written for a person -- "that
        # username is taken", "password must be at least N characters" -- so it
        # is passed through rather than wrapped in a traceback.
        raise SystemExit(f'Could not create {username!r}: {refusal}')

    print(f'Created {username!r} (household {user.household_id}).')
    return user.household_id


def resolve_household(target, create=False):
    """`target` as a household id, from a username or a bare id.

    Returns (household_id, description). Raises SystemExit with a message an
    operator can act on rather than a traceback — every failure here is a
    mistyped argument or a database that does not have the account yet.

    With `create`, a username that does not exist yet is registered instead of
    being an error. An id still cannot be: an id that resolves to nothing names
    no account, so there is nothing to create it *as*.
    """
    with unscoped():
        if target.isdigit():
            user = AppUser.query.filter_by(household_id=int(target)).first()
            if user is None:
                if create:
                    raise SystemExit(
                        f'--create-account needs a username, but --household '
                        f'was given the id {target}, which does not exist. '
                        f'Pass the demo account by name instead.')
                raise SystemExit(f'No household with id {target}.')
            return int(target), f'household {target} ({user.username})'

        user = AppUser.query.filter_by(username=target).first()
        if user is None:
            if create:
                household_id = create_demo_account(target)
                return household_id, f'{target} (household {household_id})'
            known = sorted(u.username for u in AppUser.query.all())
            raise SystemExit(
                f'No user named {target!r}. Accounts in this database: '
                f'{known or "(none)"}. Pass --create-account to register it.')
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
    parser.add_argument('--create-account', action='store_true',
                        help='register the demo account first if this database '
                             f'does not have it yet; reads its password from '
                             f'${PASSWORD_ENV}. Only names in '
                             'demo_seed.DEMO_USERNAMES can be created.')
    parser.add_argument('--months', type=int, default=demo_seed.HISTORY_MONTHS,
                        help=f'months of history (default: {demo_seed.HISTORY_MONTHS})')
    parser.add_argument('--seed', type=int, default=demo_seed.DEFAULT_SEED,
                        help='RNG seed; the same seed on the same day gives the '
                             'same household')
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        # Creating the account is a write, so it answers to the same two
        # gates the seeding does: --dry-run writes nothing, and an unconfirmed
        # run writes nothing. Checked here rather than in resolve_household
        # because both gates are the caller's contract, not the lookup's.
        creating = args.create_account
        if creating and not _account_exists(args.household):
            if args.dry_run:
                print(f'Would create {args.household!r}, then seed its new '
                      f'and empty household.\n\nDry run: nothing was '
                      f'created or written.')
                return 0
            if not args.yes:
                print(f'\n--create-account would register '
                      f'{args.household!r} and then seed it.\nRe-run with '
                      f'--yes to proceed, or --dry-run to look first.',
                      file=sys.stderr)
                return 1

        household_id, described = resolve_household(args.household,
                                                    create=creating)

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


def _account_exists(target):
    """Is there already an account for this `--household` value?

    Only ever asked so the gates in `main` can tell "nothing to create" from
    "about to create something". A bare id counts as existing, because
    `resolve_household` refuses to create from one in either case.
    """
    if target.isdigit():
        return True
    with unscoped():
        return AppUser.query.filter_by(username=target).first() is not None


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
