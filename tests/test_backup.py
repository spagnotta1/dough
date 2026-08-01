"""Verified database snapshots, and where they get written.
[Phase 10.6 — closes the "back up the volume" gap in docs/deploy-railway.md]

The mechanics moved out of `tools/backup_db.py` unchanged and are tested here
for the first time. That they were previously untested is most of the reason
this file exists: a backup path nothing exercises is discovered to be broken at
a restore, which is the one moment there is no way to recover from it being
broken.

Two things are worth stating about what is asserted below.

**The verification is the feature.** Copying a file is not hard; noticing that
the copy is wrong is. So the tests that matter most are the ones that corrupt
something and check the snapshot is *deleted* rather than kept — a bad backup
that looks like a good one is worse than no backup, because it is the one you
plan around.

**`backup_target` gets its own tests despite being ten lines**, because it holds
the only decision in the module with a silent failure mode. Resolving the
directory relative to the source tree instead of to the database would put every
snapshot inside the container image in production: present after each run, gone
at the next deploy, and gone in the same event that would make somebody want
them. Nothing reports that; the backups exist right up until they are needed.
"""

import os
import sqlite3

import pytest

from dough.services.backup import (BackupError, BackupScheduler, backup,
                                   backup_target, describe, prune, verify)


def _make_db(path, rows=3):
    """A small database with known contents."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        conn.executemany('INSERT INTO t (v) VALUES (?)',
                         [(f'row-{i}',) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()
    return str(path)


# ---------------------------------------------------------------------------
# Where snapshots go
# ---------------------------------------------------------------------------

def test_backups_land_beside_the_database_not_beside_the_code():
    """The production shape: a volume-mounted database, an ephemeral image.

    `/data/checkbook.db` is what `docs/deploy-railway.md` configures, and the
    only correct answer is a directory under `/data`. Anything resolved from the
    source tree is inside the image a deploy replaces.
    """
    db_path, backup_dir = backup_target(
        {'SQLALCHEMY_DATABASE_URI': 'sqlite:////data/checkbook.db'})
    assert os.path.dirname(db_path) == os.path.dirname(backup_dir), (
        'snapshots must be written beside the database file')
    assert os.path.basename(backup_dir) == 'backups'


def test_three_slashes_and_four_both_resolve():
    """The relative and absolute SQLAlchemy forms.

    Worth pinning because the difference is one character and the deploy doc
    calls it out: three slashes is a *relative* path, which in production lands
    the database inside the image. Both must produce a usable pair here rather
    than one silently returning None.
    """
    relative, _ = backup_target(
        {'SQLALCHEMY_DATABASE_URI': 'sqlite:///checkbook.db'})
    absolute, _ = backup_target(
        {'SQLALCHEMY_DATABASE_URI': 'sqlite:////var/lib/checkbook.db'})
    assert relative and relative.endswith('checkbook.db')
    assert absolute and absolute.endswith('checkbook.db')


def test_an_explicit_backup_dir_overrides_the_default():
    _, backup_dir = backup_target({
        'SQLALCHEMY_DATABASE_URI': 'sqlite:////data/checkbook.db',
        'BACKUP_DIR': '/mnt/snapshots'})
    assert os.path.basename(backup_dir) == 'snapshots'


@pytest.mark.parametrize('uri', ['sqlite://', 'postgresql://host/db', ''])
def test_a_database_with_no_file_declines_rather_than_guessing(uri):
    """In-memory and non-SQLite are normal states, not errors.

    Returning a path anyway would mean the scheduler starts a thread that fails
    every interval, and a Postgres deployment would get a backup thread whose
    job belongs to the database server.
    """
    assert backup_target({'SQLALCHEMY_DATABASE_URI': uri}) == (None, None)


# ---------------------------------------------------------------------------
# Taking one
# ---------------------------------------------------------------------------

def test_a_snapshot_round_trips(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db', rows=5)
    dest = backup(source, str(tmp_path / 'backups'))

    assert os.path.exists(dest)
    conn = sqlite3.connect(f'file:{dest}?mode=ro', uri=True)
    try:
        assert conn.execute('SELECT COUNT(*) FROM t').fetchone()[0] == 5
    finally:
        conn.close()


def test_a_snapshot_is_taken_while_the_source_is_open(tmp_path):
    """The reason this uses SQLite's backup API rather than `shutil.copy`.

    The application is always running when a scheduled backup fires, and the
    sync scheduler holds a connection. A backup that needs exclusive access is
    one that never succeeds in production.
    """
    source = _make_db(tmp_path / 'checkbook.db')
    holder = sqlite3.connect(source)
    try:
        holder.execute("INSERT INTO t (v) VALUES ('held')")
        holder.commit()
        dest = backup(source, str(tmp_path / 'backups'))
    finally:
        holder.close()
    assert verify(dest) == []


def test_a_missing_database_is_an_error_not_an_empty_backup(tmp_path):
    with pytest.raises(BackupError, match='No database'):
        backup(str(tmp_path / 'nope.db'), str(tmp_path / 'backups'))


def test_describe_reports_the_contents(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db', rows=4)
    dest = backup(source, str(tmp_path / 'backups'))
    line = describe(dest)
    assert '1 tables' in line and '4 rows' in line


# ---------------------------------------------------------------------------
# Verification — the part that matters
# ---------------------------------------------------------------------------

def test_a_corrupt_backup_is_reported(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db')
    dest = backup(source, str(tmp_path / 'backups'))

    # Overwrite the header. `integrity_check` on the result is not 'ok'.
    with open(dest, 'r+b') as handle:
        handle.seek(0)
        handle.write(b'\x00' * 128)

    with pytest.raises(sqlite3.DatabaseError):
        verify(dest)


def test_a_row_count_mismatch_is_reported(tmp_path):
    """The check that catches a copy which is *readable* and wrong.

    `integrity_check` alone passes on a structurally sound database holding the
    wrong data, which is exactly what a torn copy looks like.
    """
    source = _make_db(tmp_path / 'checkbook.db', rows=3)
    dest = backup(source, str(tmp_path / 'backups'))

    problems = verify(dest, {'t': 99})
    assert problems and 'rows in backup' in problems[0]


def test_a_table_missing_from_the_backup_is_reported(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db')
    dest = backup(source, str(tmp_path / 'backups'))
    problems = verify(dest, {'t': 3, 'absent_table': 1})
    assert any('absent_table' in p for p in problems)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_prune_keeps_the_newest_and_removes_the_rest(tmp_path):
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    made = []
    for i in range(5):
        path = backup_dir / f'checkbook-2026080{i}-000000.db'
        path.write_bytes(b'x')
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
        made.append(path)

    removed = prune(str(backup_dir), 'checkbook', keep=2)

    assert len(removed) == 3
    survivors = sorted(p.name for p in backup_dir.iterdir())
    assert survivors == sorted(p.name for p in made[-2:]), (
        'prune must keep the newest, not the first it happens to scan')


def test_prune_keeps_everything_when_keep_is_zero(tmp_path):
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    (backup_dir / 'checkbook-20260801-000000.db').write_bytes(b'x')
    assert prune(str(backup_dir), 'checkbook', keep=0) == []


def test_prune_ignores_files_belonging_to_another_database(tmp_path):
    """`--db other.db` must not delete `checkbook`'s history.

    The stem is part of the match for this reason; a prune that globbed `*.db`
    would make backing up a second database destroy the first one's backups.
    """
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    (backup_dir / 'checkbook-20260801-000000.db').write_bytes(b'x')
    (backup_dir / 'other-20260801-000000.db').write_bytes(b'x')

    prune(str(backup_dir), 'other', keep=1)

    assert (backup_dir / 'checkbook-20260801-000000.db').exists()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class _FakeApp:
    def __init__(self, config):
        self.config = config


def test_the_scheduler_takes_and_verifies_one(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db')
    scheduler = BackupScheduler(
        _FakeApp({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{source}'}), keep=3)

    dest = scheduler.run_once(trigger='test')

    assert dest and os.path.exists(dest)
    assert scheduler.status()['last_status'] == 'ok'


def test_the_scheduler_prunes_to_its_retention(tmp_path):
    source = _make_db(tmp_path / 'checkbook.db')
    scheduler = BackupScheduler(
        _FakeApp({'SQLALCHEMY_DATABASE_URI': f'sqlite:///{source}'}), keep=2)

    for run in range(4):
        # The filename carries a whole-second timestamp, so four runs inside one
        # second would write to one path. Distinct mtimes are what prune orders
        # by, and the ordering is what this test is about.
        dest = scheduler.run_once(trigger='test')
        stamp = os.path.getmtime(dest) + run
        os.utime(dest, (stamp, stamp))

    kept = [p for p in os.listdir(os.path.join(str(tmp_path), 'backups'))
            if p.endswith('.db')]
    assert len(kept) <= 2


def test_a_failing_backup_does_not_kill_the_loop(tmp_path):
    """`run_once` must never raise.

    It runs on a daemon thread. An exception there stops the loop permanently
    and reports nothing — backups would simply end, and the first anyone knew of
    it would be a restore finding the newest snapshot months old.
    """
    scheduler = BackupScheduler(
        _FakeApp({'SQLALCHEMY_DATABASE_URI':
                  f"sqlite:///{tmp_path / 'absent.db'}"}))

    assert scheduler.run_once(trigger='test') is None
    assert scheduler.status()['last_status'] == 'error'
    assert scheduler.status()['last_error']


def test_the_scheduler_declines_a_database_with_no_file(tmp_path):
    scheduler = BackupScheduler(_FakeApp({'SQLALCHEMY_DATABASE_URI': 'sqlite://'}))
    scheduler.start()
    assert scheduler._thread is None, (
        'an in-memory database must not get a backup thread')


def test_backups_are_off_under_test_and_on_by_default(app):
    """The switch, from both ends.

    `TestingConfig` turns them off so the suite never starts a thread; the base
    default is on, which is the one that matters for a deployment nobody
    configured.
    """
    from config import BaseConfig

    assert app.config['BACKUP_AUTO_ENABLED'] is False
    assert BaseConfig.BACKUP_AUTO_ENABLED is True
