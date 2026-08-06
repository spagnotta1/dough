"""Background synchronization scheduler.

Runs the SyncEngine in a daemon thread every ``SYNC_INTERVAL_HOURS`` (default
12) without ever blocking the UI. Also powers manual "refresh now" requests
from the API: those run on short-lived worker threads and the UI polls
:meth:`SyncScheduler.status`.

Tenancy  [Phase 5]
------------------
This module is the reason ``dough/tenancy.py`` uses a ``ContextVar`` rather
than ``flask.g``: none of the work below happens inside a request.

``ContextVar`` has a property that is doing real work here — **a new thread
starts with an empty context**, it does not inherit the parent's. So a worker
thread spawned by a request cannot silently keep serving whichever household
that request belonged to. It gets no household, and every scoped query raises
until one is bound deliberately.

That leaves two cases, and they bind the household from different places:

- **On demand** (a user pressing "refresh now"): the household is captured in
  the request that asked, and re-bound inside the worker.
- **Scheduled**: there is no caller, so the loop iterates every household and
  runs each one in its own scope. One household's broken bank must not stop the
  others from syncing, so failures are contained per household exactly as they
  already are per connection.

Automatic categorization  [UAT round 1]
---------------------------------------
Every completed sync hands off to ``_categorize``, which asks
``dough/services/auto_categorize.py`` to derive category rules from whatever
just arrived uncategorized and apply them. It lives here for the same reason
the sync loop does: it needs an app context, a tenant scope and a thread that
is not a request, and this class is the only thing in the codebase that owns
all three. The service itself stays synchronous and thread-unaware.

Two properties are load-bearing and are tested in
``tests/test_auto_categorize.py``: it runs **after** ``_busy`` is released, so
an LLM round trip never blocks an unrelated bank's refresh, and it **never
raises**, so a model outage cannot retroactively fail a sync that succeeded.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from dough.tenancy import current_household, tenant_scope, unscoped

from .engine import SyncEngine

logger = logging.getLogger("finance_sync.scheduler")


class SyncScheduler:
    """Owns the periodic sync loop and on-demand background syncs."""

    def __init__(self, app, interval_hours: float = 12.0):
        self.app = app
        self.interval = timedelta(hours=interval_hours)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = threading.Lock()   # one sync at a time
        self._state_lock = threading.Lock()
        self._state = {
            "running": False,
            "last_started": None,
            "last_finished": None,
            "last_trigger": None,
            "last_status": None,
            "next_scheduled": None,
            # Automatic categorization, reported separately from the sync so
            # the UI can say which of the two it is waiting on. [UAT round 1]
            # It runs after the sync's lock is released, so "sync finished,
            # still categorizing" is a real state the page has to render.
            "categorizing": False,
            "last_categorization": None,
        }

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the periodic loop (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="finance-sync-scheduler", daemon=True)
        self._thread.start()
        logger.info("Background sync scheduler started (every %s)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # First pass shortly after startup so a stale local DB catches up,
        # then steady-state every `interval`.
        if self._stop.wait(timeout=10):
            return
        while not self._stop.is_set():
            for household_id in self._households():
                if self._stop.is_set():
                    return
                if self._due_for_scheduled_sync(household_id):
                    # One household at a time, each in its own scope. A bank
                    # that is down for one family must not stop another family
                    # syncing, so this is deliberately not one run over
                    # everything.
                    self.run_sync(trigger="scheduled", wait=True,
                                  household_id=household_id)
            with self._state_lock:
                self._state["next_scheduled"] = (
                    datetime.utcnow() + self.interval).isoformat()
            if self._stop.wait(timeout=self.interval.total_seconds()):
                return

    def _households(self) -> List[int]:
        """Every household with something to sync.

        `unscoped()` because `Household` is the tenant registry rather than
        tenant data — enumerating it is the one read that legitimately spans
        households, and it is what a scheduled run has instead of a caller.
        """
        from models import Household
        with self.app.app_context():
            with unscoped():
                return [h.id for h in Household.query.order_by(Household.id).all()]

    def _due_for_scheduled_sync(self, household_id: int) -> bool:
        """Skip the startup pass when this household synced within the interval.

        Per household, not global: with one shared answer the first household
        in the list would satisfy the check for everybody, and every other
        household would skip its startup sync forever.
        """
        from models import SyncRun
        with self.app.app_context():
            with tenant_scope(household_id):
                last = (SyncRun.query.filter(SyncRun.status != "running")
                        .order_by(SyncRun.started_at.desc()).first())
                if last is None:
                    return True
                return datetime.utcnow() - last.started_at >= self.interval

    # -- on-demand syncs ---------------------------------------------------------

    def run_sync(self, trigger: str = "manual",
                 connection_id: Optional[int] = None,
                 wait: bool = False,
                 queue: bool = False,
                 household_id: Optional[int] = None) -> bool:
        """Run a sync (all connections, or one) for one household.

        Only one sync runs at a time. If one is already in progress the call
        returns ``False`` — unless ``queue=True``, in which case the sync
        waits its turn on a background thread (used for the initial sync of a
        freshly connected institution, so rapid connects never drop a sync).

        With ``wait=False`` (API default) the sync runs on a background thread
        and the caller polls :meth:`status` — the UI is never blocked.

        ``household_id`` defaults to whichever household is bound in the calling
        context, which is what makes the API routes need no change: they are
        already running inside the request's scope. It is captured **here**,
        while the caller's context still exists, and not inside ``_work`` —
        by the time ``_work`` runs on its own thread there is no context left
        to read it from.
        """
        wait = wait or bool(self.app.config.get("SYNC_SYNCHRONOUS"))
        if household_id is None:
            household_id = current_household()
        if household_id is None:
            # Nothing sensible left to do: a sync with no household would either
            # raise on its first query or, if the backstop were ever relaxed,
            # write one family's transactions into another's ledger.
            raise RuntimeError(
                "run_sync needs a household: call it from a request, or pass "
                "household_id explicitly as the scheduled loop does.")

        def _work(pre_acquired: bool) -> None:
            if not pre_acquired:
                self._busy.acquire()  # queued: wait for the in-flight sync
            with self._state_lock:
                self._state["running"] = True
                self._state["last_started"] = datetime.utcnow().isoformat()
                self._state["last_trigger"] = trigger
            try:
                with self.app.app_context():
                    with tenant_scope(household_id):
                        engine = SyncEngine()
                        if connection_id is not None:
                            result = engine.sync_connection(connection_id,
                                                            trigger=trigger)
                        else:
                            result = engine.sync_all(trigger=trigger)
                        with self._state_lock:
                            self._state["last_status"] = result.status
            except Exception:
                logger.exception("Background sync crashed")
                with self._state_lock:
                    self._state["last_status"] = "error"
            finally:
                with self._state_lock:
                    self._state["running"] = False
                    self._state["last_finished"] = datetime.utcnow().isoformat()
                self._busy.release()
            # After the lock, deliberately. Categorizing is model calls over a
            # network and can take longer than the sync that triggered it;
            # holding `_busy` for it would make an unrelated bank's refresh
            # queue behind an LLM round trip. A sync that failed outright is
            # skipped -- there is nothing new to read.
            with self._state_lock:
                sync_status = self._state["last_status"]
            if sync_status != "error":
                self._categorize(household_id)

        pre_acquired = self._busy.acquire(blocking=False)
        if not pre_acquired and not queue:
            return False
        if wait:
            _work(pre_acquired)
        else:
            threading.Thread(target=_work, args=(pre_acquired,),
                             name=f"finance-sync-{trigger}", daemon=True).start()
        return True

    # -- automatic categorization ------------------------------------------------

    def _categorize(self, household_id: int) -> None:
        """Derive and apply category rules for whatever the sync just imported.

        ## Why this lives in the scheduler  [UAT round 1]

        The analysis itself is `dough/services/auto_categorize.py`, which is
        synchronous and knows nothing about threads, apps or households — the
        same constraint every other service in that package is written under.
        What it needs and cannot have is a caller: an app context, a tenant
        scope, and a thread that is not a request. This class already owns all
        three for exactly this reason, so the trigger belongs here rather than
        inside `SyncEngine` (which must stay institution-agnostic and offline)
        or inside a route (there is no request behind a scheduled sync).

        ## It never raises

        Categorization is an enhancement to a sync that has already succeeded.
        A model outage, a malformed reply or a rate limit must leave the freshly
        imported transactions exactly where they are — uncategorized, which is
        the state they would have been in anyway — and must never mark the sync
        as failed. Every failure path here is a log line.
        """
        from dough.services import auto_categorize

        with self._state_lock:
            self._state["categorizing"] = True
        try:
            with self.app.app_context():
                with tenant_scope(household_id):
                    result = auto_categorize.run_once()
            if result.skipped:
                logger.info("Auto-categorization skipped for household %s: %s",
                            household_id, result.reason)
            else:
                logger.info("Auto-categorization for household %s: %d rules, "
                            "%d transactions categorized, %d still uncategorized",
                            household_id, result.rules_added,
                            result.transactions_categorized,
                            result.remaining_uncategorized)
            with self._state_lock:
                self._state["last_categorization"] = {
                    "at": datetime.utcnow().isoformat(),
                    "rules_added": result.rules_added,
                    "transactions_categorized": result.transactions_categorized,
                    "remaining_uncategorized": result.remaining_uncategorized,
                    "partial": result.partial,
                    "skipped": result.skipped,
                }
        except Exception:
            logger.exception("Auto-categorization crashed for household %s",
                             household_id)
        finally:
            with self._state_lock:
                self._state["categorizing"] = False

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)


# Module-level singleton, installed by app.create_app().
_scheduler: Optional[SyncScheduler] = None


def init_scheduler(app, interval_hours: float = 12.0,
                   autostart: bool = True) -> SyncScheduler:
    """Create (or return) the process-wide scheduler for this app."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SyncScheduler(app, interval_hours=interval_hours)
        if autostart:
            _scheduler.start()
    return _scheduler


def get_scheduler() -> Optional[SyncScheduler]:
    return _scheduler
