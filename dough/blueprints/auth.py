"""Everything that runs for somebody the application has not yet identified.

Signing in and out, first-run setup, redeeming an invitation, and — since Phase
10.5 — creating an account and recovering one. They are grouped because they
share the property that makes them different from every other blueprint: there
is no user and no household bound while they run, so none of them may touch
tenant data without saying which household it belongs to.

The session *guard* is not here. `_require_login` and the session-lifetime
checks are registered on the application in `app.create_app`, because they apply
to every request rather than to these routes, and a before_request hook that
lived on a blueprint would only ever see that blueprint's traffic -- which is
precisely backwards for a default-deny rule.

## The rule these routes exist to keep

**No response may reveal whether an account exists.**  [Phase 10.5]

That is one rule with three parts, and the third is the one that gets missed:

1. The *wording* must not differ. `/forgot-password` says the same sentence for
   an address with an account and one without.
2. The *status and shape* must not differ. Both are a 200 rendering the same
   template — not one 200 and one 404, and not a redirect in one case only.
3. The *time taken* must not differ. Sending mail takes tens of milliseconds and
   not sending it takes none, which is a signal anybody can measure over a few
   hundred requests. `_uniform_delay` is what closes it, and it is the part that
   would never be noticed as missing.

`/register` is deliberately exempt for usernames and bound by the rule for
addresses. The reasoning is at `identity.register_account`: somebody choosing a
username needs to be told it is taken, and usernames have been enumerable
through `/join` since Phase 6 anyway — whereas an address is an identifier
somebody else chose, and confirming one is registered tells a stranger that this
person banks with Dough.
"""

import time

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)

from dough.auth import (LoginThrottle, SESSION_VERSION_KEY, SIGN_OUT_DELIBERATE,
                        client_address, hash_password, notify_signed_out,
                        public, upgrade_password_hash, verify_password)
from dough.services import audit, identity
from dough.services.email import EmailError, current_email
from dough.services.membership import MembershipError, accept_invite, find_redeemable_invite
from dough.services.ratelimit import current_limiter
from dough.tenancy import tenant_scope, unscoped
from models import (AppUser, EVENT_EMAIL_VERIFICATION_SENT,
                    EVENT_EMAIL_VERIFIED, EVENT_INVITE_ACCEPTED,
                    EVENT_LOGIN_FAILED, EVENT_LOGIN_SUCCEEDED,
                    EVENT_LOGIN_THROTTLED, EVENT_LOGOUT,
                    EVENT_PASSWORD_REHASHED, EVENT_PASSWORD_RESET_COMPLETED,
                    EVENT_PASSWORD_RESET_REQUESTED, EVENT_RATE_LIMITED,
                    EVENT_REGISTERED, EVENT_SETUP_COMPLETED, Household,
                    PURPOSE_PASSWORD_RESET, PURPOSE_VERIFY_EMAIL, ROLE_OWNER,
                    db)

bp = Blueprint('auth', __name__)

#: How long every `/forgot-password` POST takes, at minimum. See the module
#: docstring, point 3.
#:
#: 350ms is chosen against what it is hiding rather than as a round number: the
#: work it has to mask is a `scrypt` verification (~100ms at the parameters in
#: `dough.auth.PASSWORD_METHOD`) plus a console or SMTP send. It is a floor, not
#: a `sleep` — a request that already took longer waits no further, so the slow
#: path is not slowed down twice.
UNIFORM_RESPONSE_SECONDS = 0.35

#: How long the session-carried "you may set this account's password" grant
#: lasts, once a reset token has been spent to obtain it. See
#: `reset_password`'s POST branch for what it is bounding — the token is already
#: gone by then, so this is the life of the *form*, not of the link.
RESET_GRANT_SECONDS = 900


@bp.record_once
def _install_throttle(state):
    """One throttle per application, not one per process.

    It was a closure over `create_app`, which gave the same guarantee for free.
    A module-level instance would not: the suite builds many applications in one
    process, and they would have shared a counter -- so a test that exhausted
    the limit would fail an unrelated test that ran afterwards.

    `setdefault` rather than assignment since Phase 10: `/api/v1/auth/login`
    installs one the same way, and the two must share a *single* instance. Two
    throttles over one credential each see half the attempts, so a distributed
    attempt alternating between the two surfaces would fill neither -- the
    throttle would still be there, and would have stopped working.
    """
    state.app.extensions.setdefault('dough_login_throttle', LoginThrottle())


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
    # Which credential generation this session belongs to.  [Phase 10.5] Written
    # here, at the one place a session is ever created, so there is no path that
    # produces a session without it -- and `session_is_current` refuses one that
    # lacks it, so a new path that forgot would fail closed and loudly rather
    # than quietly opt itself out.
    session[SESSION_VERSION_KEY] = user.session_version
    session['signed_in_at'] = int(time.time())
    session['seen_at'] = int(time.time())
    session.permanent = True


def _safe_next():
    nxt = request.args.get('next', '/')
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = '/'
    return nxt


def _uniform_delay(started_at):
    """Hold the response until `UNIFORM_RESPONSE_SECONDS` have passed.

    The third part of the no-enumeration rule, and the one that is invisible
    when it is missing: a `/forgot-password` that sends mail for a real address
    and returns immediately for an unknown one answers the question its wording
    refuses to, and it does so measurably over a few hundred requests.

    A floor rather than a fixed pause. `time.sleep(0.35)` *after* the work would
    make the real path take 0.35s longer than the fake one, which is the same
    leak with the sign flipped.

    This is not constant-time in the cryptographic sense and does not need to be
    — it is hiding a difference measured in tens of milliseconds behind a wait
    measured in hundreds, against a remote attacker whose measurements carry
    network jitter of the same order.
    """
    remaining = UNIFORM_RESPONSE_SECONDS - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)


def _external_url(endpoint, **values):
    """A link fit to put in an email.

    `PUBLIC_BASE_URL` when it is set, and `url_for(_external=True)` otherwise.
    The distinction matters because `_external=True` builds the host from the
    incoming request, which is the `Host` header, which is client-controlled — so
    a request carrying `Host: attacker.example` produces a reset link pointing at
    the attacker, mailed by us to the victim.

    Nothing here can fix that on its own; `PUBLIC_BASE_URL` is the fix, and
    `ProductionConfig.warnings()` says so at boot when it is unset.
    """
    base = current_app.config.get('PUBLIC_BASE_URL', '')
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def _limited(policy, identity_key, **audit_metadata):
    """Spend one unit of a rate-limit policy. True means refuse the request.

    Records the refusal and logs it. The caller decides what to *say*, which
    matters on `/forgot-password`, where saying "rate limited" to one address and
    "check your inbox" to another would reintroduce the enumeration signal the
    route is built to avoid.
    """
    decision = current_limiter().check(policy, identity_key)
    if decision.allowed:
        return False
    current_app.logger.warning('Rate limit %s reached', policy)
    audit.record(EVENT_RATE_LIMITED,
                 metadata={'policy': policy, 'retry_after': decision.retry_after,
                           **audit_metadata})
    return True


# ---------------------------------------------------------------------------
# Creating an account  [Phase 10.5]
# ---------------------------------------------------------------------------

@bp.route('/register', methods=['GET', 'POST'])
@public
def register():
    """Self-serve signup: an account, a household, and ownership of it.

    ## Why the route exists even when registration is closed

    `ALLOW_REGISTRATION` is off by default — this application fronts real bank
    data and is routinely exposed on a LAN. A closed instance still *serves*
    this URL and renders a page saying so, rather than 404ing.

    404 was the obvious alternative and it is worse in both directions. For the
    person, "not found" is indistinguishable from a typo, so they retry the URL
    instead of asking the household owner for an invitation — which is the thing
    they actually need to do, and which the page can tell them. For everyone
    else, a URL that exists on some deployments and not others makes the landing
    page's own "Create account" button a dead link that nothing in the
    application can detect.

    ## Ordering

    The rate limit is spent before the form is read and before any password is
    hashed. Registration is the one unauthenticated route that *grows the
    database*, and `scrypt` at these parameters is ~100ms of CPU an anonymous
    caller can ask for at will.
    """
    if not current_app.config.get('ALLOW_REGISTRATION', False):
        return render_template('register.html', closed=True), 403
    if session.get('user_id'):
        # Already signed in. Not an error, and not a silent redirect either --
        # `/join` established the pattern of telling somebody why rather than
        # bouncing them somewhere and leaving them to work it out.
        return redirect(url_for('core.dashboard'))

    error = None
    form = {}
    if request.method == 'POST':
        form = {'username': request.form.get('username', '').strip(),
                'email': request.form.get('email', '').strip()}
        if _limited('register', client_address(), username=form['username']):
            error = ('Too many accounts have been created from here recently. '
                     'Try again in an hour.')
        else:
            try:
                user = identity.register_account(
                    username=form['username'],
                    email=form['email'],
                    password=request.form.get('password', ''),
                    confirm=request.form.get('confirm', ''))
            except identity.IdentityError as exc:
                error = str(exc)
            else:
                _sign_in(user)
                audit.record(EVENT_REGISTERED, household_id=user.household_id,
                             actor_user_id=user.id, entity_type='user',
                             entity_id=user.id,
                             metadata={'username': user.username,
                                       'household_id': user.household_id})
                # Best-effort, and deliberately after the account exists. A mail
                # server that is down must not cost somebody their registration
                # -- they are already signed in, and the address can be verified
                # later from the settings page.
                send_verification(user, quiet=True)
                return redirect(url_for('core.dashboard'))
    return render_template('register.html', error=error, form=form)


def send_verification(user, *, quiet=False):
    """Issue a verification token and mail the link. True if it went.

    Public rather than `_`-prefixed because `dough/blueprints/settings.py`
    calls it, and a cross-blueprint import reaching into a private name is worse
    than one that does not. It lives here rather than in
    `dough/services/identity.py` for the reason that service's docstring gives:
    building the link needs `url_for`, which is a route's job and needs a request
    context the scheduler does not have.

    `quiet` swallows a delivery failure, and only the registration path passes
    it: there, the mail is a follow-up to an operation that has already
    succeeded. The settings page passes `quiet=False`, because there the mail
    *is* the operation and "sent" would be a lie.
    """
    if not user.email:
        return False
    try:
        _row, token = identity.issue_token(user, PURPOSE_VERIFY_EMAIL)
        current_email().send_verification_email(
            user.email, _external_url('auth.verify_email', token=token),
            username=user.username)
    except (EmailError, identity.IdentityError):
        # The exception's text is deliberately not logged: an SMTP error can
        # quote the message it was carrying, and that message holds a live
        # token. `dough/services/email.py` raises with the host and the type
        # only, and this keeps even that out of the audit trail.
        current_app.logger.warning('Verification mail could not be sent')
        if not quiet:
            raise
        return False
    audit.record(EVENT_EMAIL_VERIFICATION_SENT, household_id=user.household_id,
                 actor_user_id=user.id, entity_type='user', entity_id=user.id)
    return True


@bp.route('/verify-email/<token>')
@public
def verify_email(token):
    """Spend a verification token and mark the address proved.

    `@public`, and it has to be: the link is followed from a mail client, which
    carries no session, and requiring one would mean the only people who can
    verify an address are the ones already signed in on that device.

    Nothing is *granted* by this beyond the timestamp — `REQUIRE_EMAIL_VERIFICATION`
    is off and nothing reads the column yet — so a redeemed token is a fact
    recorded rather than a door opened. That is why it is safe for the route to
    be a GET, which a link has to be.
    """
    row, user_or_reason = identity.redeem(token, PURPOSE_VERIFY_EMAIL)
    if row is None:
        current_app.logger.info('Email verification refused (%s)',
                                user_or_reason)
        return render_template('verified.html', invalid=True), 404

    user = user_or_reason
    identity.mark_email_verified(user)
    audit.record(EVENT_EMAIL_VERIFIED, household_id=user.household_id,
                 actor_user_id=user.id, entity_type='user', entity_id=user.id)
    return render_template('verified.html', user=user)


# ---------------------------------------------------------------------------
# Recovering an account  [Phase 10.5]
# ---------------------------------------------------------------------------

@bp.route('/forgot-password', methods=['GET', 'POST'])
@public
def forgot_password():
    """Ask for a reset link. Says the same thing whatever the answer is.

    Every branch below reaches the same `sent=True` render through the same
    delay. That is the whole route: the interesting code is the code that
    *cannot* be seen from outside.

    The rate limit is two policies rather than one, and they stop different
    things. `password_reset` is keyed on the source address and stops one host
    walking a list of addresses. `password_reset_account` is keyed on the account
    and stops the distributed version, where every request comes from somewhere
    new and the address bucket never fills — which here is not credential
    stuffing but mailbox flooding: the victim's inbox is the resource being
    attacked, and it does not care how many hosts sent the mail.

    Neither refusal changes the response. A caller who has hit the limit is told
    the same sentence as everybody else, because "you are being rate limited"
    for one address and "check your inbox" for another is the enumeration oracle
    again, wearing a different hat.
    """
    started_at = time.monotonic()
    if request.method != 'POST':
        return render_template('forgot_password.html')

    address = request.form.get('email', '')
    if _limited('password_reset', client_address()):
        _uniform_delay(started_at)
        return render_template('forgot_password.html', sent=True)

    user = identity.find_by_email(address)
    # Recorded for both outcomes, with the fact in the metadata rather than in
    # the response -- exactly as `auth.login.failed` records `user_exists`. This
    # is what makes somebody walking a list of addresses visible to an operator
    # while telling the walker nothing.
    audit.record(EVENT_PASSWORD_RESET_REQUESTED,
                 household_id=user.household_id if user else None,
                 actor_user_id=user.id if user else None,
                 metadata={'email': identity.normalize_email(address),
                           'user_exists': bool(user)})

    if user is not None and not _limited('password_reset_account', user.id):
        try:
            _row, token = identity.issue_token(user, PURPOSE_PASSWORD_RESET)
            current_email().send_password_reset_email(
                user.email, _external_url('auth.reset_password', token=token),
                username=user.username)
        except (EmailError, identity.IdentityError):
            # Swallowed on purpose, and this is the one place in the application
            # where swallowing a delivery failure is the *security* behaviour
            # rather than a convenience. Reporting it would mean an error page
            # for an address that has an account and a success page for one that
            # does not -- the enumeration signal, reintroduced by an error
            # handler somebody added for good reasons.
            current_app.logger.warning('Password reset mail could not be sent')

    _uniform_delay(started_at)
    return render_template('forgot_password.html', sent=True)


@bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@public
def reset_password(token):
    """Set a new password with a token instead of the old one.

    ## The token is spent by the GET

    `identity.redeem` stamps `used_at` the moment it resolves the row, so
    loading this form consumes the link. That is deliberate and it costs
    something real: a person who fails the password rules has to request a new
    link rather than retry.

    The alternative — validate on GET, spend on POST — leaves a redeemable token
    alive across a window the application does not control, which is exactly the
    window somebody who has read the victim's mail is waiting in. Between "a
    user occasionally requests a second link" and "a stolen link stays live while
    a form sits open", the first is an inconvenience and the second is the
    vulnerability the flow exists to close.

    So the redeemed user is carried across the POST in the session. Not the
    token, which is spent, and not a user id in a hidden form field, which would
    let anybody POST a chosen id and set that account's password.

    ## What succeeds here invalidates everything

    Setting the password raises `session_version` through the listener in
    `dough/auth.py`, so every other browser session and every API token this
    account issued stops working on its next request. That is the point of a
    reset: the plausible reason for needing one is that somebody else has the
    old credential.
    """
    error = None

    if request.method == 'POST':
        # Written by the GET below, after the token was spent. A POST that
        # arrives without it is a form replayed after the session was cleared,
        # or a direct post by somebody who never held a token at all.
        user_id, granted_at = session.get('_reset_user_id') or (None, 0)
        user = db.session.get(AppUser, user_id) if user_id else None
        # The grant expires on its own, and it has to.  [Phase 10.5] Somebody
        # who loads the form and never submits would otherwise carry "may set
        # this account's password, no current password required" in their cookie
        # for the whole session lifetime -- days. That turns a stolen session
        # cookie into a full takeover, including lockout, on an account whose
        # password change normally demands the current one.
        #
        # Fifteen minutes is the form's useful life, not the token's: the token
        # is already spent by the time this exists.
        if user is None or time.time() - granted_at > RESET_GRANT_SECONDS:
            session.pop('_reset_user_id', None)
            return render_template('reset_password.html', invalid=True), 404
        try:
            identity.set_password(user, request.form.get('password', ''),
                                  request.form.get('confirm', ''))
        except identity.IdentityError as exc:
            error = str(exc)
            return render_template('reset_password.html', token=token,
                                   error=error)

        session.pop('_reset_user_id', None)
        audit.record(EVENT_PASSWORD_RESET_COMPLETED,
                     household_id=user.household_id, actor_user_id=user.id,
                     entity_type='user', entity_id=user.id,
                     metadata={'username': user.username})
        # Not signed in afterwards, deliberately. The session_version bump has
        # already invalidated every session including any this browser held, and
        # signing them straight in would mean the one flow whose premise is
        # "somebody may have your credentials" ends by handing out a session
        # without anyone having typed the new password once.
        flash('Your password has been changed, and everything signed in with '
              'the old one has been signed out. Sign in with the new one.',
              'success')
        return redirect(url_for('auth.login'))

    row, user_or_reason = identity.redeem(token, PURPOSE_PASSWORD_RESET)
    if row is None:
        # Unknown, expired, already-used, wrong-purpose and address-changed all
        # render the same page. Distinguishing them confirms which tokens were
        # once real, which is what somebody guessing wants to learn.
        current_app.logger.info('Password reset refused (%s)', user_or_reason)
        return render_template('reset_password.html', invalid=True), 404

    session['_reset_user_id'] = (user_or_reason.id, int(time.time()))
    return render_template('reset_password.html', token=token)


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
                # The only moment the plaintext exists to re-derive from. A hash
                # stored under an older KDF cannot be upgraded by a migration.
                # Shared with `/api/v1/auth/login` since Phase 10 -- see
                # `dough.auth.upgrade_password_hash` for why it is one function.
                if upgrade_password_hash(user, password):
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
    # After the clear, not before: flashes live in the session, so a message
    # queued first would be discarded by the line above and the login page
    # would render silently. See `dough.auth.notify_signed_out`.
    notify_signed_out(SIGN_OUT_DELIBERATE)
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
