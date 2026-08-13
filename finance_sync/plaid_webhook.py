"""Verification of Plaid's webhook signatures.

Nothing about *what* a webhook means lives here — that is
``finance_sync/plaid_backfill.py``. This module answers one question:
**did Plaid actually send this?**

The endpoint has to be reachable by an anonymous request from the public
internet, so it carries ``@public`` and ``@csrf_exempt`` and has no session,
no cookie and no bearer token behind it. This signature *is* its
authentication, and it is the only thing standing between a stranger and a
route that starts background syncs and rewrites connection status. It is
therefore fail-closed everywhere: an unset key, an unfetchable key, a
malformed header, an unexpected algorithm and an expired timestamp all return
False, and the caller rejects.

How Plaid signs
---------------
Each POST carries a ``Plaid-Verification`` header holding a JWT signed with
ES256. The body is **not** in the JWT — the JWT carries a
``request_body_sha256`` claim, and the verifier hashes the raw request bytes
and compares. That indirection is why `verify` takes the raw body rather than
parsed JSON: ``json.loads`` then ``json.dumps`` does not round-trip
byte-for-byte, and re-serializing would fail every legitimate webhook.

Verifying ES256 without PyJWT
-----------------------------
`cryptography` is already a dependency (it holds the key to every stored bank
token), and the whole of ES256 verification is ~30 lines against it, so this
does the JWS assembly by hand rather than adding a library to the pinned set
for one route. The only real subtlety is the signature encoding: JWS writes
ECDSA signatures as the raw ``r || s`` integers, while `cryptography` verifies
DER, so `_der_signature` re-encodes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

logger = logging.getLogger("finance_sync.plaid_webhook")

HEADER = "Plaid-Verification"

# How old a webhook may be and still be accepted. Plaid's own guidance is five
# minutes; the window is what stops a signed request captured off the wire from
# being replayed indefinitely.
_MAX_AGE_SECONDS = 5 * 60

# Verification keys, by `kid`. Plaid rotates these, and fetching one costs an
# API round trip that would otherwise happen on *every* webhook — of which a
# busy household's initial backfill produces a burst. Keys are immutable for a
# given kid, so this never needs invalidating: a rotation issues a new kid,
# which simply misses the cache once.
_key_cache: Dict[str, Dict[str, Any]] = {}
_key_lock = threading.Lock()


def _b64url(segment: str) -> bytes:
    """Decode a base64url JWT segment, restoring the stripped padding."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _split(token: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]]:
    """(header, payload, signature, signing input) — or None if malformed."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64url(parts[0]))
        payload = json.loads(_b64url(parts[1]))
        signature = _b64url(parts[2])
    except (ValueError, TypeError, base64.binascii.Error):
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    return header, payload, signature, f"{parts[0]}.{parts[1]}".encode()


def _der_signature(raw: bytes) -> Optional[bytes]:
    """JWS `r || s` (64 bytes for P-256) as the DER `cryptography` expects."""
    if len(raw) != 64:
        return None
    half = len(raw) // 2
    return asym_utils.encode_dss_signature(
        int.from_bytes(raw[:half], "big"), int.from_bytes(raw[half:], "big"))


def _public_key(jwk: Dict[str, Any]):
    """An EC public key from Plaid's JWK, or None if it is not the shape we verify."""
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return None
    try:
        x = int.from_bytes(_b64url(jwk["x"]), "big")
        y = int.from_bytes(_b64url(jwk["y"]), "big")
    except (KeyError, ValueError, TypeError, base64.binascii.Error):
        return None
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def _fetch_key(key_id: str) -> Optional[Dict[str, Any]]:
    """Plaid's verification key for `key_id`, cached. None if it cannot be had."""
    with _key_lock:
        cached = _key_cache.get(key_id)
    if cached is not None:
        return cached
    # Imported here rather than at module scope to keep the import graph one
    # directional: the adapter package must be able to load without this module.
    from .adapters.plaid_adapter import PlaidAdapter
    try:
        data = PlaidAdapter()._plaid_call("webhook_verification_key/get",
                                          {"key_id": key_id})
    except Exception:
        # Includes a network failure and a rejected credential alike. Either way
        # this webhook cannot be verified, and an unverified webhook is refused.
        logger.exception("Could not fetch Plaid webhook verification key %s", key_id)
        return None
    key = data.get("key")
    if not isinstance(key, dict):
        return None
    with _key_lock:
        _key_cache[key_id] = key
    return key


def verify(body: bytes, header_value: Optional[str]) -> bool:
    """True only if `header_value` is Plaid's valid signature over exactly `body`.

    `body` must be the raw request bytes (``request.get_data()``), not
    re-serialized JSON — see the module docstring.
    """
    if not header_value:
        return False
    parsed = _split(header_value)
    if parsed is None:
        logger.warning("Plaid webhook rejected: malformed verification header")
        return False
    jwt_header, payload, signature, signing_input = parsed

    # Pinned, not read. Accepting whatever `alg` the token asks for is the
    # classic JWT forgery: `alg: none` verifies everything, and `alg: HS256`
    # invites the verifier to use a *public* key as an HMAC secret.
    if jwt_header.get("alg") != "ES256":
        logger.warning("Plaid webhook rejected: unexpected alg %r",
                       jwt_header.get("alg"))
        return False
    key_id = jwt_header.get("kid")
    if not isinstance(key_id, str) or not key_id:
        return False

    jwk = _fetch_key(key_id)
    if jwk is None:
        return False
    public_key = _public_key(jwk)
    der = _der_signature(signature)
    if public_key is None or der is None:
        logger.warning("Plaid webhook rejected: unusable key or signature encoding")
        return False
    try:
        public_key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        logger.warning("Plaid webhook rejected: bad signature")
        return False

    issued_at = payload.get("iat")
    if not isinstance(issued_at, (int, float)):
        return False
    # Signed but stale. Only the lower bound is enforced loosely: a webhook
    # dated slightly in the future is clock skew between us and Plaid, not an
    # attack, and rejecting it would drop real traffic on a drifting host.
    if time.time() - issued_at > _MAX_AGE_SECONDS:
        logger.warning("Plaid webhook rejected: signature is %.0fs old",
                       time.time() - issued_at)
        return False

    claimed = payload.get("request_body_sha256")
    if not isinstance(claimed, str):
        return False
    # compare_digest: the signature is verified by this point, so a timing leak
    # here is of limited use, but the comparison is free to do properly.
    return hmac.compare_digest(claimed, hashlib.sha256(body).hexdigest())
