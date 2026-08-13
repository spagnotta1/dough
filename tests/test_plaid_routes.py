"""HTTP API for the Plaid Link handshake: link-token creation and exchange."""

from unittest.mock import MagicMock, patch

from models import InstitutionConnection


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}"
    resp.json.return_value = json_data or {}
    resp.text = "{}"
    return resp


def _plaid_side_effect(item_id):
    def _post(url, json=None, timeout=None):
        if url.endswith("/link/token/create"):
            return _mock_response(200, {"link_token": "link-" + item_id})
        if url.endswith("/item/public_token/exchange"):
            return _mock_response(200, {"access_token": "access-" + item_id, "item_id": item_id})
        if url.endswith("/accounts/get"):
            return _mock_response(200, {"accounts": [{
                "account_id": item_id + "-acct", "name": "Checking",
                "type": "depository", "subtype": "checking", "mask": "1234",
                "balances": {"current": 100.0, "available": 100.0, "iso_currency_code": "USD"},
            }], "item": {"item_id": item_id}})
        if url.endswith("/transactions/sync"):
            return _mock_response(200, {"added": [], "modified": [], "removed": [],
                                        "next_cursor": "c1", "has_more": False})
        if url.endswith("/investments/holdings/get"):
            return _mock_response(200, {"holdings": [], "securities": []})
        raise AssertionError(f"unexpected Plaid call: {url}")
    return _post


def test_link_token_404_when_not_configured(client, monkeypatch):
    # app.py loads .env at import time, so a developer's real Plaid credentials
    # may already be in os.environ — force the unconfigured case explicitly.
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    resp = client.post("/api/plaid/link-token")
    assert resp.status_code == 404


def test_exchange_400_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    resp = client.post("/api/connections/plaid/exchange",
                       json={"public_token": "pt", "institution_name": "Chase"})
    assert resp.status_code == 400


def test_link_token_endpoint_when_configured(client, monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as mock_post:
        mock_post.side_effect = _plaid_side_effect("item-1")
        resp = client.post("/api/plaid/link-token")
    assert resp.status_code == 200
    assert resp.get_json()["link_token"] == "link-item-1"


def test_exchange_creates_connection_and_linking_a_second_item_adds_another_row(client, monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as mock_post:
        mock_post.side_effect = _plaid_side_effect("item-1")
        resp = client.post("/api/connections/plaid/exchange",
                           json={"public_token": "pt-1", "institution_name": "Chase"})
        assert resp.status_code == 201, resp.get_json()

        mock_post.side_effect = _plaid_side_effect("item-2")
        resp = client.post("/api/connections/plaid/exchange",
                           json={"public_token": "pt-2", "institution_name": "Fidelity"})
        assert resp.status_code == 201, resp.get_json()

    connections = InstitutionConnection.query.filter_by(institution="plaid").all()
    assert {c.item_id for c in connections} == {"item-1", "item-2"}
    assert {c.display_name for c in connections} == {"Chase", "Fidelity"}


def test_exchange_starts_a_backfill_watcher(client, monkeypatch):
    """Linking is not the end of the import.  [UAT round 1]

    The connect sync fires immediately and gets whatever Plaid has ready, which
    moments after linking is typically the initial ~30 days — a tester whose
    bank was slower than most ended up with exactly that and no more. The
    watcher is what comes back for the rest, so the exchange has to start one
    every time; a connection nobody re-syncs keeps whatever the race gave it.
    """
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as mock_post:
        mock_post.side_effect = _plaid_side_effect("item-1")
        with patch("finance_sync.plaid_backfill.watch") as watch:
            resp = client.post("/api/connections/plaid/exchange",
                               json={"public_token": "pt-1",
                                     "institution_name": "Tompkins Mahopac"})
    assert resp.status_code == 201, resp.get_json()
    watch.assert_called_once()
    connection = InstitutionConnection.query.filter_by(item_id="item-1").one()
    assert watch.call_args.args[1] == connection.id


def test_link_token_asks_for_two_years_and_registers_the_webhook(client, monkeypatch):
    """Both halves of "get the whole history" are set at Item creation.

    `days_requested` is what Plaid will eventually fetch; `webhook` is the only
    way it can tell us it has finished fetching it. Neither can be added to an
    Item later through Link, so a regression here is silent and only shows up
    as a short ledger weeks afterwards.
    """
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://dough.example.com/api/plaid/webhook")
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as mock_post:
        mock_post.side_effect = _plaid_side_effect("item-1")
        assert client.post("/api/plaid/link-token").status_code == 200
    sent = mock_post.call_args.kwargs["json"]
    assert sent["transactions"]["days_requested"] == 730
    assert sent["webhook"] == "https://dough.example.com/api/plaid/webhook"


def test_link_token_omits_the_webhook_when_none_is_configured(client, monkeypatch):
    """A local install has no publicly reachable URL. Sending an empty one would
    have Plaid reject the whole link-token call, taking the connect flow down
    with it — the backfill watcher covers this case instead."""
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    monkeypatch.delenv("PLAID_WEBHOOK_URL", raising=False)
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as mock_post:
        mock_post.side_effect = _plaid_side_effect("item-1")
        assert client.post("/api/plaid/link-token").status_code == 200
    assert "webhook" not in mock_post.call_args.kwargs["json"]


def test_exchange_missing_public_token_400(client, monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    resp = client.post("/api/connections/plaid/exchange", json={"institution_name": "Chase"})
    assert resp.status_code == 400
