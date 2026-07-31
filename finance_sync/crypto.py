"""Encryption at rest for the stored institution credentials.  [Phase 10.5]

`connected_accounts.auth_blob` holds the Plaid and Coinbase access tokens — the
credentials that read a family's real bank data. They are encrypted with Fernet
(AES-128-CBC + HMAC, authenticated), so the database file, a backup, or a stray
``SELECT *`` in a log yields ciphertext rather than working access. Usernames and
passwords are never stored at all.

## Where the key comes from, in order

1. ``ENCRYPTION_KEY`` or ``SYNC_ENCRYPTION_KEY`` in the environment. Both, with
   ``SYNC_ENCRYPTION_KEY`` winning when they disagree — it is the name existing
   installations already have in `.env`, and preferring the newer name would
   decrypt nothing on the first machine that set both.
2. ``.sync_encryption_key`` next to the database, if it exists.
3. A newly generated key written to that file — **only when generation is
   permitted**.

## Why step 3 is not always permitted

Generating a key is silent, succeeds, and destroys data. Every token already
encrypted under the previous key becomes unreadable, and the failure does not
arrive at boot: it arrives at the next sync, as
``ConfigurationError('Stored credentials cannot be decrypted')``, pointing at the
encryption layer rather than at the deleted file that caused it.

That is survivable on a laptop, where the fix is to re-link a connection. It is
not survivable in production, where the file may live on a container filesystem
that is empty on every deploy — so the key would be regenerated on every deploy,
every connection would break on every deploy, and nothing would ever say why.

So `allow_generate` is False in production (`ALLOW_GENERATED_ENCRYPTION_KEY`,
config.py) and `MissingEncryptionKey` is raised instead, at construction, with
the command that produces a valid key.

## Fail-safe, not fail-open

There is no path here that stores a credential unencrypted. A missing key raises;
a wrong key raises on the first decrypt. The alternative — falling back to
plaintext when no key is available — would mean the security property depends on
configuration nobody checks, and the resulting rows are indistinguishable from
encrypted ones without trying to decrypt them.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .exceptions import ConfigurationError

_KEY_FILENAME = ".sync_encryption_key"

#: Read in this order; the first non-empty wins. See the module docstring for
#: why the older name is first.
_KEY_ENV_VARS = ("SYNC_ENCRYPTION_KEY", "ENCRYPTION_KEY")

#: How to produce a valid value, quoted in every error this module raises. A
#: Fernet key is not "any random string" -- it is 32 url-safe base64 bytes, and
#: an operator who pastes `token_hex(32)` in gets an exception that does not say
#: what was wrong with it.
_GENERATE_HINT = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"')


class MissingEncryptionKey(ConfigurationError):
    """No key was configured and generating one was not permitted.

    A `ConfigurationError` subclass so every existing caller that already
    handles one keeps working, and a distinct type so a deployment check can
    tell "no key" from "wrong key" -- the first is a variable somebody forgot,
    the second is a variable somebody changed, and the recovery differs.
    """


class TokenCipher:
    """Encrypts/decrypts credential dictionaries for storage in SQLite."""

    def __init__(self, key: Optional[bytes] = None,
                 base_dir: Optional[str] = None,
                 allow_generate: bool = True):
        self._base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._allow_generate = allow_generate
        resolved = key or self._load_or_create_key()
        try:
            self._fernet = Fernet(resolved)
        except (ValueError, TypeError) as exc:
            # Raised at construction rather than at first use. A malformed key
            # discovered on the first sync is an error attributed to the sync;
            # discovered here it is attributed to the configuration, which is
            # where the fix is.
            raise ConfigurationError(
                'The configured encryption key is not a valid Fernet key. It '
                'must be 32 url-safe base64-encoded bytes. Generate one with:\n'
                f'    {_GENERATE_HINT}') from exc

    @classmethod
    def from_config(cls, config, base_dir=None):
        """Build one from a Flask config, honouring the production policy.

        The seam that lets `ALLOW_GENERATED_ENCRYPTION_KEY` mean anything.
        `finance_sync` runs on the scheduler thread with no application, so the
        no-argument constructor stays the default path; this is for the callers
        that *do* have configuration in hand.

        Note what `allow_generate=False` does and does not forbid. It forbids
        *creating* a key; it does not forbid reading one that is already on
        disk, and it must not — a production deployment with a persisted
        `.sync_encryption_key` on a real volume is a perfectly good deployment,
        and refusing to read it would force every such installation to move a
        working key into an environment variable to keep running.

        `base_dir` is where that file is looked for. It exists so a test can
        point the lookup somewhere empty; without it, a test asserting that a
        missing key raises would find the repository's own key file and pass
        for the wrong reason on a developer's machine and fail in a clean CI
        checkout.
        """
        key = config.get('ENCRYPTION_KEY') or None
        return cls(key=key.encode() if isinstance(key, str) else key,
                   base_dir=base_dir,
                   allow_generate=config.get('ALLOW_GENERATED_ENCRYPTION_KEY',
                                             True))

    def _key_path(self) -> str:
        return os.path.join(self._base_dir, _KEY_FILENAME)

    def _load_or_create_key(self) -> bytes:
        for var in _KEY_ENV_VARS:
            env_key = (os.environ.get(var) or '').strip()
            if env_key:
                return env_key.encode()

        path = self._key_path()
        if os.path.exists(path):
            with open(path, "rb") as fh:
                stored = fh.read().strip()
            if stored:
                return stored

        if not self._allow_generate:
            raise MissingEncryptionKey(
                'No encryption key is configured and generating one is not '
                'permitted in this environment.\n'
                '\n'
                'Generating a key here would make every already-stored '
                'institution token unreadable, and the failure would surface at '
                'the next sync rather than now.\n'
                '\n'
                f'Set ENCRYPTION_KEY to a durable value:\n'
                f'    {_GENERATE_HINT}')

        key = Fernet.generate_key()
        with open(path, "wb") as fh:
            fh.write(key)
        return key

    def encrypt(self, credentials: dict) -> str:
        """Serialize and encrypt a credentials dict to a storable string."""
        raw = json.dumps(credentials).encode()
        return self._fernet.encrypt(raw).decode()

    def decrypt(self, blob: str) -> dict:
        """Decrypt a stored credentials blob back into a dict."""
        if not blob:
            return {}
        try:
            raw = self._fernet.decrypt(blob.encode())
        except InvalidToken as exc:
            # Deliberately says nothing about the blob. Fernet is authenticated,
            # so this means the key does not match the one that wrote it -- which
            # is a configuration fact -- and quoting any part of the ciphertext
            # into an error that will be logged serves nobody.
            raise ConfigurationError(
                'Stored credentials cannot be decrypted. The encryption key '
                'does not match the one they were written with -- it was '
                'changed, or a generated `.sync_encryption_key` was lost and '
                'replaced.\n'
                '\n'
                'If the original key is recoverable, set ENCRYPTION_KEY to it. '
                'If it is not, these credentials cannot be recovered and the '
                'affected institutions must be connected again.'
            ) from exc
        return json.loads(raw.decode())
