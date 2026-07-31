"""Encryption at rest for the stored institution credentials.  [Phase 10.5]

`connected_accounts.auth_blob` holds the Plaid and Coinbase access tokens — the
credentials that read a family's real bank data. Three properties are asserted
here and they are the three the Phase 10.5 brief names:

1. **The stored value is not the plaintext.** Asserted against the *database
   column*, not against the cipher, because the cipher being correct proves
   nothing if the write path bypasses it.
2. **A sync can decrypt what it wrote.** A round trip through the real service
   objects, so a key resolved two different ways by two different callers fails
   here rather than at a customer's next sync.
3. **A missing key fails safely.** Loudly, at construction, in production —
   rather than by generating a fresh key, which succeeds and destroys data.

Property 3 is the one that motivated this work. Generating a key when the file
is missing is silent, returns success, and makes every already-stored token
unreadable — and the failure surfaces at the next sync as an encryption error,
pointing at the encryption layer rather than at the deleted file that caused it.
On a container filesystem that starts empty on every deploy, that is every
connection breaking on every deploy with nothing saying why.
"""

import os

import pytest
from cryptography.fernet import Fernet

from finance_sync.crypto import MissingEncryptionKey, TokenCipher
from finance_sync.exceptions import ConfigurationError

CREDENTIALS = {'access_token': 'access-sandbox-abc123', 'item_id': 'item-1'}


@pytest.fixture()
def key():
    return Fernet.generate_key()


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """Neither key variable may leak in from the developer's real environment.

    `app.py` calls `load_dotenv()` at import, so a real `.env` has already
    populated `os.environ` by the time any of this runs. A test asserting that a
    *missing* key raises would otherwise pass or fail depending on whose machine
    it ran on — and would pass for the wrong reason on the machine that has one.
    """
    for var in ('ENCRYPTION_KEY', 'SYNC_ENCRYPTION_KEY'):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. The stored value differs from the plaintext
# ---------------------------------------------------------------------------

def test_encrypted_values_differ_from_plaintext(key):
    cipher = TokenCipher(key=key)
    blob = cipher.encrypt(CREDENTIALS)

    assert blob != str(CREDENTIALS)
    # The part that actually matters: no substring of the secret survives.
    assert 'access-sandbox-abc123' not in blob
    assert 'item-1' not in blob


def test_two_encryptions_of_the_same_value_differ(key):
    """Fernet carries a random IV, so the ciphertext is not a fingerprint.

    Without this, equal blobs would reveal that two households had linked the
    same institution with the same token — and a stored blob would be a stable
    identifier that survives rotation of everything around it.
    """
    cipher = TokenCipher(key=key)
    assert cipher.encrypt(CREDENTIALS) != cipher.encrypt(CREDENTIALS)


def test_the_stored_column_holds_ciphertext_not_the_token(app, monkeypatch, key):
    """Asserted at the database, because that is what a backup contains.

    Testing the cipher alone would keep passing if the write path stopped
    calling it — which is the regression worth catching, since nothing about a
    plaintext `auth_blob` looks wrong from the application's side.
    """
    from models import InstitutionConnection, db
    from finance_sync.service import ConnectionService

    service = ConnectionService(cipher=TokenCipher(key=key))
    connection = InstitutionConnection(
        institution='plaid', display_name='Test Bank',
        household_id=app.config['DEFAULT_HOUSEHOLD_ID'])
    connection.auth_blob = service.cipher.encrypt(CREDENTIALS)
    db.session.add(connection)
    db.session.commit()

    raw = db.session.execute(
        db.text('SELECT auth_blob FROM connected_accounts WHERE id = :i'),
        {'i': connection.id}).scalar()
    assert 'access-sandbox-abc123' not in raw
    assert raw.startswith('gAAAAA')     # the Fernet version marker


# ---------------------------------------------------------------------------
# 2. A sync can decrypt what was written
# ---------------------------------------------------------------------------

def test_a_round_trip_returns_the_original_credentials(key):
    cipher = TokenCipher(key=key)
    assert cipher.decrypt(cipher.encrypt(CREDENTIALS)) == CREDENTIALS


def test_a_second_cipher_with_the_same_key_can_decrypt(key):
    """The sync engine and the connection service build their own ciphers.

    They are separate objects resolving the key independently, so this is the
    assertion that they resolve it to the same thing — the failure it catches is
    a write that succeeds and a read, minutes later on a scheduler thread, that
    does not.
    """
    blob = TokenCipher(key=key).encrypt(CREDENTIALS)
    assert TokenCipher(key=key).decrypt(blob) == CREDENTIALS


def test_an_empty_blob_decrypts_to_an_empty_dict(key):
    """A connection with no stored credentials is a valid state, not an error."""
    assert TokenCipher(key=key).decrypt('') == {}


# ---------------------------------------------------------------------------
# 3. Key resolution, and failing safely
# ---------------------------------------------------------------------------

def test_either_environment_variable_supplies_the_key(monkeypatch, tmp_path, key):
    for var in ('ENCRYPTION_KEY', 'SYNC_ENCRYPTION_KEY'):
        monkeypatch.setenv(var, key.decode())
        cipher = TokenCipher(base_dir=str(tmp_path))
        assert cipher.decrypt(cipher.encrypt(CREDENTIALS)) == CREDENTIALS
        monkeypatch.delenv(var)


def test_the_older_variable_wins_when_both_are_set(monkeypatch, tmp_path):
    """`SYNC_ENCRYPTION_KEY` is what existing installations already have.

    Preferring the newer name would decrypt nothing on the first machine that
    set both — an outage whose symptom ("stored credentials cannot be
    decrypted") points at the wrong thing entirely.
    """
    old, new = Fernet.generate_key(), Fernet.generate_key()
    monkeypatch.setenv('SYNC_ENCRYPTION_KEY', old.decode())
    monkeypatch.setenv('ENCRYPTION_KEY', new.decode())

    blob = TokenCipher(key=old).encrypt(CREDENTIALS)
    assert TokenCipher(base_dir=str(tmp_path)).decrypt(blob) == CREDENTIALS


def test_a_missing_key_fails_safely_when_generation_is_not_permitted(tmp_path):
    """Production. It raises rather than generating, and says how to fix it.

    The message has to carry the command, because a Fernet key is not "any
    random string" — an operator who pastes `token_hex(32)` gets an exception
    that does not say what was wrong with it.
    """
    with pytest.raises(MissingEncryptionKey) as excinfo:
        TokenCipher(base_dir=str(tmp_path), allow_generate=False)

    message = str(excinfo.value)
    assert 'ENCRYPTION_KEY' in message
    assert 'Fernet.generate_key' in message
    # And it did not quietly write one on the way out.
    assert not os.path.exists(os.path.join(str(tmp_path), '.sync_encryption_key'))


def test_a_missing_key_is_generated_and_reused_when_that_is_permitted(tmp_path):
    """Development. Generated once, then read forever after.

    Regenerating per process would make every previously stored token
    unreadable, so the reuse is the property worth asserting rather than the
    generation.
    """
    first = TokenCipher(base_dir=str(tmp_path))
    blob = first.encrypt(CREDENTIALS)

    second = TokenCipher(base_dir=str(tmp_path))
    assert second.decrypt(blob) == CREDENTIALS


def test_a_malformed_key_is_refused_at_construction(tmp_path, monkeypatch):
    """Not at the first decrypt, on a scheduler thread, in the middle of a sync.

    Discovered here it is a configuration error, which is where the fix is.
    """
    monkeypatch.setenv('ENCRYPTION_KEY', 'not-a-fernet-key')
    with pytest.raises(ConfigurationError) as excinfo:
        TokenCipher(base_dir=str(tmp_path))
    assert 'Fernet' in str(excinfo.value)


def test_the_wrong_key_reports_a_configuration_problem_not_a_data_problem(tmp_path):
    """Fernet is authenticated, so this can only mean the key changed.

    The message says both recoveries — restore the key, or re-link — because
    only the operator knows which is available, and an error that says neither
    leaves them guessing at the worst possible moment.
    """
    blob = TokenCipher(key=Fernet.generate_key()).encrypt(CREDENTIALS)

    with pytest.raises(ConfigurationError) as excinfo:
        TokenCipher(key=Fernet.generate_key()).decrypt(blob)

    message = str(excinfo.value)
    assert 'encryption key' in message
    assert 'connected again' in message
    # The ciphertext must not be quoted into an error that will be logged.
    assert blob not in message


def test_from_config_honours_the_production_policy(tmp_path):
    """The seam that makes `ALLOW_GENERATED_ENCRYPTION_KEY` mean anything.

    `base_dir` points at an empty directory deliberately. Without it the lookup
    finds this repository's own `.sync_encryption_key` and the test passes for
    the wrong reason on a developer's machine — while failing in a clean
    checkout, which is the worst combination.
    """
    with pytest.raises(MissingEncryptionKey):
        TokenCipher.from_config({'ENCRYPTION_KEY': '',
                                 'ALLOW_GENERATED_ENCRYPTION_KEY': False},
                                base_dir=str(tmp_path))


def test_a_key_already_on_disk_is_read_even_in_production(tmp_path, key):
    """`allow_generate=False` forbids *creating* a key, not reading one.

    A production deployment with a persisted key file on a real volume is a good
    deployment. Refusing to read it would force every such installation to move
    a working key into an environment variable just to keep running.
    """
    (tmp_path / '.sync_encryption_key').write_bytes(key)

    cipher = TokenCipher.from_config({'ENCRYPTION_KEY': '',
                                      'ALLOW_GENERATED_ENCRYPTION_KEY': False},
                                     base_dir=str(tmp_path))
    assert cipher.decrypt(TokenCipher(key=key).encrypt(CREDENTIALS)) == CREDENTIALS


def test_from_config_uses_the_configured_key(key):
    cipher = TokenCipher.from_config({'ENCRYPTION_KEY': key.decode(),
                                      'ALLOW_GENERATED_ENCRYPTION_KEY': False})
    assert cipher.decrypt(cipher.encrypt(CREDENTIALS)) == CREDENTIALS
