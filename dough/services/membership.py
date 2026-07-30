"""Who is in a household, and how they got there.  [Phase 6]

`dough/tenancy.py` decides which rows a household may see. `dough/auth.py`
decides who is asking. This module holds the third question — who belongs to a
household at all — and it is the only place membership changes.

Everything here is deliberately free of Flask. Two of these operations are
reachable from a route that has no session and no bound household (redeeming an
invitation is done by somebody who is, by definition, not yet a member), so the
caller passes in what it knows rather than this module reaching for it. That
also means the last-owner rule can be tested without a request.

## The rule this module exists to keep

**A household always has at least one owner.** `tools/verify_tenancy.py` checks
it against the database and reports a household without one as a failure; a
household with only members is one nobody can invite to, rename, or remove
anyone from — it is not a recoverable state through the UI, only through SQL.

It is enforced by writing first and counting afterwards. Reading the owner count
and then deciding is a check-then-act race: two owners demoting each other at
the same moment each see the other still in place and both succeed. Flushing the
change and *then* counting means the count includes our own uncommitted write,
so one of the two transactions sees zero owners and rolls back. SQLite's single
writer makes this hard to hit today, which is exactly why it is worth writing
the version that stays correct if the database ever changes.

Allowed:   sqlalchemy, models, dough.tenancy, stdlib
Must not:  app, anthropic, flask response helpers, dough.ai
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

__all__ = [
    'InviteError',
    'LastOwnerError',
    'MembershipError',
    'accept_invite',
    'find_redeemable_invite',
    'hash_invite_token',
    'household_members',
    'household_invites',
    'issue_invite',
    'remove_member',
    'revoke_invite',
    'set_member_role',
]

#: How many bytes of entropy an invitation token carries. 32 bytes is 256 bits,
#: which is the reason `hash_invite_token` can be a bare SHA-256 with no salt
#: and no work factor: there is no dictionary to precompute against a value
#: nobody chose. See the HouseholdInvite docstring.
TOKEN_BYTES = 32


class MembershipError(Exception):
    """Base for the refusals this module raises. Carries a user-safe message."""


class LastOwnerError(MembershipError):
    """The operation would leave a household with no owner."""


class InviteError(MembershipError):
    """An invitation could not be issued, revoked, or redeemed."""


def hash_invite_token(token):
    """The stored form of an invitation token.

    Bare SHA-256, no salt, no iterations — the opposite of the advice for
    passwords, and correct for the same reason it is wrong there. The input is
    256 bits from `secrets.token_urlsafe`, so there is nothing to guess and a
    work factor would only slow the person redeeming a legitimate link.
    """
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def household_members(household_id):
    """Everyone in this household, owners first then alphabetical.

    `AppUser` does not carry `TenantScopedMixin` — it cannot, because login has
    to find a user before a household is known — so the `household_id` filter
    here is not defence in depth backed by the ORM. It is the only thing
    separating households, and every query in this module states it explicitly
    for that reason.
    """
    from models import AppUser, ROLE_OWNER

    return (AppUser.query
            .filter(AppUser.household_id == household_id)
            .order_by((AppUser.role != ROLE_OWNER), AppUser.username)
            .all())


def household_invites(household_id):
    """Every invitation this household has issued, newest first."""
    from models import HouseholdInvite

    return (HouseholdInvite.query
            .filter(HouseholdInvite.household_id == household_id)
            .order_by(HouseholdInvite.created_at.desc())
            .all())


def find_redeemable_invite(token, now=None):
    """The pending invitation this token unlocks, or None.

    Returns None for unknown, expired, revoked and already-accepted alike. The
    caller must not distinguish them to the person following the link: "this
    token was revoked" confirms it was once real, which tells somebody guessing
    that they are guessing in the right shape.

    **The caller is responsible for the query being unscoped.** This resolves a
    row before any household is bound — the token is what says which household
    is involved — and it is the one lookup in the application that legitimately
    does so. `app.py` binds `tenant_scope(invite.household_id)` immediately
    after, so everything downstream is ordinary scoped work.
    """
    from models import HouseholdInvite

    if not token:
        return None
    invite = HouseholdInvite.query.filter(
        HouseholdInvite.token_hash == hash_invite_token(token)).first()
    if invite is None:
        return None
    if invite.state(now) != 'pending':
        return None
    return invite


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def issue_invite(household_id, created_by, *, role, label=None, ttl_hours=72,
                 now=None):
    """Create an invitation and return `(invite, plaintext_token)`.

    The plaintext is returned and never stored. It exists for exactly as long as
    it takes to render the link once; if the owner loses it, the fix is to
    revoke and issue another, which is the same guarantee that makes the stored
    hash worth having.
    """
    from models import HouseholdInvite, ROLE_MEMBER, ROLE_OWNER, db

    if role not in (ROLE_OWNER, ROLE_MEMBER):
        raise InviteError(f'Unknown role {role!r}.')

    now = now or datetime.utcnow()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    invite = HouseholdInvite(
        household_id=household_id,
        token_hash=hash_invite_token(token),
        role=role,
        label=(label or '').strip() or None,
        created_by_id=created_by.id,
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    db.session.add(invite)
    db.session.commit()
    return invite, token


def revoke_invite(household_id, invite_id, now=None):
    """Mark an invitation unusable. Idempotent; already-revoked stays revoked."""
    from models import HouseholdInvite, db

    invite = HouseholdInvite.query.filter(
        HouseholdInvite.id == invite_id,
        HouseholdInvite.household_id == household_id).first()
    if invite is None:
        raise InviteError('That invitation no longer exists.')
    if invite.accepted_at:
        raise InviteError('That invitation has already been accepted.')
    if not invite.revoked_at:
        invite.revoked_at = now or datetime.utcnow()
        db.session.commit()
    return invite


def accept_invite(invite, username, password_hash, now=None):
    """Create the invited member and consume the invitation, in one transaction.

    Both halves or neither. A user created against an invitation that stayed
    pending would let the same link be used again; an invitation consumed with
    no user created would burn it for nothing.

    The re-check of `is_redeemable` inside the transaction is not belt and
    braces for its own sake — `find_redeemable_invite` runs before the username
    is even validated, and two people following the same link at the same moment
    would both pass that earlier check.
    """
    from models import AppUser, db

    if not invite.is_redeemable:
        raise InviteError('That invitation is no longer valid.')

    now = now or datetime.utcnow()
    user = AppUser(
        username=username,
        password_hash=password_hash,
        household_id=invite.household_id,
        role=invite.role,
    )
    db.session.add(user)
    db.session.flush()          # assigns user.id

    invite.accepted_at = now
    invite.accepted_by_id = user.id
    db.session.commit()
    return user


def set_member_role(household_id, user_id, role, *, acting_user_id=None):
    """Promote or demote a member, unless it would leave no owner behind."""
    from models import AppUser, ROLE_MEMBER, ROLE_OWNER, db

    if role not in (ROLE_OWNER, ROLE_MEMBER):
        raise MembershipError(f'Unknown role {role!r}.')

    member = _member(household_id, user_id)
    if member.role == role:
        return member

    member.role = role
    _commit_keeping_an_owner(
        household_id,
        'That would leave the household with no owner. '
        'Promote someone else first.')
    return member


def remove_member(household_id, user_id, *, acting_user_id=None):
    """Remove somebody from the household.

    Their financial data is not theirs to take: transactions, budgets and
    connections belong to the household, so nothing cascades and nothing is
    deleted beyond the login itself. That is the whole reason `AppUser` has no
    dependent scoped rows hanging off it.

    Removing yourself is refused. It is not a safety property — an owner can
    always be removed by another owner — but a self-removal that succeeded
    would 500 the very next request, and "log out" is what the person actually
    wanted in every case where they were not the last owner anyway.
    """
    from models import db

    if acting_user_id is not None and int(acting_user_id) == int(user_id):
        raise MembershipError(
            'You cannot remove yourself. Ask another owner, or sign out.')

    member = _member(household_id, user_id)
    db.session.delete(member)
    _commit_keeping_an_owner(
        household_id,
        'That would leave the household with no owner. '
        'Promote someone else first.')
    return member


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _member(household_id, user_id):
    from models import AppUser

    member = AppUser.query.filter(
        AppUser.id == user_id,
        AppUser.household_id == household_id).first()
    if member is None:
        # Same message whether the id is unknown or belongs to another
        # household. Distinguishing them would turn this route into an oracle
        # for which user ids exist elsewhere in the installation.
        raise MembershipError('That person is not in this household.')
    return member


def _commit_keeping_an_owner(household_id, message):
    """Flush, count owners, and roll back if the change orphaned the household.

    Write-then-check, not check-then-write. See the module docstring: the count
    has to see our own pending change, which is the only way two simultaneous
    demotions cannot both believe the other owner is still there.
    """
    from models import AppUser, ROLE_OWNER, db

    db.session.flush()
    owners = (db.session.query(AppUser)
              .filter(AppUser.household_id == household_id,
                      AppUser.role == ROLE_OWNER)
              .count())
    if owners == 0:
        db.session.rollback()
        raise LastOwnerError(message)
    db.session.commit()
