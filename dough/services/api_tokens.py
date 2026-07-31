"""Issuing, verifying and revoking the credentials `/api/v1` accepts.  [Phase 10]

Allowed:   models, sqlalchemy, stdlib
Must not:  app, flask response helpers, dough.ai, dough.blueprints, request/session

This is the *only* module that reads `api_tokens`. That is not a style
preference — it is the isolation guarantee. `ApiToken` deliberately does not
carry `TenantScopedMixin` (the lookup runs before any household is bound, so it
cannot), which means the ORM backstop does not filter this table and every query
here states its own predicate. A second reader elsewhere would be a second place
that has to remember, and the whole reason `dough/services/audit.py` is written
the same way is that "remembered everywhere" is not a property anybody can
check.

## The three questions, kept apart

`authenticate()` answers *is this string a usable credential, and whose*. It is
the only function that touches an unverified caller-supplied value, so it is the
only one that has to be careful about timing, about what it logs, and about the
difference between "no such token" and "revoked" (which it deliberately does not
distinguish to the caller — see below).

`issue()` and `revoke()` are ordinary household-scoped operations. Their caller
is already authenticated and already has a household, so they take one as an
argument and filter on it like every other service here.

## What a failed authentication may say

Nothing specific. `authenticate()` returns `None` for unknown, revoked, expired,
malformed, and belongs-to-a-deleted-user alike. Telling a caller that a token
was *revoked* confirms it was once real, which tells somebody working through a
list of guesses that they are guessing in the right shape — the same reasoning
that makes `find_redeemable_invite` return None for every failure mode, and that
makes `find_owned` answer "not found" for "not yours".

The distinction is not lost, it is *relocated*: the reason lands in the audit log
under `api.token.rejected`, where an operator can read it and an attacker cannot.

## Why comparison is a hash lookup and not a scan

`authenticate()` hashes the presented string and looks the digest up on a unique
index. It never loads candidate rows and compares. That is what keeps it O(1)
rather than O(tokens), and it also removes the timing question entirely: there is
no comparison loop to leak how far a guess got, because there is no comparison —
the database either finds the digest or does not.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

__all__ = [
    'ApiTokenError',
    'SCOPE_READ',
    'SCOPE_WRITE',
    'TOKEN_BYTES',
    'TOKEN_PREFIX',
    'VALID_SCOPES',
    'authenticate',
    'hash_token',
    'household_tokens',
    'issue',
    'normalize_scopes',
    'revoke',
    'touch',
]


class ApiTokenError(Exception):
    """A token could not be issued or revoked. Carries a user-safe message."""


#: 32 bytes is 256 bits, which is what lets `hash_token` be an unsalted SHA-256
#: with no work factor. See the `ApiToken` docstring: there is no dictionary to
#: precompute against a value nobody chose.
TOKEN_BYTES = 32

#: Marks the string as this application's credential wherever it turns up. The
#: practical value is secret-scanning: a fixed, distinctive prefix is what lets
#: a scanner recognise one of these in a commit or a log and say so. A bare
#: base64 blob is indistinguishable from a hundred other things and gets missed.
TOKEN_PREFIX = 'dgh_'

#: How much of the token is kept in clear, for the revocation UI. Long enough to
#: tell three tokens apart at a glance, short enough to be worthless as a head
#: start -- it leaves well over 200 bits unknown.
PREFIX_LENGTH = 12

SCOPE_READ = 'read'
SCOPE_WRITE = 'write'

#: A closed set, for the same reason `AUDIT_EVENT_TYPES` is closed: a column
#: holding `write`, `Write` and `writes` is a permission system that cannot be
#: queried and cannot be reasoned about.
VALID_SCOPES = (SCOPE_READ, SCOPE_WRITE)

#: How often `last_used_at` is actually written. Every authenticated request
#: would otherwise be a write, which on SQLite means every read of the API takes
#: the single writer lock -- turning the busiest path in the application into
#: the one that serializes. A minute's resolution answers the only question the
#: column exists for ("is this credential still in use?") at a fraction of the
#: cost. See OPS-0012 for why write amplification is not a theoretical concern
#: on this database.
TOUCH_RESOLUTION_SECONDS = 60


def hash_token(token):
    """The stored form. Unsalted SHA-256 — see the module docstring."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def normalize_scopes(scopes):
    """Validate and canonicalize a scope list into its stored string.

    Order is canonicalized so two equivalent requests store the same value, and
    duplicates are collapsed. `read` is implied by `write` and added explicitly
    rather than inferred at check time: a token whose stored scopes do not say
    what it can do is a token whose permissions are only knowable by running the
    code that checks them.
    """
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(',')]
    requested = {s.strip().lower() for s in (scopes or []) if s and s.strip()}
    if not requested:
        requested = {SCOPE_READ}
    unknown = requested - set(VALID_SCOPES)
    if unknown:
        raise ApiTokenError(
            f'Unknown scope: {", ".join(sorted(unknown))}. '
            f'Valid scopes are {", ".join(VALID_SCOPES)}.')
    if SCOPE_WRITE in requested:
        requested.add(SCOPE_READ)
    return ','.join(s for s in VALID_SCOPES if s in requested)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def household_tokens(household_id, *, include_revoked=True):
    """Every token this household has issued, newest first.

    Revoked ones are included by default. A revoked credential is part of the
    record of what existed — hiding it makes "was that phone's token ever
    revoked?" unanswerable from the product, which is precisely the question
    somebody asks after losing a phone.
    """
    from models import ApiToken

    query = ApiToken.query.filter(ApiToken.household_id == household_id)
    if not include_revoked:
        query = query.filter(ApiToken.revoked_at.is_(None))
    return query.order_by(ApiToken.created_at.desc()).all()


def authenticate(presented, *, now=None):
    """Resolve a presented token string to `(token, user)`, or `(None, reason)`.

    The reason is for the audit log and the application log, never for the
    caller — see the module docstring. It is returned rather than logged here so
    this function stays free of Flask and callable from a test with no request.

    The user is re-read on every request rather than trusted from the token row,
    which is what makes four things true without any extra machinery: a removed
    member's tokens stop working, a demoted owner's tokens lose owner powers, a
    token whose user was deleted fails closed instead of authenticating as a
    dangling foreign key, and — since Phase 10.5 — a token issued before the
    account's credentials were invalidated is refused, because the row that says
    which generation is current is already in hand.
    """
    from models import ApiToken, AppUser

    now = now or datetime.utcnow()

    if not presented or not isinstance(presented, str):
        return None, 'malformed'
    presented = presented.strip()
    # Checked before the database is touched. It costs nothing and it means the
    # overwhelmingly common garbage case -- a client sending a session cookie,
    # an empty header, a placeholder from a config file -- never becomes a query.
    if not presented.startswith(TOKEN_PREFIX) or len(presented) < 24:
        return None, 'malformed'

    token = ApiToken.query.filter(
        ApiToken.token_hash == hash_token(presented)).first()
    if token is None:
        return None, 'unknown'

    # Belt and braces over the unique index. The lookup above already proves the
    # digest matched, so this can only fail if two distinct tokens collided on
    # SHA-256; `compare_digest` rather than `==` keeps the habit consistent with
    # `dough.auth.validate_csrf` and costs a few microseconds once per request.
    if not hmac.compare_digest(token.token_hash, hash_token(presented)):
        return None, 'unknown'

    if token.revoked_at is not None:
        return None, 'revoked'
    if token.expires_at is not None and token.expires_at <= now:
        return None, 'expired'

    user = AppUser.query.filter(
        AppUser.id == token.user_id,
        # Stated explicitly rather than trusting the token's own column. If the
        # two ever disagreed -- a restore from a backup taken mid-migration, a
        # hand-edited row -- the safe reading is that the credential is invalid,
        # not that either value should be believed.
        AppUser.household_id == token.household_id).first()
    if user is None:
        return None, 'orphaned'

    # The credential generation check.  [Phase 10.5] A password change raises
    # `AppUser.session_version`, and every token stamped with an older value
    # stops working here — the same number, on the same request, that
    # `dough.auth.session_is_current` checks the browser session against.
    #
    # Deliberately *after* the user lookup and not a join in the query above:
    # this must be able to tell "wrong generation" from "no such user", because
    # the two land in the audit log as different reasons and an operator reading
    # `api.token.rejected` needs to know which happened.
    if token.session_version != user.session_version:
        return None, 'stale'

    return token, user


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def issue(household_id, user, *, name, scopes=None, ttl_days=None, now=None):
    """Create a token and return `(token_row, plaintext)`.

    The plaintext is returned and never stored, exactly as `issue_invite` does
    it. It exists for as long as it takes to serialize one response; if the
    caller loses it the fix is to revoke and issue another, which is the same
    property that makes storing only the hash worth doing.
    """
    from models import ApiToken, db

    label = (name or '').strip()
    if not label:
        raise ApiTokenError('Give the token a name so you can recognise it later.')
    if user.household_id != household_id:
        # Cannot happen through the routes, which read both from the same
        # request. Stated anyway because this function mints a credential, and
        # the cost of the check is nothing against what it would prevent.
        raise ApiTokenError('That user does not belong to this household.')

    now = now or datetime.utcnow()
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    token = ApiToken(
        household_id=household_id,
        user_id=user.id,
        name=label[:80],
        token_hash=hash_token(plaintext),
        prefix=plaintext[:PREFIX_LENGTH],
        scopes=normalize_scopes(scopes),
        # Stamped at issue, compared at every use.  [Phase 10.5] `or 1` covers
        # a user object built before the column existed -- a fixture, a partial
        # load -- for which the stored default is what the row would have had.
        session_version=user.session_version or 1,
        created_at=now,
        expires_at=now + timedelta(days=ttl_days) if ttl_days else None,
    )
    db.session.add(token)
    db.session.commit()
    return token, plaintext


def revoke(household_id, token_id, *, now=None):
    """Make a token unusable. Idempotent; an already-revoked one stays revoked.

    Revoked rather than deleted, and the row is kept. Deleting it would remove
    the evidence that the credential ever existed, which is the record somebody
    reviewing an incident needs most. `household_tokens` still lists it.
    """
    from models import ApiToken, db

    token = ApiToken.query.filter(
        ApiToken.id == token_id,
        ApiToken.household_id == household_id).first()
    if token is None:
        # Same answer for "no such id" and "another household's id" -- otherwise
        # this route is an oracle for how many tokens exist elsewhere in the
        # installation. Identical reasoning to `membership._member`.
        raise ApiTokenError('That token no longer exists.')
    if token.revoked_at is None:
        token.revoked_at = now or datetime.utcnow()
        db.session.commit()
    return token


def touch(token, *, now=None):
    """Record that a token was used, at minute resolution.

    Coarse on purpose. See `TOUCH_RESOLUTION_SECONDS`: writing on every request
    would make every authenticated read take SQLite's writer lock, so the
    busiest path in the application would be the one that serializes against
    itself. The column answers "is this still in use?", and that question does
    not need second-level precision.

    Failures are swallowed. This is bookkeeping attached to a request that has
    already been authorized; letting a write contention here turn a successful
    API call into a 500 would trade a real response for a cosmetic field.
    """
    from models import db

    now = now or datetime.utcnow()
    if (token.last_used_at is not None
            and (now - token.last_used_at).total_seconds() < TOUCH_RESOLUTION_SECONDS):
        return False
    try:
        token.last_used_at = now
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False
