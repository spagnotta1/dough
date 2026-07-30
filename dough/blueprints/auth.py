"""Signing in, signing out, first-run setup, and redeeming an invitation.

These four are grouped because they are the only views that run for somebody the
application has not yet identified. Everything else in `dough/blueprints/`
executes with a user and a household already bound.

The session *guard* is not here. `_require_login` and the session-lifetime
checks are registered on the application in `app.create_app`, because they apply
to every request rather than to these routes, and a before_request hook that
lived on a blueprint would only ever see that blueprint's traffic -- which is
precisely backwards for a default-deny rule.
"""

import time

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)

from dough.auth import (LoginThrottle, client_address, hash_password,
                        needs_rehash, public, verify_password)
from dough.services import audit
from dough.services.membership import MembershipError, accept_invite, find_redeemable_invite
from dough.tenancy import tenant_scope, unscoped
from models import (AppUser, EVENT_INVITE_ACCEPTED, EVENT_LOGIN_FAILED,
                    EVENT_LOGIN_SUCCEEDED, EVENT_LOGIN_THROTTLED, EVENT_LOGOUT,
                    EVENT_PASSWORD_REHASHED, EVENT_SETUP_COMPLETED, Household,
                    ROLE_OWNER, db)

bp = Blueprint('auth', __name__)


@bp.record_once
def _install_throttle(state):
    """One throttle per application, not one per process.

    It was a closure over `create_app`, which gave the same guarantee for free.
    A module-level instance would not: the suite builds many applications in one
    process, and they would have shared a counter -- so a test that exhausted
    the limit would fail an unrelated test that ran afterwards.
    """
    state.app.extensions['dough_login_throttle'] = LoginThrottle()


def _throttle():
    return current_app.extensions['dough_login_throttle']


def _sign_in(user):
    """Start a fresh session for this user.

    `session.clear()` before writing anything is session fixation defence: an
    attacker who plants a known session id in a victim's browser must not still
    hold a valid one after the victim signs in. It also drops the pre-login CSRF
    token, so a token minted on the login page cannot be replayed against the
    authenticated session.
    """
    session.clear()
    session['user_id'] = user.id
    session['signed_in_at'] = int(time.time())
    session['seen_at'] = int(time.time())
    session.permanent = True


def _safe_next():
    nxt = request.args.get('next', '/')
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = '/'
    return nxt


@bp.route('/setup', methods=['GET', 'POST'])
@public
def setup():
    if AppUser.query.first():
        return redirect(url_for('auth.login'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username:
            error = 'Please choose a username.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif password != request.form.get('confirm', ''):
            error = 'Passwords do not match.'
        else:
            # The household and its first owner are created together, in one
            # transaction. A user with no household cannot see anything (every
            # scoped query needs one) and a household with no owner is one
            # nobody can administer, so neither is allowed to exist even
            # briefly -- tools/verify_tenancy.py reports both as failures.
            household = Household(name=f"{username}'s household")
            db.session.add(household)
            db.session.flush()   # assigns household.id
            user = AppUser(username=username,
                           password_hash=hash_password(password),
                           household_id=household.id,
                           role=ROLE_OWNER)
            db.session.add(user)
            db.session.commit()
            _sign_in(user)
            # Explicit ids on every audit call in this module: the tenancy hook
            # binds the household in before_request, and none of these four
            # views had a signed-in user when that ran. Left to default, they
            # would all record a null household.
            audit.record(EVENT_SETUP_COMPLETED, household_id=household.id,
                         actor_user_id=user.id, entity_type='household',
                         entity_id=household.id,
                         metadata={'username': username})
            return redirect('/')
    return render_template('setup.html', error=error)


@bp.route('/login', methods=['GET', 'POST'])
@public
def login():
    if not AppUser.query.first():
        return redirect(url_for('auth.setup'))
    error = None
    if request.method == 'POST':
        address = client_address()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        throttle = _throttle()
        locked = throttle.lock_reason(address, username)
        if locked:
            current_app.logger.warning('Throttled sign-in (%s bucket) for %r',
                                       locked, username)
            audit.record(EVENT_LOGIN_THROTTLED,
                         metadata={'username': username, 'bucket': locked})
            # See LoginThrottle.lock_reason: the address lock says so, the
            # account lock stays silent so it cannot be used to test whether a
            # username exists.
            error = ('Too many failed attempts — try again in 15 minutes.'
                     if locked == 'address'
                     else 'Invalid username or password.')
        else:
            user = AppUser.query.filter_by(username=username).first()
            if user and verify_password(user.password_hash, password):
                if needs_rehash(user.password_hash):
                    # The only moment the plaintext exists to re-derive from. A
                    # hash stored under an older KDF cannot be upgraded by a
                    # migration.
                    user.password_hash = hash_password(password)
                    db.session.commit()
                    audit.record(EVENT_PASSWORD_REHASHED,
                                 household_id=user.household_id,
                                 actor_user_id=user.id,
                                 entity_type='user', entity_id=user.id)
                throttle.record_success(address, username)
                _sign_in(user)
                audit.record(EVENT_LOGIN_SUCCEEDED,
                             household_id=user.household_id,
                             actor_user_id=user.id,
                             entity_type='user', entity_id=user.id)
                return redirect(_safe_next())
            throttle.record_failure(address, username)
            # No household and no actor: a failed attempt names a string
            # somebody typed, not a member of anything. Recording the *claimed*
            # username as metadata is the point -- it is what makes a spray
            # across many usernames from one address visible afterwards.
            audit.record(EVENT_LOGIN_FAILED,
                         metadata={'username': username,
                                   'user_exists': bool(user)})
            error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@bp.route('/logout', methods=['POST'])
def logout():
    # Before session.clear(), which is what the actor is read from.
    user = db.session.get(AppUser, session.get('user_id') or 0)
    if user is not None:
        audit.record(EVENT_LOGOUT, household_id=user.household_id,
                     actor_user_id=user.id,
                     entity_type='user', entity_id=user.id)
    session.clear()
    return redirect(url_for('auth.login'))


@bp.route('/join/<token>', methods=['GET', 'POST'])
@public
def join(token):
    """Redeem an invitation: create a login inside somebody's household.

    The one route in the application that resolves a row before any household is
    bound. It has to: whoever follows the link is anonymous, and the token is
    what says which household they are joining. So the lookup runs inside
    `unscoped()` -- deliberately one greppable token, per ADR-0008 -- and the
    household is bound immediately afterwards, so the write that consumes the
    invitation is ordinary scoped work with the usual guard behind it.

    A signed-in visitor is turned away rather than switched. ADR-0009 records
    why: one person belongs to one household, so "accepting" while already a
    member would mean silently leaving, and the household left behind might be
    one nobody else can administer.
    """
    with unscoped():
        invite = find_redeemable_invite(token)
    if invite is None:
        # Unknown, expired, revoked and already-used all say the same thing.
        # Distinguishing them confirms which tokens were once real, which is
        # what somebody guessing wants to learn.
        return render_template('join.html', invalid=True), 404

    if session.get('user_id'):
        return render_template('join.html', invite=invite,
                               already_signed_in=True), 403

    with tenant_scope(invite.household_id):
        home = db.session.get(Household, invite.household_id)
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            if not username:
                error = 'Please choose a username.'
            elif AppUser.query.filter_by(username=username).first():
                # Usernames are unique installation-wide, not per household --
                # one login namespace, asserted by tools/verify_tenancy.py.
                # Saying so here is not an enumeration leak worth worrying
                # about: the visitor already holds a valid invitation.
                error = 'That username is taken. Please pick another.'
            elif len(password) < 8:
                error = 'Password must be at least 8 characters.'
            elif password != request.form.get('confirm', ''):
                error = 'Passwords do not match.'
            else:
                try:
                    user = accept_invite(invite, username,
                                         hash_password(password))
                except MembershipError as exc:
                    error = str(exc)
                else:
                    _sign_in(user)
                    audit.record(EVENT_INVITE_ACCEPTED,
                                 household_id=invite.household_id,
                                 actor_user_id=user.id,
                                 entity_type='user', entity_id=user.id,
                                 metadata={'username': username,
                                           'invited_by': invite.created_by_id,
                                           'role': user.role})
                    return redirect('/')
        return render_template('join.html', invite=invite,
                               household=home, error=error)
