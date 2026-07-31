"""`/api/v1/household` — who is in it, and who may change that.

Every rule is `dough/services/membership.py`: the last-owner invariant, the
transaction boundaries, and every refusal message. This module decides who may
call what and turns a `MembershipError` into a 409.

## Why refusals are 409 and not 422

`MembershipError` means the request was well-formed and the household's state
does not permit it — demoting the last owner, removing yourself. That is a
conflict with current state, which is what 409 says. 422 would claim a field was
wrong, and no field is: the same request would succeed once somebody else is
promoted.
"""

from __future__ import annotations

from flask import Blueprint, current_app

from dough.api.envelope import created, no_content, ok
from dough.api.errors import Conflict
from dough.api.validation import body, optional_str, require_str
from dough.auth import current_user, owner_required
from dough.services import audit
from dough.services.membership import (MembershipError, household_invites,
                                       household_members, issue_invite,
                                       remove_member, revoke_invite,
                                       set_member_role)
from dough.tenancy import require_household, unscoped

from models import (EVENT_INVITE_CREATED, EVENT_INVITE_REVOKED,
                    EVENT_MEMBER_REMOVED, EVENT_ROLE_CHANGED, ROLE_MEMBER,
                    ROLE_OWNER)

bp = Blueprint('api_v1_household', __name__)


def _serialize_member(user, *, me=None):
    return {
        'id': user.id,
        'username': user.username,
        'role': user.role,
        'is_owner': user.is_owner,
        'is_you': me is not None and user.id == me.id,
        'created_at': user.created_at.isoformat() + 'Z',
    }


def _serialize_invite(invite):
    """An invitation, minus the one thing that would make it usable.

    There is no branch here that could emit the token. It is not stored — only
    its hash is — so this is not a filter that could be forgotten; the plaintext
    genuinely does not exist by the time anything lists an invitation.
    """
    return {
        'id': invite.id,
        'role': invite.role,
        'label': invite.label,
        'state': invite.state(),
        'created_at': invite.created_at.isoformat() + 'Z',
        'expires_at': invite.expires_at.isoformat() + 'Z',
        'accepted_at': (invite.accepted_at.isoformat() + 'Z'
                        if invite.accepted_at else None),
        'revoked_at': (invite.revoked_at.isoformat() + 'Z'
                       if invite.revoked_at else None),
    }


@bp.route('/household', methods=['GET'])
def get_household():
    """The household, its members, and its outstanding invitations."""
    from models import Household, db

    household_id = require_household()
    with unscoped():   # Household is the tenant; it has no household of its own
        home = db.session.get(Household, household_id)

    me = current_user()
    return ok({
        'id': household_id,
        'name': home.name if home else None,
        'created_at': home.created_at.isoformat() + 'Z' if home else None,
        'members': [_serialize_member(u, me=me)
                    for u in household_members(household_id)],
        'invites': [_serialize_invite(i) for i in household_invites(household_id)],
        'roles': [ROLE_OWNER, ROLE_MEMBER],
    })


@bp.route('/household/members', methods=['GET'])
def list_members():
    me = current_user()
    return ok([_serialize_member(u, me=me)
               for u in household_members(require_household())])


@bp.route('/household/members/<int:user_id>', methods=['PATCH'])
@owner_required
def update_member(user_id):
    """Promote or demote a member.

    `@owner_required` rather than a tenancy check, and the distinction is the
    one ADR-0009 §6 makes: a member of household 7 demoting household 7's owner
    never crosses a tenant boundary, so no version of the ORM backstop could
    catch it. This is a role question and it is answered at the route.
    """
    data = body()
    role = require_str(data, 'role', choices=(ROLE_OWNER, ROLE_MEMBER))

    was = _member_snapshot(user_id)
    try:
        member = set_member_role(require_household(), user_id, role,
                                 acting_user_id=_acting_user_id())
    except MembershipError as exc:
        raise Conflict(str(exc))

    audit.record(EVENT_ROLE_CHANGED, entity_type='user', entity_id=user_id,
                 metadata={'username': was.get('username'),
                           'from': was.get('role'), 'to': role,
                           'surface': 'api'})
    return ok(_serialize_member(member, me=current_user()))


@bp.route('/household/members/<int:user_id>', methods=['DELETE'])
@owner_required
def delete_member(user_id):
    """Remove somebody from the household.

    Their financial data stays — transactions, budgets and connections belong to
    the household, not to the person. That is the whole reason `AppUser` has no
    dependent scoped rows hanging off it.
    """
    # Read before the change: after a successful removal the row this names is
    # gone, and `entity_id=4` alone is not a record anybody can read a year later.
    was = _member_snapshot(user_id)
    try:
        remove_member(require_household(), user_id,
                      acting_user_id=_acting_user_id())
    except MembershipError as exc:
        raise Conflict(str(exc))

    audit.record(EVENT_MEMBER_REMOVED, entity_type='user', entity_id=user_id,
                 metadata={**was, 'surface': 'api'})
    return no_content()


@bp.route('/household/invites', methods=['GET'])
def list_invites():
    return ok([_serialize_invite(i) for i in household_invites(
        require_household())])


@bp.route('/household/invites', methods=['POST'])
@owner_required
def create_invite():
    """Issue an invitation link.

    The plaintext token is returned exactly once, here, and never stored — the
    same contract as `/auth/tokens`. The web route puts it in the session rather
    than the response so it stays out of browser history and referrer headers; a
    native client has neither, so returning it directly is both safe and the
    only thing that would work.

    `join_url` is built from `PUBLIC_BASE_URL` when set, because `url_for
    (_external=True)` would produce whatever host this particular request
    arrived on — which for an API call from a phone on the LAN is an address the
    invited person cannot reach.
    """
    data = body()
    role = optional_str(data, 'role', choices=(ROLE_OWNER, ROLE_MEMBER)) or ROLE_MEMBER
    ttl = current_app.config['INVITE_TTL_HOURS']

    try:
        invite, token = issue_invite(
            require_household(), current_user(), role=role,
            label=optional_str(data, 'label', max_length=120) or None,
            ttl_hours=ttl)
    except MembershipError as exc:
        raise Conflict(str(exc))

    # `token` is deliberately absent from the metadata. It is a bearer
    # credential and the audit log is the one table nothing deletes from.
    audit.record(EVENT_INVITE_CREATED, entity_type='invite',
                 entity_id=invite.id,
                 metadata={'role': invite.role, 'label': invite.label,
                           'expires_at': invite.expires_at, 'surface': 'api'})

    return created({**_serialize_invite(invite),
                    'token': token, 'shown_once': True,
                    'join_url': _join_url(token),
                    'expires_in_hours': ttl})


def _join_url(token):
    base = (current_app.config.get('PUBLIC_BASE_URL') or '').rstrip('/')
    if base:
        return f'{base}/join/{token}'
    # No configured public URL. The path is still useful to a client that knows
    # its own base; a guessed absolute URL would not be.
    return f'/join/{token}'


@bp.route('/household/invites/<int:invite_id>', methods=['DELETE'])
@owner_required
def delete_invite(invite_id):
    """Revoke an invitation. Idempotent — already-revoked stays revoked."""
    try:
        revoke_invite(require_household(), invite_id)
    except MembershipError as exc:
        raise Conflict(str(exc))
    audit.record(EVENT_INVITE_REVOKED, entity_type='invite',
                 entity_id=invite_id, metadata={'surface': 'api'})
    return no_content()


@bp.route('/household/activity', methods=['GET'])
def activity():
    """This household's audit trail, most recent first.

    Read through `dough/services/audit.py::recent`, which is the single function
    that filters this table by household — `AuditEvent` is outside the ORM
    backstop, so that function is the isolation guarantee and reading the table
    any other way here would be the second place that has to remember.
    """
    from dough.api.pagination import int_arg

    limit = int_arg('limit', default=current_app.config['AUDIT_PAGE_SIZE'],
                    minimum=1, maximum=500)
    return ok([e.to_dict() for e in audit.recent(limit=limit)])


def _acting_user_id():
    user = current_user()
    return user.id if user else None


def _member_snapshot(user_id):
    """What to remember about a member before an operation changes them."""
    from models import AppUser, db

    user = db.session.get(AppUser, user_id)
    if user is None or user.household_id != require_household():
        return {}
    return {'username': user.username, 'role': user.role}
