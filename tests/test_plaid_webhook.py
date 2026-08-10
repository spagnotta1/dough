"""`POST /api/plaid/webhook`: the signature is the only thing guarding it.

The endpoint is `@public` and `@csrf_exempt` — it has to be, Plaid's servers
have no session with us — so every one of these tests is really the same
question asked from a different angle: can an anonymous request that is not
from Plaid change anything? The answer has to stay "no" on all of them, because
the handler behind this route starts syncs and rewrites connection status.

`tests/test_route_guard.py` and `tests/test_csrf.py` pin the two markers from
their side; this file pins what pays for them.
"""

import base64
import hashlib
import json
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from finance_sync import plaid_webhook
from models import InstitutionConnection, db

KID = "test-key-1"


@pytest.fixture(autouse=True)
def _clear_key_cache():
    """The verification-key cache is module state, so it outlives a test.

    Without this, the first test to fetch a key would hand its key to every
    later test — including the ones asserting that a *bad* key is refused,
    which would then pass for the wrong reason.
    """
    plaid_webhook._key_cache.clear()
    yield
    plaid_webhook._key_cache.clear()


@pytest.fixture()
def signing_key():
    return ec.generate_private_key(ec.SECP256R1())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwk(private_key, kid=KID) -> dict:
    numbers = private_key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256", "kid": kid,
            "x": _b64(numbers.x.to_bytes(32, "big")),
            "y": _b64(numbers.y.to_bytes(32, "big"))}


def _sign(private_key, body: bytes, *, alg="ES256", kid=KID, iat=None,
          body_hash=None) -> str:
    """A `Plaid-Verification` header for `body`, forgeable in the ways we test."""
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    payload = {"iat": int(time.time() if iat is None else iat),
               "request_body_sha256": body_hash or hashlib.sha256(body).hexdigest()}
    encoded = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    der = private_key.sign(encoded.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{encoded}.{_b64(raw)}"


def _post(client, body: dict, header_value, private_key=None):
    """POST exactly the bytes the signature covers.

    `json=` would re-serialize and could produce different bytes than the ones
    hashed into the JWT, which is the failure mode the raw-body handling in
    `plaid_webhook` exists to avoid — so the test sends bytes too.
    """
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if header_value is not None:
        headers[plaid_webhook.HEADER] = header_value
    return client.post("/api/plaid/webhook", data=raw, headers=headers)


def _plaid_connection(item_id="item-tompkins", **kwargs):
    connection = InstitutionConnection(
        institution="plaid", item_id=item_id,
        display_name=kwargs.pop("display_name", "Tompkins Mahopac"),
        status=kwargs.pop("status", "connected"), **kwargs)
    db.session.add(connection)
    db.session.commit()
    return connection


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_unsigned_request_is_refused(client):
    """The whole point. No header, no session, no CSRF token — nothing at all."""
    connection = _plaid_connection()
    resp = _post(client, {"webhook_type": "TRANSACTIONS",
                          "webhook_code": "SYNC_UPDATES_AVAILABLE",
                          "item_id": connection.item_id,
                          "historical_update_complete": True}, None)
    assert resp.status_code == 401
    db.session.refresh(connection)
    assert connection.history_status is None


def test_a_body_edited_after_signing_is_refused(client, signing_key):
    """The signature covers the body, not just the fact that Plaid sent something.

    Without the `request_body_sha256` check, a signature captured off any real
    webhook would authenticate an attacker's payload naming any item_id they
    liked.
    """
    honest = {"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "item-a"}
    header = _sign(signing_key, json.dumps(honest).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        resp = _post(client, {**honest, "item_id": "item-b"}, header)
    assert resp.status_code == 401


def test_alg_none_is_refused(client, signing_key):
    """`alg` is pinned, not read. Trusting the token's own choice of algorithm
    is the oldest JWT forgery there is."""
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "DEFAULT_UPDATE",
            "item_id": "item-a"}
    header = _sign(signing_key, json.dumps(body).encode(), alg="none")
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        resp = _post(client, body, header)
    assert resp.status_code == 401


def test_a_signature_from_the_wrong_key_is_refused(client, signing_key):
    """Signed correctly, by somebody who is not Plaid."""
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "DEFAULT_UPDATE",
            "item_id": "item-a"}
    header = _sign(attacker_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)  # Plaid's real key
        resp = _post(client, body, header)
    assert resp.status_code == 401


def test_a_stale_signature_is_refused(client, signing_key):
    """A genuine webhook captured off the wire must not be replayable forever."""
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "DEFAULT_UPDATE",
            "item_id": "item-a"}
    header = _sign(signing_key, json.dumps(body).encode(),
                   iat=time.time() - plaid_webhook._MAX_AGE_SECONDS - 60)
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        resp = _post(client, body, header)
    assert resp.status_code == 401


def test_an_unfetchable_key_refuses_rather_than_admits(client, signing_key):
    """Fail-closed. A Plaid outage must not turn the endpoint into an open door."""
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "DEFAULT_UPDATE",
            "item_id": "item-a"}
    header = _sign(signing_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.side_effect = OSError("plaid is down")
        resp = _post(client, body, header)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

def _key_response(private_key, kid=KID):
    """A `webhook_verification_key/get` response carrying `private_key`'s public half."""
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = {"key": _jwk(private_key, kid)}
    resp.text = "{}"
    return resp


def test_a_valid_historical_update_marks_the_history_complete(client, signing_key):
    """The signal this whole feature was built for.

    Plaid saying `historical_update_complete` is the only authoritative answer
    to "has the backfill finished?" — the question that, unanswered, left one
    UAT tester with one month of a promised two years.
    """
    connection = _plaid_connection()
    connection.history_status = "importing"
    db.session.commit()

    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": connection.item_id, "historical_update_complete": True}
    header = _sign(signing_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        with patch("finance_sync.scheduler.SyncScheduler.run_sync") as run_sync:
            resp = _post(client, body, header)

    assert resp.status_code == 200
    run_sync.assert_called_once()
    assert run_sync.call_args.kwargs["connection_id"] == connection.id
    db.session.refresh(connection)
    assert connection.history_status == "complete"


def test_an_update_still_in_progress_syncs_without_claiming_completion(client, signing_key):
    """`historical_update_complete: false` means fetch what is there and keep waiting.

    Marking it complete here would stop the backfill watcher on the strength of
    a webhook that explicitly said it was not finished — reintroducing the
    original bug through the mechanism meant to fix it.
    """
    connection = _plaid_connection()
    connection.history_status = "importing"
    db.session.commit()

    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": connection.item_id, "historical_update_complete": False}
    header = _sign(signing_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        with patch("finance_sync.scheduler.SyncScheduler.run_sync") as run_sync:
            resp = _post(client, body, header)

    assert resp.status_code == 200
    run_sync.assert_called_once()
    db.session.refresh(connection)
    assert connection.history_status == "importing"


def test_an_item_error_marks_the_connection_expired(client, signing_key):
    """Found out now rather than at the next scheduled sync twelve hours later."""
    connection = _plaid_connection()
    body = {"webhook_type": "ITEM", "webhook_code": "ERROR",
            "item_id": connection.item_id,
            "error": {"error_code": "ITEM_LOGIN_REQUIRED"}}
    header = _sign(signing_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        resp = _post(client, body, header)

    assert resp.status_code == 200
    db.session.refresh(connection)
    assert connection.status == "expired"
    assert "sign in again" in connection.last_error


def test_a_webhook_for_an_unknown_item_is_accepted_and_ignored(client, signing_key):
    """200, not 404. Plaid retries a non-2xx, and no number of redeliveries will
    make an Item we do not have appear — it is another deployment sharing these
    credentials, or one disconnected between the event and its delivery."""
    body = {"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item-belonging-to-nobody", "historical_update_complete": True}
    header = _sign(signing_key, json.dumps(body).encode())
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        with patch("finance_sync.scheduler.SyncScheduler.run_sync") as run_sync:
            resp = _post(client, body, header)

    assert resp.status_code == 200
    run_sync.assert_not_called()


def test_the_verification_key_is_fetched_once_and_reused(client, signing_key):
    """A backfill delivers a burst of webhooks; one key fetch each would double
    the round trips for no benefit, since a `kid` names an immutable key."""
    connection = _plaid_connection()
    with patch("finance_sync.adapters.plaid_adapter.requests.post") as post:
        post.return_value = _key_response(signing_key)
        with patch("finance_sync.scheduler.SyncScheduler.run_sync"):
            for _ in range(3):
                body = {"webhook_type": "TRANSACTIONS",
                        "webhook_code": "DEFAULT_UPDATE",
                        "item_id": connection.item_id}
                _post(client, body, _sign(signing_key, json.dumps(body).encode()))
        key_fetches = [call for call in post.call_args_list
                       if call.args and "webhook_verification_key" in call.args[0]]
    assert len(key_fetches) == 1
