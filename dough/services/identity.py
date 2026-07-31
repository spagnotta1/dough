"""The account lifecycle: creating one, proving an address, replacing a
password, and invalidating everything issued against it.  [Phase 10.5]

Allowed:   models, sqlalchemy, dough.auth (hashing only), dough.services, stdlib
Must not:  app, render_template, url_for, redirect, flash, jsonify, anthropic,
           blueprints, request, session

Nothing here reads a request. That is what lets `/register`, `/api/v1` and a CLI
reset share one implementation, and it is why the *links* are built by the
routes: `url_for` is a route's job, and a service that produced a URL would need
a request context to know the host.

## What this module is for

Before it, the only way an account came into existence was `/setup` (the first
one) or `/join` (an invitation), and each carried its own copy of "make a
household, make an owner, hash the password". Adding `/register` as a third copy
is how the three drift — and the field they drift on is validation, so the third
copy is the one that accepts a six-character password because whoever wrote it
was reading the `/join` branch that checks for eight.

So the rules live here, once, and `dough/blueprints/auth.py` translates the
refusal into a message.

## The single-use token, and why redemption is one function

`redeem()` is the only thing that consumes an `email_verifications` row, and it
does three things in one transaction: check the token is pending, stamp
`used_at`, and hand the caller the user. A caller cannot skip the stamp, because
it cannot get the user without it.

The alternative — a `find_token()` that returns a row and a `mark_used()` the
caller remembers to call — is the shape that produces a reusable password-reset
link. It only takes one path that returns early between the two calls (a
validation failure on the new password, say), and the token is still pending
after somebody has been shown the form it unlocked.

## Invalidation is never called explicitly for a password change

`dough/auth.py` installs a `before_flush` listener that raises
`AppUser.session_version` whenever `password_hash` changes, so
`set_password()` below does *not* bump it and must not. Doing both would raise it
twice, which is harmless today and would stop being harmless the moment
something compares the number to a value it recorded earlier.

`revoke_all_credentials()` is the separate, explicit act — "sign me out
everywhere" with no password change behind it — and it is the only function here
that touches the counter directly.

## What a failed lookup may say

`find_by_email()` returns None for "no such address" and for "an address that
belongs to an account with no password" alike, and the caller must not tell the
two apart in a response. The reasoning is `api_tokens.authenticate`'s, applied to
the surface where it matters most: `/forgot-password` is reachable by anybody,
so any difference in its output — wording, status, or time taken — is a free
oracle for which addresses have accounts.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta

__all__ = [
    'IdentityError',
    'MIN_PASSWORD_LENGTH',
    'TOKEN_BYTES',
    'find_by_email',
    'find_by_username',
    'hash_token',
    'mark_email_verified',
    'issue_token',
    'normalize_email',
    'redeem',
    'register_account',
    'revoke_all_credentials',
    'set_email',
    'set_password',
    'validate_email',
    'validate_password',
    'validate_username',
]


class IdentityError(Exception):
    """An account operation was refused. Carries a message written for a person.

    Same contract as `MembershipError`: the wording lives with the rule rather
    than in the route, so two surfaces refusing the same thing cannot refuse it
    with two different explanations.
    """


#: Eight, matching what `/setup` and `/join` have always enforced.
#:
#: Deliberately not raised to twelve while adding a third entry point. The number
#: is now checked in one place, so raising it later is a one-line change that
#: applies everywhere at once — whereas raising it *here and now* would mean
#: `/register` and `/setup` disagreed for as long as it took somebody to notice,
#: and the disagreement would be invisible to every test that only exercises one.
MIN_PASSWORD_LENGTH = 8

#: 256 bits, from `secrets.token_urlsafe`. The same size and the same reasoning
#: as `api_tokens.TOKEN_BYTES` and `membership`'s invite tokens: it is what lets
#: `hash_token` be an unsalted SHA-256 with no work factor.
TOKEN_BYTES = 32

#: How long a link stays good. Verification is generous because nothing is
#: blocked while it is unspent; a reset is not, because the window is exactly how
#: long a stolen mail is worth stealing.
VERIFICATION_TTL_HOURS = 48
RESET_TTL_MINUTES = 60

#: Deliberately permissive. This is not an attempt to decide which addresses
#: exist — RFC 5322 allows far more than anybody expects, and every stricter
#: pattern in the wild rejects somebody's real address. It rejects the shapes
#: that cannot be an address at all, and delivery is what actually proves one.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')

#: Letters, digits, and the three separators people actually use. Constrained
#: rather than free text because a username appears in URLs, in audit metadata
#: and in the invitation UI, and "no surprises in any of those" is cheaper to
#: guarantee here than to escape in three places.
_USERNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$')

#: Passwords refused regardless of length. A deliberately tiny list: a real
#: breached-password check needs a corpus this application has no way to ship,
#: and a long homegrown list mostly produces false confidence. These are the
#: handful that survive an eight-character minimum and are still guessed first.
_OBVIOUS_PASSWORDS = frozenset({
    'password', 'password1', 'password123', '12345678', '123456789',
    '1234567890', 'qwertyui', 'qwerty123', 'iloveyou', 'letmein1',
    'welcome1', 'admin123', 'football', 'baseball', 'sunshine',
    'princess', 'dragon12', 'monkey12', 'abc12345', 'passw0rd',
})


# ---------------------------------------------------------------------------
# Normalizing and validating
# ---------------------------------------------------------------------------

def normalize_email(value):
    """Lowercase and strip. The stored form, and the lookup form.

    Both, and it has to be both. Normalizing on the way in but not on the way out
    means `Sam@x.com` registers as a distinct account from `sam@x.com` *or* that
    the reset lookup misses the account it should find, depending on which side
    forgot — and which of those two happens is decided by whichever call site was
    written second.

    Only case and surrounding whitespace. Not the dots-and-plus-addressing
    normalization Gmail applies: that is one provider's routing rule, and
    applying it universally would merge two genuinely different addresses at
    every provider that treats them as different.
    """
    return (value or '').strip().lower()


def validate_email(value):
    """Return the normalized address, or raise `IdentityError`."""
    email = normalize_email(value)
    if not email:
        raise IdentityError('Please enter an email address.')
    if len(email) > 255:
        # The column's width. Checked here so the refusal is a sentence rather
        # than a database error surfacing as a 500.
        raise IdentityError('That email address is too long.')
    if not _EMAIL_RE.match(email):
        raise IdentityError('That does not look like an email address.')
    return email


def validate_username(value):
    """Return the stripped username, or raise `IdentityError`."""
    username = (value or '').strip()
    if not username:
        raise IdentityError('Please choose a username.')
    if not _USERNAME_RE.match(username):
        raise IdentityError(
            'Usernames are 3–40 characters, start with a letter or number, and '
            'use only letters, numbers, dots, dashes and underscores.')
    return username


def validate_password(password, confirm=None, *, username=None, email=None):
    """Raise `IdentityError` unless this password may be stored.

    The `username` and `email` checks are the ones worth having. Length and a
    blocklist stop the passwords everybody already knows; "not your own username"
    stops the one an attacker who has just read a list of usernames tries first,
    and it is the only rule here that gets stronger as the attacker learns more
    about the target.

    No composition rules — no "must contain a digit and a symbol". They push
    people towards `Password1!`, which satisfies every one of them and is on
    every list.
    """
    password = password or ''
    if len(password) < MIN_PASSWORD_LENGTH:
        raise IdentityError(
            f'Password must be at least {MIN_PASSWORD_LENGTH} characters.')
    if len(password) > 1024:
        # Not a security rule — scrypt's cost is in its parameters, not the
        # input length. It is a denial-of-service bound: hashing a 10 MB
        # "password" is work an unauthenticated caller can ask for repeatedly.
        raise IdentityError('That password is too long.')
    if confirm is not None and password != confirm:
        raise IdentityError('Passwords do not match.')
    if password.lower() in _OBVIOUS_PASSWORDS:
        raise IdentityError(
            'That password is one of the most commonly guessed. Please pick '
            'another.')
    lowered = password.lower()
    if username and username.lower() in lowered:
        raise IdentityError('Password must not contain your username.')
    if email:
        local = normalize_email(email).split('@')[0]
        if len(local) >= 3 and local in lowered:
            raise IdentityError('Password must not contain your email address.')
    return password


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def find_by_email(email):
    """The account holding this address, or None. Never raises.

    Never raising is part of the contract, not an accident of the
    implementation: the caller is `/forgot-password`, and an exception there
    would be a different response for a malformed address than for a
    well-formed one that matches nothing — which is the enumeration signal the
    whole route is built to avoid. A malformed address matches no account, and
    that is all this needs to say.
    """
    from models import AppUser

    normalized = normalize_email(email)
    if not normalized:
        return None
    return AppUser.query.filter(AppUser.email == normalized).first()


def find_by_username(username):
    """The account with this username, or None."""
    from models import AppUser

    name = (username or '').strip()
    if not name:
        return None
    return AppUser.query.filter(AppUser.username == name).first()


# ---------------------------------------------------------------------------
# Creating an account
# ---------------------------------------------------------------------------

def register_account(*, username, email, password, confirm=None,
                     household_name=None, now=None):
    """Create a household and its first owner, together, in one transaction.

    The pairing is the same invariant `/setup` states and for the same reason:
    a user with no household cannot see anything (every scoped query needs one),
    and a household with no owner is one nobody can administer. Neither may
    exist even briefly — `tools/verify_tenancy.py` reports both as failures.

    ## Why the duplicate checks are here *and* at the database

    `IntegrityError` is what actually guarantees uniqueness, because two
    simultaneous registrations both pass a `SELECT` before either commits. The
    checks above it exist to turn the common case into a sentence a person can
    act on ("that username is taken") instead of a 500, and the `except` below
    exists because the check cannot be trusted to have been the one that mattered.

    ## What a duplicate is allowed to reveal

    A taken *username* is said out loud. It has to be: the person is choosing
    one, and "registration failed" without saying why leaves them retyping the
    same value. Usernames are also already enumerable through `/join`, which has
    said "that username is taken" since Phase 6.

    A taken *email address* is not. That is the asymmetry, and it is deliberate:
    an address is an identifier somebody else chose and holds, and confirming one
    is registered here tells a stranger that this person banks with Dough. The
    caller is handed `IdentityError` with a message that names neither field —
    see `dough/blueprints/auth.py` for what it does with it.
    """
    from sqlalchemy.exc import IntegrityError

    from dough.auth import hash_password
    from models import AppUser, Household, ROLE_OWNER, db

    username = validate_username(username)
    email = validate_email(email)
    validate_password(password, confirm, username=username, email=email)

    if find_by_username(username) is not None:
        raise IdentityError('That username is taken. Please pick another.')
    if find_by_email(email) is not None:
        raise IdentityError(_DUPLICATE_EMAIL_MESSAGE)

    now = now or datetime.utcnow()
    household = Household(name=(household_name or f"{username}'s household"),
                          created_at=now, updated_at=now)
    db.session.add(household)
    db.session.flush()   # assigns household.id

    user = AppUser(username=username,
                   email=email,
                   password_hash=hash_password(password),
                   household_id=household.id,
                   role=ROLE_OWNER,
                   created_at=now)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # The race the SELECTs above cannot close. Rolled back so the session is
        # usable afterwards -- a caller that went on to record an audit event on
        # a poisoned session would fail on that instead, which is a confusing
        # place to discover a duplicate username.
        db.session.rollback()
        raise IdentityError(
            'That username or email address is already registered.') from None
    return user


#: Said to somebody whose address is already registered. It deliberately does not
#: say so. Reused by `set_email` so the two paths that can meet a taken address
#: cannot answer differently — a difference between them would be exactly the
#: oracle this wording exists to close.
_DUPLICATE_EMAIL_MESSAGE = (
    'That username or email address is already registered. If it is yours, try '
    'signing in or resetting your password.')


# ---------------------------------------------------------------------------
# Changing an account
# ---------------------------------------------------------------------------

def set_password(user, password, confirm=None, *, current_password=None):
    """Replace a password. Every credential the account holds stops working.

    The invalidation is *not* performed here. `dough/auth.py`'s `before_flush`
    listener sees `password_hash` change and raises `session_version` itself,
    which is what makes the guarantee hold for a caller that has never heard of
    this module — a CLI, a shell, a revision. Bumping here as well would raise it
    twice per change.

    `current_password` is verified when supplied, and supplying it is the
    caller's decision rather than this function's: the settings page must
    (the session might be somebody else's, sitting at an unlocked screen), and
    the reset flow cannot (whoever is resetting is, by definition, the person who
    does not know it). Making it mandatory here would leave the reset path
    passing something meaningless to satisfy the signature.
    """
    from dough.auth import hash_password, verify_password
    from models import db

    if current_password is not None:
        if not verify_password(user.password_hash, current_password):
            raise IdentityError('That is not your current password.')

    validate_password(password, confirm, username=user.username,
                      email=user.email)
    if current_password is not None and password == current_password:
        raise IdentityError('The new password must differ from the old one.')

    user.password_hash = hash_password(password)
    db.session.commit()
    return user


def set_email(user, email):
    """Point the account at a new address. Verification starts over.

    `email_verified_at` is cleared, always, including when the address is
    unchanged in anything but case. A proof attaches to an address, not to an
    account, so carrying the old timestamp forward would mark an unproven
    address as verified — and the address is where a password reset gets sent.
    """
    from sqlalchemy.exc import IntegrityError

    from models import db

    email = validate_email(email)
    existing = find_by_email(email)
    if existing is not None and existing.id != user.id:
        raise IdentityError(_DUPLICATE_EMAIL_MESSAGE)

    user.email = email
    user.email_verified_at = None
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise IdentityError(_DUPLICATE_EMAIL_MESSAGE) from None
    return user


def revoke_all_credentials(user):
    """Invalidate every session and every API token for this account.

    The explicit half of the mechanism `dough/auth.py` describes. A password
    change gets there through the listener; this is the button that gets there
    without one — "sign out everywhere", and whatever a compromised-account
    response looks like later.

    One increment covers both surfaces because both compare against the same
    number: the session cookie carries the value it was signed in under, and
    `api_tokens` stores the value each token was issued under. Nothing is swept
    and nothing is written to `api_tokens` at all, which is what makes this
    atomic — there is no second write that could be lost, so no token can survive
    by having been missed.

    Returns the new version, so a caller that wants to keep *this* session alive
    can re-stamp its cookie with it.
    """
    from models import db

    user.session_version = (user.session_version or 1) + 1
    db.session.commit()
    return user.session_version


# ---------------------------------------------------------------------------
# Tokens: verification and reset
# ---------------------------------------------------------------------------

def hash_token(token):
    """The stored form. Unsalted SHA-256 — see the `EmailVerification` model."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def issue_token(user, purpose, *, ttl=None, now=None, invalidate_previous=True):
    """Mint a single-use link token. Returns `(row, plaintext)`.

    The plaintext is returned and never stored, exactly as `issue_invite` and
    `api_tokens.issue` do it. If the caller loses it, the fix is to issue
    another.

    `invalidate_previous` marks this user's outstanding tokens *of the same
    purpose* as used, and defaults to on. Two reasons, and the second is the one
    that matters: it keeps "I clicked request three times, which mail do I use?"
    from having a wrong answer, and it means a reset requested by an attacker
    who has read the victim's inbox is cancelled the moment the victim requests
    their own.

    Scoped to the purpose, so requesting a reset does not silently cancel a
    pending address verification — two unrelated flows, and cancelling one from
    the other would be a bug nobody would think to look for.
    """
    from models import EmailVerification, VERIFICATION_PURPOSES, db

    if purpose not in VERIFICATION_PURPOSES:
        # A closed vocabulary, checked. A typo'd purpose would mint a token that
        # `redeem` can never match, and the symptom is a link that silently does
        # nothing rather than an error anybody can act on.
        raise IdentityError(f'Unknown verification purpose {purpose!r}.')
    if not user.email:
        raise IdentityError('That account has no email address on file.')

    now = now or datetime.utcnow()
    if ttl is None:
        ttl = (timedelta(minutes=RESET_TTL_MINUTES)
               if purpose == 'password_reset'
               else timedelta(hours=VERIFICATION_TTL_HOURS))

    if invalidate_previous:
        (EmailVerification.query
         .filter(EmailVerification.user_id == user.id,
                 EmailVerification.purpose == purpose,
                 EmailVerification.used_at.is_(None))
         .update({'used_at': now}, synchronize_session=False))

    plaintext = secrets.token_urlsafe(TOKEN_BYTES)
    row = EmailVerification(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        purpose=purpose,
        # The address as it stands *now*. `redeem` compares it back, so changing
        # the address after requesting a link retires the link.
        sent_to=user.email,
        created_at=now,
        expires_at=now + ttl,
    )
    db.session.add(row)
    db.session.commit()
    return row, plaintext


def redeem(token, purpose, *, now=None):
    """Spend a token. Returns `(row, user)`, or `(None, reason)`.

    The reason is for the audit log and never for the caller, exactly as
    `api_tokens.authenticate` returns one: unknown, expired, used, wrong-purpose
    and address-changed must all look identical from outside, because telling
    them apart confirms which tokens were once real.

    ## Why the stamp happens here

    `used_at` is written before this returns, in the same transaction that
    resolved the row. A caller cannot obtain the user without having spent the
    token, so there is no code path — not an early return, not a validation
    failure on the form the token unlocked, not an exception — that leaves a
    redeemable token behind after somebody has been let through.

    That costs something real and it is the right trade: a reset link is spent by
    *loading* the form, so a user who then fails the password rules has to
    request a new link. The alternative keeps the link alive across a window the
    application does not control, which is the window an attacker with the mail
    is waiting for.
    """
    from models import AppUser, EmailVerification, db

    now = now or datetime.utcnow()

    if not token or not isinstance(token, str):
        return None, 'malformed'

    row = EmailVerification.query.filter(
        EmailVerification.token_hash == hash_token(token.strip())).first()
    if row is None:
        return None, 'unknown'
    # Checked rather than filtered in the query above so the reason can be
    # distinguished for the audit log -- a token presented at the wrong route is
    # a different event from one that does not exist.
    if row.purpose != purpose:
        return None, 'wrong_purpose'
    if row.used_at is not None:
        return None, 'used'
    if row.expires_at <= now:
        return None, 'expired'

    user = db.session.get(AppUser, row.user_id)
    if user is None:
        return None, 'orphaned'
    # The address moved after the link was sent. Redeeming would act on an
    # address whose holder never proved they control it -- which is the whole
    # takeover this comparison exists to prevent.
    if normalize_email(user.email) != normalize_email(row.sent_to):
        return None, 'address_changed'

    row.used_at = now
    db.session.commit()
    return row, user


def mark_email_verified(user, *, now=None):
    """Record that the account's address has been proved reachable."""
    from models import db

    user.email_verified_at = now or datetime.utcnow()
    db.session.commit()
    return user
