"""Verified database snapshots, and the loop that takes them unattended.
[Phase 10.6 — closes the "back up the volume" gap in docs/deploy-railway.md]

Allowed:   flask.current_app, sqlite3, threading, stdlib
Must not:  app, models, render_template/url_for/jsonify, blueprints

No `models` import, and that is not incidental. This module reads the database
as a *file*, through `sqlite3` directly, so it works on a database whose schema
this code no longer matches — which is exactly the situation a restore is for.
An ORM-mediated backup can only copy tables the running code declares, and would
silently drop a table left behind by a half-applied migration: the rows you most
need are the ones it would omit.

## Why the mechanics live here and not in `tools/`

They were in `tools/backup_db.py` and are unchanged; only their address moved.
`tools/backup_db.py` is now a CLI in front of this module, which is the
dependency direction that lets both the operator's command and the scheduled
loop run *the same code*. A backup path that only the CLI exercises is a backup
path nobody runs on the schedule that matters.

## What a snapshot is

`Connection.backup()`, never `shutil.copy`. A plain copy of a live SQLite file
can capture a torn page set if the sync scheduler thread commits mid-copy; the
backup API holds the right locks and produces a consistent snapshot without
stopping the application.

Every snapshot is verified before it is kept — `PRAGMA integrity_check`, plus a
row count per table matched against the source — and one that fails either check
is **deleted** rather than left on disk. A corrupt file that looks like a backup
is worse than no backup: it is the one you reach for at the worst moment.

## What this protects against, and what it does not

It covers corruption, a bad migration, and an accidental delete: the failures
where the volume is fine and the data on it is not.

It does **not** cover losing the volume. These snapshots sit beside the database
on the same disk, so a disk that goes takes both. Copying them off-host is still
a manual step (`railway volume files ... download`, per docs/deploy-railway.md)
and this module does not pretend otherwise — an off-site story is the next piece
of work, not something the schedule quietly implies.

It also does not cover `.sync_encryption_key`. That file is not in the database
and every stored institution credential is unreadable without it, so a backup
taken without it restores an application that cannot sync. The disaster-recovery
runbook makes this a step; it is repeated here because this module is what
somebody reads when they are deciding whether they are covered.

## One process

Same constraint as `finance_sync/scheduler.py`, for the same reason (OPS-0012):
the loop starts per process, so a second worker means two threads writing
snapshots into one directory and pruning each other's. The deployment is a
single worker by decision, and `--workers 2` breaks this the same way it breaks
sync.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger('dough.backup')

__all__ = [
    'BackupScheduler',
    'DEFAULT_KEEP',
    'backup',
    'backup_target',
    'get_backup_scheduler',
    'init_backup_scheduler',
    'install',
    'prune',
    'verify',
]

DEFAULT_KEEP = 7

#: Prefix of a SQLite SQLAlchemy URI. Both the three-slash (relative) and
#: four-slash (absolute) forms strip with this one rule: what remains is the
#: path, leading slash included when there was a fourth.
_SQLITE_PREFIX = 'sqlite:///'


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

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


class BackupError(RuntimeError):
    """A backup did not happen, or happened and did not verify.

    An exception rather than a return code because both callers must not carry
    on as if there were a snapshot: the CLI turns it into a non-zero exit, and
    the scheduler logs it at ERROR. A backup that failed quietly is the same
    thing as no backup with a false sense of one.
    """


def backup(db_path: str, backup_dir: str, *, label: str | None = None) -> str:
    """Take one verified snapshot. Returns the path it was written to.

    Does not prune. Retention is the caller's call and a separate step, so that
    a snapshot which verified is on disk before anything is deleted — the other
    order can, on a bad day, remove the oldest backup to make room for one that
    then fails to verify and is itself removed.
    """
    if not os.path.exists(db_path):
        raise BackupError(f'No database at {db_path}')

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
        raise BackupError(
            f'Backup verification failed; {dest_path} deleted. '
            + '; '.join(problems))

    return dest_path


def describe(dest_path: str) -> str:
    """One line about a written snapshot, for a log or a terminal."""
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    conn = sqlite3.connect(f'file:{dest_path}?mode=ro', uri=True)
    try:
        counts = _row_counts(conn)
    finally:
        conn.close()
    return (f'{dest_path} ({size_mb:.2f} MB, {len(counts)} tables, '
            f'{sum(counts.values())} rows)')


# ---------------------------------------------------------------------------
# Where the snapshots go
# ---------------------------------------------------------------------------

def backup_target(config) -> tuple[Optional[str], Optional[str]]:
    """Resolve `(db_path, backup_dir)` from configuration, or `(None, None)`.

    `None` means "there is nothing here to back up on a schedule", which is a
    normal state rather than an error: an in-memory test database, or a future
    Postgres URI whose backups belong to the database server and not to this
    process.

    **The directory defaults to one beside the database file, not to the repo
    root**, and that difference is the whole reason this function exists. In
    production the database is at `/data/checkbook.db` on a mounted volume while
    the code is in an image that a deploy replaces. A `backups/` directory
    resolved relative to the source tree would put every snapshot inside that
    image — present after each backup, gone at the next deploy, and gone in the
    same event that would make you want them. Beside the database, they are on
    the volume, which is the thing that persists.
    """
    uri = (config.get('SQLALCHEMY_DATABASE_URI') or '').strip()
    if not uri.startswith(_SQLITE_PREFIX):
        return None, None

    path = uri[len(_SQLITE_PREFIX):].split('?', 1)[0]
    if not path:
        # `sqlite://` — in-memory. Nothing on disk to snapshot.
        return None, None

    db_path = os.path.abspath(path)
    configured = (config.get('BACKUP_DIR') or '').strip()
    backup_dir = (os.path.abspath(configured) if configured
                  else os.path.join(os.path.dirname(db_path), 'backups'))
    return db_path, backup_dir


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

class BackupScheduler:
    """A daemon thread that snapshots the database every `interval_hours`.

    Deliberately not built on the sync scheduler. They share a shape and nothing
    else: sync talks to institutions over the network on behalf of one household
    at a time, and this copies a file for the whole deployment. Folding the
    backup into that loop would tie how often the data is protected to how often
    banks are polled, and would put "did the backup run" behind a per-household
    due-check that has nothing to do with it.
    """

    def __init__(self, app, interval_hours: float = 24.0,
                 keep: int = DEFAULT_KEEP):
        self.app = app
        self.interval_hours = interval_hours
        self.keep = keep
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._state = {
            'last_started': None,
            'last_finished': None,
            'last_status': None,
            'last_path': None,
            'last_error': None,
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the loop (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        db_path, backup_dir = backup_target(self.app.config)
        if db_path is None:
            logger.info('Backups not scheduled: database is not a file '
                        '(%s)', self.app.config.get('SQLALCHEMY_DATABASE_URI'))
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='dough-backup-scheduler', daemon=True)
        self._thread.start()
        logger.info('Backup scheduler started: every %sh, keeping %d, into %s',
                    self.interval_hours, self.keep, backup_dir)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # A snapshot shortly after startup, then every `interval`. The early one
        # is the point rather than a warm-up: a deployment that restarts more
        # often than the interval would otherwise never reach the first backup,
        # and "it restarts a lot" describes exactly the deployment you would
        # most like to have a recent snapshot of.
        if self._stop.wait(timeout=60):
            return
        while not self._stop.is_set():
            self.run_once(trigger='scheduled')
            if self._stop.wait(timeout=self.interval_hours * 3600):
                return

    # -- taking one ----------------------------------------------------------

    def run_once(self, trigger: str = 'manual') -> Optional[str]:
        """Take one verified snapshot. Returns its path, or None on failure.

        Never raises. It runs on a daemon thread whose death would be silent and
        permanent — the loop would stop and nothing would report that backups
        had ended — so every failure is caught, logged and left for the next
        interval to retry.
        """
        db_path, backup_dir = backup_target(self.app.config)
        if db_path is None:
            return None

        with self._state_lock:
            self._state['last_started'] = dt.datetime.utcnow().isoformat()
        try:
            dest = backup(db_path, backup_dir)
        except Exception as exc:
            logger.error('Backup failed (%s): %s', trigger, exc, exc_info=True)
            with self._state_lock:
                self._state['last_status'] = 'error'
                self._state['last_error'] = str(exc)
                self._state['last_finished'] = dt.datetime.utcnow().isoformat()
            return None

        removed = prune(backup_dir, os.path.splitext(os.path.basename(db_path))[0],
                        self.keep)
        logger.info('Backup ok (%s): %s%s', trigger, describe(dest),
                    f', pruned {len(removed)}' if removed else '')
        with self._state_lock:
            self._state['last_status'] = 'ok'
            self._state['last_error'] = None
            self._state['last_path'] = dest
            self._state['last_finished'] = dt.datetime.utcnow().isoformat()
        return dest

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)


#: Module-level singleton, installed by `app.create_app()`. Mirrors
#: `finance_sync.scheduler._scheduler` — one per process, not one per app —
#: because the thing being protected is the process's database file.
_scheduler: Optional[BackupScheduler] = None


def init_backup_scheduler(app, interval_hours: float = 24.0,
                          keep: int = DEFAULT_KEEP,
                          autostart: bool = True) -> BackupScheduler:
    """Create (or return) the process-wide backup scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackupScheduler(app, interval_hours=interval_hours,
                                     keep=keep)
        if autostart:
            _scheduler.start()
    return _scheduler


def get_backup_scheduler() -> Optional[BackupScheduler]:
    return _scheduler


def install(app) -> None:
    """Wire the scheduler into an application. Called by `create_app`.

    The `before_request` indirection is the same one `finance_sync` needs and
    for the same reason: the thread must belong to the process that serves
    requests, and starting it during `create_app` would also start it in the
    werkzeug reloader's parent, which then holds a second one.

    Registered separately from the sync scheduler rather than folded into its
    hook, so that `SYNC_AUTO_ENABLED=0` -- which a deployment may reasonably set
    while the process model is being settled -- does not also silently turn off
    backups. Those two switches answer different questions and a deployment
    should not discover they were one.
    """
    if not app.config.get('BACKUP_AUTO_ENABLED', True) or app.config.get('TESTING'):
        return

    @app.before_request
    def _ensure_backup_scheduler():
        init_backup_scheduler(
            app,
            interval_hours=app.config.get('BACKUP_INTERVAL_HOURS', 24),
            keep=app.config.get('BACKUP_KEEP', 7))
