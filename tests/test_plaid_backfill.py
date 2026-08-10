"""Reclaiming a Plaid Item's history when the first sync only got part of it.

The bug, from UAT round 1: two testers linked the same institution, one got two
years of transactions and the other got one month, and nothing anywhere said
so. `finance_sync/plaid_backfill.py` has the full account; these are the tests
that pin the behaviour it added.

The watcher is a sleeping thread by nature, so every test here drives it with a
schedule measured in hundredths of a second and joins it. `PLAID_BACKFILL_ENABLED`
is off in `TestingConfig` precisely so that no *other* test acquires one of
these threads by accident; each test below turns it on deliberately.
"""

import threading
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from finance_sync import plaid_backfill
from finance_sync.adapters.plaid_adapter import PlaidAdapter
from finance_sync.exceptions import AuthenticationError
from models import (FinancialAccount, InstitutionConnection, SyncRun,
                    Transaction, db)

FAST = (0.01, 0.01, 0.01)


@pytest.fixture()
def backfill_app(app):
    """The app fixture with watchers switched on, and no watcher left running."""
    app.config["PLAID_BACKFILL_ENABLED"] = True
    yield app
    for connection_id in list(plaid_backfill._watchers):
        plaid_backfill.stop_watching(connection_id)


def _connection(**kwargs):
    connection = InstitutionConnection(
        institution="plaid", item_id=kwargs.pop("item_id", "item-tompkins"),
        display_name="Tompkins Mahopac",
        status=kwargs.pop("status", "connected"), **kwargs)
    db.session.add(connection)
    db.session.commit()
    return connection


def _account_with_transaction_dated(connection, when):
    account = FinancialAccount(connection_id=connection.id, external_id="acct-1",
                               name="Checking", account_type="checking")
    db.session.add(account)
    db.session.commit()
    db.session.add(Transaction(account_name="Checking", date=when,
                               description="Groceries", amount=-20,
                               category="Groceries", source="sync",
                               account_id=account.id, external_id="txn-1"))
    db.session.commit()
    return account


def _run_sync_importing(app, household_id, count):
    """A fake sync that reports `count` transactions arriving, as the real one does
    through the SyncRun row the watcher reads."""
    from dough.tenancy import tenant_scope

    def _fake(trigger=None, connection_id=None, **kwargs):
        with app.app_context():
            with tenant_scope(household_id):
                db.session.add(SyncRun(connection_id=connection_id,
                                       institution="plaid", trigger=trigger,
                                       status="success", transactions_added=count))
                db.session.commit()
        return True
    return _fake


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

def test_watching_marks_the_connection_as_importing_straight_away(backfill_app):
    """Before any retry has run. The page has to be able to say "more is coming"
    during the wait, which is the entire window in which the user is confused."""
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        db.session.refresh(connection)
        assert connection.history_status == "importing"
        thread.join(timeout=10)


def test_transactions_older_than_the_initial_pull_prove_the_backfill_landed(backfill_app):
    """Plaid's initial update is ~30 days. A transaction from a year ago can only
    have come from the historical backfill, so its presence answers the question
    Plaid offers no endpoint for."""
    connection = _connection()
    _account_with_transaction_dated(connection, date.today() - timedelta(days=365))
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        thread.join(timeout=10)
    db.session.refresh(connection)
    assert connection.history_status == "complete"


def test_a_recent_only_account_settles_complete_rather_than_waiting_it_out(backfill_app):
    """An account genuinely opened last month has no old transactions to find.

    Depth alone would leave it importing for the whole schedule and then call a
    complete history `partial`, which is a false alarm on a perfectly good
    connection — so stillness (two passes importing nothing) settles it too.
    """
    connection = _connection()
    _account_with_transaction_dated(connection, date.today() - timedelta(days=5))
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        thread.join(timeout=10)
    db.session.refresh(connection)
    assert connection.history_status == "complete"


def test_an_item_that_never_delivers_anything_is_not_called_complete(backfill_app):
    """Stillness only counts once something has moved.

    An Item answering PRODUCT_NOT_READY — the state a brand-new connection is
    most likely to be in — imports nothing on every pass, which is
    indistinguishable from "finished" if quiet passes alone decide it. Calling
    an empty ledger `complete` would be the original bug in its worst form:
    confidently wrong instead of merely quiet.
    """
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        thread.join(timeout=10)
    db.session.refresh(connection)
    assert connection.history_status == "partial"


def test_a_history_still_arriving_when_the_schedule_runs_out_is_partial(backfill_app):
    """Not `complete`, and the distinction is the point.

    Every pass keeps importing and nothing old ever shows up — the institution
    is slower than we are willing to wait. The data present is real, so this is
    not an error; what we have lost is the ability to promise it is everything,
    and `partial` is how the page gets to say that instead of showing a
    confident, wrong "synced".
    """
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync",
               side_effect=_run_sync_importing(backfill_app,
                                               connection.household_id, count=5)):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        thread.join(timeout=10)
    db.session.refresh(connection)
    assert connection.history_status == "partial"


def test_only_one_watcher_runs_per_connection(backfill_app):
    """Re-linking an Item, or a webhook arriving mid-wait, must not end up with
    two threads syncing the same connection on independent schedules."""
    connection = _connection()
    started = threading.Event()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync",
               side_effect=lambda **kw: started.set()):
        first = plaid_backfill.watch(backfill_app, connection.id,
                                     connection.household_id, schedule=(5, 5))
        second = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=FAST)
        assert second is None
        plaid_backfill.stop_watching(connection.id)
        first.join(timeout=10)
    assert not started.is_set()  # cancelled during its first wait


def test_stop_watching_cancels_a_pending_retry(backfill_app):
    """A disconnect has to reach the watcher. Otherwise it wakes up an hour later
    and syncs a connection id that no longer exists."""
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync") as run_sync:
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=(5, 5))
        plaid_backfill.stop_watching(connection.id)
        thread.join(timeout=10)
    run_sync.assert_not_called()


def test_watchers_are_off_under_the_testing_default(app):
    """The suite must not acquire background threads from linking a connection.

    `app` here is the ordinary fixture, without the `backfill_app` override.
    """
    connection = _connection()
    assert plaid_backfill.watch(app, connection.id, connection.household_id) is None


# ---------------------------------------------------------------------------
# Webhook dispatch, at the module level
# ---------------------------------------------------------------------------

def test_a_completion_webhook_stops_the_watcher(backfill_app):
    """The webhook is authoritative, so the retries left on the schedule are
    syncs against a bank that has nothing further to give."""
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.watch(backfill_app, connection.id,
                                      connection.household_id, schedule=(5, 5, 5))
        result = plaid_backfill.handle_webhook(backfill_app, {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": connection.item_id,
            "historical_update_complete": True})
        thread.join(timeout=10)
    assert result == "synced: history complete"
    db.session.refresh(connection)
    assert connection.history_status == "complete"


def test_the_older_historical_update_webhook_counts_as_completion(backfill_app):
    """Plaid's pre-`/sync` webhook carries no `historical_update_complete` flag —
    it says the same thing by existing at all, and Items created before the
    switch still send it."""
    connection = _connection()
    connection.history_status = "importing"
    db.session.commit()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        plaid_backfill.handle_webhook(backfill_app, {
            "webhook_type": "TRANSACTIONS", "webhook_code": "HISTORICAL_UPDATE",
            "item_id": connection.item_id})
    db.session.refresh(connection)
    assert connection.history_status == "complete"


def test_a_repaired_login_clears_the_error_and_resyncs(backfill_app):
    """The user fixed it at the bank without coming back through Link, so nothing
    else would tell us to try again until the next scheduled run."""
    connection = _connection(status="expired")
    connection.last_error = "Your bank needs you to sign in again"
    db.session.commit()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync") as run_sync:
        plaid_backfill.handle_webhook(backfill_app, {
            "webhook_type": "ITEM", "webhook_code": "LOGIN_REPAIRED",
            "item_id": connection.item_id})
    run_sync.assert_called_once()
    db.session.refresh(connection)
    assert connection.status == "connected"
    assert connection.last_error is None


def test_an_unhandled_webhook_type_changes_nothing(backfill_app):
    connection = _connection()
    result = plaid_backfill.handle_webhook(backfill_app, {
        "webhook_type": "ASSETS", "webhook_code": "PRODUCT_READY",
        "item_id": connection.item_id})
    assert result == "ignored: ASSETS/PRODUCT_READY"
    db.session.refresh(connection)
    assert connection.history_status is None


# ---------------------------------------------------------------------------
# The adapter's half: "not ready" is not a failure
# ---------------------------------------------------------------------------

def _plaid_error(code):
    resp = MagicMock()
    resp.status_code = 400
    resp.content = b"{}"
    resp.text = "{}"
    resp.json.return_value = {"error_code": code,
                              "error_message": "the product is not ready"}
    return resp


def test_product_not_ready_reports_nothing_rather_than_failing_the_sync(monkeypatch):
    """Plaid saying "not yet" is the expected state moments after linking.

    Letting it raise marks a healthy connection `error` and shows the user a
    warning about a bank that is fine — while the retry that fixes it is
    already scheduled.
    """
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    adapter = PlaidAdapter(credentials={"mode": "live", "access_token": "tok",
                                        "cursor": "cursor-1"})
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _plaid_error("PRODUCT_NOT_READY")
        raw = adapter._fetch_transactions_raw(None)

    assert raw["added"] == []
    assert adapter.transactions_not_ready is True
    # The cursor must not move. Advancing it past a page Plaid never sent would
    # skip that history permanently — the retries would ask for what comes
    # *after* transactions we never received.
    assert adapter.credentials["cursor"] == "cursor-1"
    assert raw["next_cursor"] == "cursor-1"


def test_other_plaid_errors_on_transactions_still_fail(monkeypatch):
    """The exemption is one error code wide. A genuinely broken Item must still
    surface as a failed sync rather than an eternally empty one."""
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    adapter = PlaidAdapter(credentials={"mode": "live", "access_token": "tok"})
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _plaid_error("INVALID_ACCESS_TOKEN")
        with pytest.raises(AuthenticationError):
            adapter._fetch_transactions_raw(None)


# ---------------------------------------------------------------------------
# What the user is told
# ---------------------------------------------------------------------------

def test_the_connections_page_says_history_is_still_arriving(client):
    """The half of this the tester actually experiences.

    The mechanism above is worth nothing if the page still reads "Last sync:
    success" over a third of somebody's transactions. Both testers saw a
    connection that looked finished; only one of them had a finished
    connection.
    """
    connection = _connection()
    connection.history_status = "importing"
    db.session.commit()
    body = client.get("/connections").get_data(as_text=True)
    assert "Still importing your older transactions" in body


def test_the_connections_page_says_when_history_may_be_short(client):
    """`partial` has to reach the user, because it is the one state they can do
    something about — pressing Refresh asks the bank again."""
    connection = _connection()
    connection.history_status = "partial"
    db.session.commit()
    # Whitespace-normalised: the sentence is wrapped in the template, and this
    # test is about what it says, not where the line breaks fall.
    body = " ".join(client.get("/connections").get_data(as_text=True).split())
    assert "older transactions may be missing" in body
    assert "press Refresh to ask again" in body


def test_a_complete_connection_says_nothing_extra(client):
    """No banner for the normal case. A note on every row is a note nobody reads,
    including the two that matter."""
    connection = _connection()
    connection.history_status = "complete"
    db.session.commit()
    body = client.get("/connections").get_data(as_text=True)
    assert "conn-row__history" not in body


# ---------------------------------------------------------------------------
# Startup adoption
# ---------------------------------------------------------------------------

def test_existing_items_get_the_webhook_registered(backfill_app, monkeypatch):
    """Items linked before the deployment had a webhook URL can only be told
    about it with an explicit call — and those are exactly the connections the
    testers who reported this bug are holding."""
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://dough.example.com/api/plaid/webhook")
    connection = _connection()
    connection.auth_blob = "encrypted-blob"  # opaque here; decrypt is patched
    db.session.commit()

    with patch("finance_sync.crypto.TokenCipher.decrypt",
               return_value={"mode": "live", "access_token": "tok"}):
        with patch.object(PlaidAdapter, "update_webhook") as update:
            with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
                thread = plaid_backfill.install(backfill_app)
                thread.join(timeout=10)
    update.assert_called_once_with("https://dough.example.com/api/plaid/webhook")


def test_a_connection_with_no_history_status_is_adopted(backfill_app, monkeypatch):
    """NULL means "never asked", which is the state every pre-existing row is in
    — including the truncated one. Adopting it is what recovers the history
    without anybody having to notice and press Refresh.

    Asserts the adoption, not the outcome: what happens after a watcher starts
    is the subject of the tests at the top of this file, and re-proving it here
    would mean waiting out the real retry schedule.
    """
    monkeypatch.delenv("PLAID_WEBHOOK_URL", raising=False)
    connection = _connection()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.install(backfill_app)
        thread.join(timeout=10)
    assert connection.id in plaid_backfill._watchers
    db.session.refresh(connection)
    assert connection.history_status == "importing"


def test_a_connection_that_already_has_a_history_status_is_left_alone(backfill_app,
                                                                     monkeypatch):
    """Only the never-asked rows are adopted. Restarting the process must not
    re-open a question that has already been answered — a `complete` connection
    would otherwise pick up a fresh watcher, and two pointless syncs, on every
    deploy."""
    monkeypatch.delenv("PLAID_WEBHOOK_URL", raising=False)
    connection = _connection()
    connection.history_status = "complete"
    db.session.commit()
    with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
        thread = plaid_backfill.install(backfill_app)
        thread.join(timeout=10)
    assert connection.id not in plaid_backfill._watchers
