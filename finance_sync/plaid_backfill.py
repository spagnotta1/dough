"""Getting a linked Item's *whole* transaction history in, not just the first page.

The bug this exists for  [UAT round 1]
--------------------------------------
Two testers linked the same institution through Plaid on the same day. One
received two years of transactions; the other received one month. No error was
raised at any point, for either of them, and both connections showed
`connected` / last sync `success`.

The cause is a race nobody can win by trying harder. `POST /api/connections/
plaid/exchange` starts a sync the instant the public-token exchange returns,
and at that moment Plaid has usually finished only its initial ~30-day pull;
the 730 days `create_link_token` asks for are still being fetched on Plaid's
side. `transactions/sync` answers with what exists so far and sets
``has_more: false``, which means "nothing more is ready **right now**" and not
"this Item's history is complete" — the distinction the original code read the
wrong way. Whether a tester got 30 days or 730 came down to how fast their
bank's backfill finished relative to our own connect-sync, which for a small
institution can be many minutes.

The connect-sync is still worth doing: a user who just linked a bank should see
*something* immediately. What was missing is the part that comes back for the
rest.

Two mechanisms, because one of them is optional
-----------------------------------------------
**The webhook is the real fix.** Plaid tells us when the historical update has
landed, we sync once, and the history is complete within seconds of it being
available. `handle_webhook` is that path. It needs `PLAID_WEBHOOK_URL` set to a
publicly reachable https URL, which a local development install will not have.

**The watcher is the backstop.** A thread that re-syncs the connection on a
widening schedule until the history looks complete. It exists because webhook
delivery is not guaranteed — a deploy, a restart, or a dropped POST loses the
notification permanently, and a lost notification with no backstop is exactly
the silent truncation above, back again. It is also the only mechanism at all
on a deployment with no webhook URL.

Both converge on `history_status` (see `models.InstitutionConnection`), and
either one reaching `complete` stops the other doing more work.

Why "complete" is inferred rather than asked
--------------------------------------------
Plaid has no endpoint that answers "is this Item's backfill finished?" — the
webhook is genuinely the only authoritative signal, and the watcher runs
precisely when that signal did not arrive. So `_settled` judges from what we
can see: transactions materially older than the initial pull window (the
backfill landed), or two consecutive passes that imported nothing new (it has
stopped moving). Neither is a proof, and the watcher does not pretend
otherwise: a schedule that runs out without either says `partial`, not
`complete`.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from typing import Dict, Optional

from dough.tenancy import tenant_scope, unscoped

logger = logging.getLogger("finance_sync.plaid_backfill")

IMPORTING = "importing"
COMPLETE = "complete"
PARTIAL = "partial"

# When to come back and ask again, in seconds after the connect-sync: a minute,
# five, fifteen, an hour, three hours. Front-loaded because most institutions
# finish in the first few minutes, and stretched at the end because the ones
# that do not are usually small banks that take a long time — the case that
# produced the original report.
RETRY_SCHEDULE = (60, 300, 900, 3600, 10800)

# How much older than the initial pull a transaction has to be before its
# presence is taken as proof the backfill landed. Plaid's initial update is
# ~30 days; 60 leaves room for that to vary without ever being satisfied by an
# initial pull alone.
_BACKFILL_HORIZON_DAYS = 60

# One watcher per connection. Re-linking an Item, or a webhook arriving while a
# watcher is already running, must not start a second thread syncing the same
# connection on its own schedule.
_watchers: Dict[int, threading.Event] = {}
_watchers_lock = threading.Lock()


# ---------------------------------------------------------------------------
# History status
# ---------------------------------------------------------------------------

def set_history_status(app, connection_id: int, household_id: int,
                       status: str) -> None:
    """Record how much of `connection_id`'s history has arrived.

    Takes `household_id` explicitly because every caller here is a background
    thread or a webhook — neither has a request to inherit a tenant scope from.
    """
    from models import InstitutionConnection, db
    with app.app_context():
        with tenant_scope(household_id):
            connection = InstitutionConnection.query.filter_by(
                id=connection_id).first()
            if connection is None or connection.history_status == status:
                return
            connection.history_status = status
            db.session.commit()
            logger.info("Connection %s history is now %s", connection_id, status)


def _oldest_transaction(connection) -> Optional[date]:
    """The earliest transaction date across this connection's accounts."""
    from models import Transaction, db
    account_ids = [a.id for a in connection.accounts]
    if not account_ids:
        return None
    return db.session.query(db.func.min(Transaction.date)).filter(
        Transaction.account_id.in_(account_ids)).scalar()


def _settled(connection, quiet_passes: int) -> bool:
    """Has this Item's history stopped growing?

    Two independent signals, either of which is enough — see the module
    docstring for why neither is a proof and why that is accepted here:

    - **Depth.** A transaction older than the initial-pull window can only have
      come from the historical backfill, so the backfill ran.
    - **Stillness.** Two consecutive passes importing nothing new. This is what
      answers for a genuinely young account, which has no old transactions to
      find and would otherwise wait out the whole schedule to be told its
      complete history is `partial`.
    """
    oldest = _oldest_transaction(connection)
    if oldest is None:
        # Nothing has arrived at all, so there is nothing to be still *about*.
        # Without this, an Item answering PRODUCT_NOT_READY -- the state a
        # brand-new connection is most likely to be in -- would look identical
        # to one that had finished, and two quiet passes would declare an empty
        # ledger complete. Stillness only means something once something moved.
        return False
    if oldest < date.today() - timedelta(days=_BACKFILL_HORIZON_DAYS):
        return True
    return quiet_passes >= 2


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

def watch(app, connection_id: int, household_id: int,
          schedule=RETRY_SCHEDULE) -> Optional[threading.Thread]:
    """Re-sync `connection_id` on a widening schedule until its history settles.

    Returns the thread (or None if one is already watching this connection, or
    the app has watchers switched off). Daemon, like every other thread this
    package starts: an interpreter shutting down must not wait on a sleep that
    has three hours left to run.
    """
    if not app.config.get("PLAID_BACKFILL_ENABLED", True):
        return None
    with _watchers_lock:
        if connection_id in _watchers:
            return None
        stop = threading.Event()
        _watchers[connection_id] = stop

    set_history_status(app, connection_id, household_id, IMPORTING)

    thread = threading.Thread(
        target=_watch_loop, args=(app, connection_id, household_id, schedule, stop),
        name=f"plaid-backfill-{connection_id}", daemon=True)
    thread.start()
    return thread


def stop_watching(connection_id: int) -> None:
    """Cancel a running watcher — the connection was disconnected, or a webhook
    already confirmed the history is in and the remaining syncs would be waste."""
    with _watchers_lock:
        stop = _watchers.get(connection_id)
    if stop is not None:
        stop.set()


def _watch_loop(app, connection_id: int, household_id: int, schedule,
                stop: threading.Event) -> None:
    from models import InstitutionConnection, SyncRun
    from .scheduler import get_scheduler

    quiet_passes = 0
    settled = False
    try:
        for delay in schedule:
            if stop.wait(timeout=delay):
                return  # disconnected, or a webhook got there first
            scheduler = get_scheduler()
            if scheduler is None:
                # Nothing left that can sync. Fall through to `partial` rather
                # than returning: leaving the connection claiming to be
                # mid-import forever is the one outcome worse than admitting we
                # do not know.
                break

            # queue=True so this never loses a turn to a sync that happens to be
            # running; wait=True because we are already on our own thread and the
            # decision below needs the result, not a promise of one.
            scheduler.run_sync(trigger="backfill", connection_id=connection_id,
                               queue=True, wait=True, household_id=household_id)

            with app.app_context():
                with tenant_scope(household_id):
                    connection = InstitutionConnection.query.filter_by(
                        id=connection_id).first()
                    if connection is None:
                        return  # disconnected mid-wait
                    if connection.history_status == COMPLETE:
                        return  # a webhook answered while the sync ran
                    # `id` breaks the tie: two passes a hundredth of a second
                    # apart can share a timestamp at SQLite's resolution, and
                    # reading the wrong one would miscount a quiet pass.
                    run = (SyncRun.query
                           .filter_by(connection_id=connection_id, trigger="backfill")
                           .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
                           .first())
                    imported = (run.transactions_added or 0) if run else 0
                    quiet_passes = quiet_passes + 1 if imported == 0 else 0
                    settled = _settled(connection, quiet_passes)
            if settled:
                break

        # `partial` when the schedule ran out with the history still moving --
        # deliberately not `complete`. Everything imported is real; we just
        # stopped being able to promise it is everything, and saying so is what
        # lets the user press Refresh instead of quietly missing a year.
        set_history_status(app, connection_id, household_id,
                           COMPLETE if settled else PARTIAL)
    except Exception:
        # A crashed watcher must not take the process with it, and must not
        # leave the connection claiming to be mid-import forever.
        logger.exception("Backfill watcher failed for connection %s", connection_id)
        set_history_status(app, connection_id, household_id, PARTIAL)
    finally:
        with _watchers_lock:
            _watchers.pop(connection_id, None)


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------

# Plaid's TRANSACTIONS webhooks that mean "there is something new to fetch".
# SYNC_UPDATES_AVAILABLE is the one that matters for this bug: it carries
# `historical_update_complete`, the only authoritative answer to "is the
# backfill done?" that Plaid provides anywhere.
_SYNC_CODES = ("SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE", "HISTORICAL_UPDATE",
               "DEFAULT_UPDATE")

# ITEM webhooks meaning the user has to go back through Link. Handled here
# because the alternative is discovering it at the next scheduled sync, up to
# twelve hours later, having shown a stale balance the whole time.
_REAUTH_CODES = ("ERROR", "PENDING_EXPIRATION", "USER_PERMISSION_REVOKED",
                 "LOGIN_REPAIRED")


def handle_webhook(app, payload: dict) -> str:
    """Act on one verified Plaid webhook. Returns a short label for the log.

    Never raises: Plaid retries on a non-2xx, and a webhook we cannot make
    sense of will not start making sense on the third delivery. The caller
    answers 200 to everything that verified.

    Resolving the household
    -----------------------
    A webhook has no session, no cookie and no caller — only an opaque
    `item_id`. So the connection is looked up `unscoped()` and the household is
    read off the row it finds, which is the same shape as the scheduled sync
    loop enumerating households: work with no request behind it, binding a
    tenant scope deliberately before it touches anything. The lookup key comes
    from Plaid rather than from a user, and `item_id` is unguessable, but the
    scope below is what actually contains the request either way.
    """
    from models import InstitutionConnection

    item_id = payload.get("item_id")
    code = payload.get("webhook_code")
    webhook_type = payload.get("webhook_type")
    if not item_id:
        return "ignored: no item_id"

    with app.app_context():
        with unscoped():
            connection = InstitutionConnection.query.filter_by(
                institution="plaid", item_id=item_id).first()
            if connection is None:
                # Not ours: a stale Item from another deployment sharing these
                # Plaid credentials, or one disconnected between the event and
                # its delivery.
                return "ignored: unknown item"
            connection_id = connection.id
            household_id = connection.household_id

    if webhook_type == "TRANSACTIONS" and code in _SYNC_CODES:
        return _handle_transactions_update(app, payload, connection_id, household_id)
    if webhook_type == "ITEM" and code in _REAUTH_CODES:
        return _handle_item_status(app, payload, connection_id, household_id, code)
    return f"ignored: {webhook_type}/{code}"


def _handle_transactions_update(app, payload: dict, connection_id: int,
                                household_id: int) -> str:
    from .scheduler import get_scheduler

    historical_done = bool(payload.get("historical_update_complete"))
    if payload.get("webhook_code") == "HISTORICAL_UPDATE":
        # The older non-sync webhook says the same thing by existing at all.
        historical_done = True

    scheduler = get_scheduler()
    if scheduler is not None:
        # queue=True, wait=False: return to Plaid promptly. A webhook handler
        # that blocks on a full sync is a webhook handler that times out and
        # gets redelivered, syncing the same Item twice.
        scheduler.run_sync(trigger="webhook", connection_id=connection_id,
                           queue=True, household_id=household_id)

    if historical_done:
        # Authoritative, so the watcher's remaining passes are pure waste.
        # Ordered after the sync is queued, not before: the status describes
        # what Plaid holds, and the sync that fetches it is already in the queue.
        set_history_status(app, connection_id, household_id, COMPLETE)
        stop_watching(connection_id)
        return "synced: history complete"
    return "synced"


def _handle_item_status(app, payload: dict, connection_id: int,
                        household_id: int, code: str) -> str:
    from models import InstitutionConnection, db

    error_code = (payload.get("error") or {}).get("error_code")
    needs_reauth = code in ("PENDING_EXPIRATION", "USER_PERMISSION_REVOKED") or (
        code == "ERROR" and error_code == "ITEM_LOGIN_REQUIRED")

    with app.app_context():
        with tenant_scope(household_id):
            connection = InstitutionConnection.query.filter_by(
                id=connection_id).first()
            if connection is None:
                return "ignored: unknown item"
            if code == "LOGIN_REPAIRED":
                connection.status = "connected"
                connection.last_error = None
            elif needs_reauth:
                connection.status = "expired"
                # The user-facing sentence, not Plaid's error code. This lands
                # on the Connections page verbatim.
                connection.last_error = (
                    "Your bank needs you to sign in again — reconnect to keep "
                    "this account syncing.")
            else:
                return f"ignored: ITEM/{code}"
            db.session.commit()

    if code == "LOGIN_REPAIRED":
        # The user fixed it at the bank without coming back through Link, so
        # nothing else would tell us to try again until the next scheduled run.
        from .scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler is not None:
            scheduler.run_sync(trigger="webhook", connection_id=connection_id,
                               queue=True, household_id=household_id)
        return "reconnected"
    return "reauth required"


# ---------------------------------------------------------------------------
# Startup: bring already-linked Items up to the same standard
# ---------------------------------------------------------------------------

def install(app) -> Optional[threading.Thread]:
    """One-shot pass over Plaid connections that predate this module.

    Two jobs, both only relevant to Items linked before the deployment had any
    of this:

    - **Register the webhook.** `create_link_token` sets it for every Item
      linked from now on, but an existing Item's webhook can only be changed
      with an explicit call. Without this, the fix would reach exactly the
      people who happen to re-link — and the testers who reported the bug are
      by definition not those people.
    - **Adopt connections with no history status.** NULL means "we have never
      asked", which is true of every existing row and is also the state a
      truncated connection is in. Starting a watcher answers the question. For
      a connection that is already complete this costs one sync that imports
      nothing, twice, and then settles; for a truncated one it recovers the
      missing history without anybody having to notice and press Refresh.

    Runs on a background thread because it makes a network call per connection
    and this is called during app setup — a slow Plaid response must not hold
    up the first request. Returns the thread so tests can join it.
    """
    if not app.config.get("PLAID_BACKFILL_ENABLED", True):
        return None
    thread = threading.Thread(target=_install_pass, args=(app,),
                              name="plaid-backfill-install", daemon=True)
    thread.start()
    return thread


def _install_pass(app) -> None:
    from models import InstitutionConnection

    try:
        with app.app_context():
            with unscoped():
                # Read to plain tuples inside the scope: the work below binds a
                # *different* tenant scope per connection, and detached ORM
                # instances read across that boundary are exactly the confusion
                # `tenant_scope` exists to prevent.
                rows = [(c.id, c.household_id, c.history_status)
                        for c in InstitutionConnection.query.filter_by(
                            institution="plaid")
                        .filter(InstitutionConnection.status != "disconnected").all()]
    except Exception:
        logger.exception("Could not enumerate Plaid connections at startup")
        return

    webhook_url = _webhook_url()
    for connection_id, household_id, history_status in rows:
        if webhook_url:
            _register_webhook(app, connection_id, household_id, webhook_url)
        if history_status is None:
            watch(app, connection_id, household_id)


def _webhook_url() -> str:
    from .adapters.plaid_adapter import PlaidAdapter
    return PlaidAdapter.webhook_url()


def _register_webhook(app, connection_id: int, household_id: int,
                      url: str) -> None:
    """Point one existing Item at our webhook. Best effort, by design.

    A failure here costs the watcher's retries instead of the webhook's
    promptness — degraded, not broken — so it must not stop the rest of the
    pass or fail startup.
    """
    from models import InstitutionConnection
    from .adapters import get_adapter_class
    from .crypto import TokenCipher

    try:
        with app.app_context():
            with tenant_scope(household_id):
                connection = InstitutionConnection.query.filter_by(
                    id=connection_id).first()
                if connection is None or not connection.auth_blob:
                    return
                credentials = TokenCipher().decrypt(connection.auth_blob)
        adapter = get_adapter_class("plaid")(credentials=credentials)
        adapter.update_webhook(url)
        logger.info("Registered Plaid webhook for connection %s", connection_id)
    except Exception:
        logger.warning("Could not register Plaid webhook for connection %s",
                       connection_id, exc_info=True)
