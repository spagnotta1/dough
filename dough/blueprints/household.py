"""Membership: who is in this household, who may invite, who may leave.

The rules are not here. The last-owner invariant, the transaction boundaries and
every refusal message live in `dough/services/membership.py`, so they can be
tested without a request and cannot be half-applied by a second caller. These
views do the three things a view should: decide who may call them, translate
form input, and turn a refusal into a message.

`@owner_required` is the whole role model, and it has to be here rather than in
the ORM backstop. A member of household 7 removing household 7's owner never
crosses a tenant boundary -- every row involved belongs to the household doing
the asking -- so there is no version of the tenancy guard that could catch it.
See ADR-0009 §6.
"""

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)

from dough.auth import current_user, owner_required
from dough.services import audit
from dough.services.membership import (MembershipError, household_invites,
                                       household_members, issue_invite,
                                       remove_member, revoke_invite,
                                       set_member_role)
from dough.tenancy import require_household
from models import (AppUser, EVENT_INVITE_CREATED, EVENT_INVITE_REVOKED,
                    EVENT_MEMBER_REMOVED, EVENT_ROLE_CHANGED, Household,
                    ROLE_MEMBER, ROLE_OWNER, db)

bp = Blueprint('household', __name__, url_prefix='/household')


def _member_or_flash(action, *args, **kwargs):
    """Run a membership operation, turning its refusal into a flash.

    Every one of these raises `MembershipError` with a message written for the
    person reading it, so there is nothing to translate here -- which is the
    point of the exception carrying the wording rather than a code the route has
    to map.
    """
    try:
        action(*args, **kwargs)
        return True
    except MembershipError as exc:
        flash(str(exc), 'error')
        return False


@bp.route('')
def index():
    me = current_user()
    home = db.session.get(Household, require_household())
    return render_template(
        'household.html',
        household=home,
        me=me,
        members=household_members(home.id),
        invites=household_invites(home.id),
        invite_link=session.pop('_new_invite_link', None),
        roles=(ROLE_OWNER, ROLE_MEMBER),
    )


@bp.route('/invites', methods=['POST'])
@owner_required
def invite_create():
    me = current_user()
    role = request.form.get('role', ROLE_MEMBER)
    ttl = current_app.config['INVITE_TTL_HOURS']
    try:
        invite, token = issue_invite(
            require_household(), me, role=role,
            label=request.form.get('label'), ttl_hours=ttl)
    except MembershipError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('household.index'))

    # The plaintext token exists only here and is never stored. It goes through
    # the session rather than the query string so it does not end up in a
    # browser history entry, a referrer header, or the access log of whatever
    # sits in front of this app -- all three of which outlive the page that
    # displays it.
    session['_new_invite_link'] = url_for('auth.join', token=token,
                                          _external=True)
    # `token` is deliberately absent from the metadata. It is a bearer
    # credential, and the audit log is the one table nothing ever deletes from.
    audit.record(EVENT_INVITE_CREATED, entity_type='invite',
                 entity_id=invite.id,
                 metadata={'role': invite.role, 'label': invite.label,
                           'expires_at': invite.expires_at})
    flash(f'Invitation created. The link works once, '
          f'and expires in {ttl} hours.', 'success')
    return redirect(url_for('household.index'))


@bp.route('/invites/<int:invite_id>/revoke', methods=['POST'])
@owner_required
def invite_revoke(invite_id):
    if _member_or_flash(revoke_invite, require_household(), invite_id):
        audit.record(EVENT_INVITE_REVOKED, entity_type='invite',
                     entity_id=invite_id)
        flash('Invitation revoked.', 'success')
    return redirect(url_for('household.index'))


@bp.route('/members/<int:user_id>/role', methods=['POST'])
@owner_required
def member_role(user_id):
    role = request.form.get('role', ROLE_MEMBER)
    # Read before the change: "role changed to member" is half a sentence, and
    # the half it leaves out is the one an incident review needs.
    was = _member_snapshot(user_id)
    if _member_or_flash(set_member_role, require_household(), user_id, role,
                        acting_user_id=session.get('user_id')):
        audit.record(EVENT_ROLE_CHANGED, entity_type='user', entity_id=user_id,
                     metadata={'username': was.get('username'),
                               'from': was.get('role'), 'to': role})
        flash('Role updated.', 'success')
    return redirect(url_for('household.index'))


@bp.route('/members/<int:user_id>/remove', methods=['POST'])
@owner_required
def member_remove(user_id):
    # Same reason as above, more so: after a successful removal the row this
    # names may be gone, and `entity_id=4` on its own is not a record of
    # anything a person can read a year later.
    was = _member_snapshot(user_id)
    if _member_or_flash(remove_member, require_household(), user_id,
                        acting_user_id=session.get('user_id')):
        audit.record(EVENT_MEMBER_REMOVED, entity_type='user',
                     entity_id=user_id, metadata=was)
        flash('Removed from the household.', 'success')
    return redirect(url_for('household.index'))


def _member_snapshot(user_id):
    """What to remember about a member before an operation changes them."""
    user = db.session.get(AppUser, user_id)
    if user is None or user.household_id != require_household():
        return {}
    return {'username': user.username, 'role': user.role}
