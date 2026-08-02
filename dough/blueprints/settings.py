"""The account: its password, its address, its sessions, and its API tokens.
[Phase 10.5]

Separate from `household.py` on purpose, and the split is not "personal vs
shared" — it is **who the operation can hurt**. Everything here acts on the
signed-in account and nobody else's: changing a password signs *you* out of your
own devices, revoking a token stops *your* client. Everything in `household.py`
acts on other people, which is why every route there carries `@owner_required`
and nothing here does.

That is also why the two are not merged behind one `/settings` page with tabs.
A page where "change my password" and "remove Sam from the household" sit
side by side is a page where the second is one misread click away, and the
permission check that separates them is invisible in the markup.

## Re-authentication

`/settings/password` requires the current password even though the caller
already holds a valid session. The session is not proof that *this person* is
here — an unlocked screen is a session — and a password change is the operation
that locks the real owner out. It is the one place in the application where
holding a credential is not enough to use it.

`/settings/sessions/revoke` deliberately does **not** require it. It only ever
*removes* access, so the worst an attacker at an unlocked screen achieves is
signing everybody out, which is the same thing the real owner would do on
discovering them. Demanding a password there would mean the person who most
needs the button — the one who thinks their account is compromised — is the one
who has to stop and remember something first.
"""

import json
from datetime import datetime

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)

from dough.auth import SESSION_VERSION_KEY, current_user, verify_password
from dough.blueprints.auth import send_verification
from dough.services import account_lifecycle, api_tokens, audit, identity
from dough.services.email import EmailError
from dough.services.membership import household_members
from dough.services.ratelimit import current_limiter
from dough.tenancy import require_household
from models import (EVENT_ACCOUNT_EXPORTED,
                    EVENT_API_TOKEN_ISSUED, EVENT_API_TOKEN_REVOKED,
                    EVENT_PASSWORD_CHANGED, EVENT_SESSIONS_REVOKED, Household,
                    db)

bp = Blueprint('settings', __name__, url_prefix='/settings')


@bp.route('')
def index():
    """Account information, security controls, and the token list in one page.

    The tokens are read here rather than on a page of their own because the
    question "what can currently reach my data?" has two answers — browser
    sessions and API tokens — and splitting them across two pages is what makes
    somebody check one and forget the other.
    """
    me = current_user()
    home = db.session.get(Household, require_household())
    return render_template(
        'settings.html',
        me=me,
        household=home,
        tokens=api_tokens.household_tokens(home.id),
        # Shown once and never again -- the same session-carried, pop-on-read
        # handover `household.invite_create` uses, and for the same reasons: a
        # query string ends up in browser history, in a Referer header, and in
        # the access log of whatever sits in front of this application, all
        # three of which outlive the page that displayed it.
        new_token=session.pop('_new_api_token', None),
        members=household_members(home.id) if me and me.is_owner else [],
        scopes=api_tokens.VALID_SCOPES,
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@bp.route('/password', methods=['POST'])
def change_password():
    """Replace the password, which invalidates everything except this session.

    ## Why this session survives

    `identity.set_password` raises `session_version` through the listener in
    `dough/auth.py`, and the cookie in this very browser carries the old value —
    so without the re-stamp below, the response to a successful password change
    would be a redirect to the login page.

    That reads as a bug and it teaches the wrong lesson. The person just proved
    they know both the old password and the new one; they are the *least*
    suspicious party in the system at that moment. Every other credential is
    gone, which is the security outcome, and this one is re-stamped rather than
    exempted — it is issued afresh under the new generation, not carried over
    from the old one.

    The re-stamp is one line and it must stay next to the change. Splitting them
    is how a later edit to either half produces a password change that signs the
    person changing it out.
    """
    user = current_user()
    try:
        identity.set_password(
            user,
            request.form.get('password', ''),
            request.form.get('confirm', ''),
            current_password=request.form.get('current_password', ''))
    except identity.IdentityError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings.index'))

    session[SESSION_VERSION_KEY] = user.session_version
    audit.record(EVENT_PASSWORD_CHANGED, household_id=user.household_id,
                 actor_user_id=user.id, entity_type='user', entity_id=user.id,
                 metadata={'username': user.username})
    flash('Password changed. Everything else signed in with the old one — other '
          'browsers, and every API token — has been signed out.', 'success')
    return redirect(url_for('settings.index'))


@bp.route('/email', methods=['POST'])
def change_email():
    """Point the account at a different address and send a fresh verification.

    Changing the address retires any password-reset link already in flight,
    without this route having to do anything: `identity.redeem` compares the
    token's `sent_to` against the account's current address and refuses a
    mismatch. That matters here specifically — an attacker who has requested a
    reset and is waiting on the mail loses the link the moment the real owner
    changes their address.
    """
    user = current_user()
    try:
        identity.set_email(user, request.form.get('email', ''))
    except identity.IdentityError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings.index'))

    try:
        send_verification(user)
        flash('Address updated. Check it for a link confirming it works.',
              'success')
    except (EmailError, identity.IdentityError):
        # The address *was* saved -- the commit happened in `set_email` above --
        # so the message says both halves. Reporting only the failure would
        # leave somebody believing their address is unchanged when it is not,
        # which is the state in which they stop watching the new inbox.
        flash('Address updated, but the confirmation email could not be sent. '
              'You can resend it from this page.', 'error')
    return redirect(url_for('settings.index'))


@bp.route('/verify-email/resend', methods=['POST'])
def resend_verification():
    """Send the confirmation link again.

    Both non-sending branches say something true rather than silently doing
    nothing: an account with no address, and one already confirmed, are the two
    states in which this button is visible but pointless, and a page that
    reloads unchanged reads as a broken button.
    """
    user = current_user()
    # Rate-limited per account rather than per address: this route sends mail on
    # demand, and the resource being protected is the recipient's inbox, which
    # does not care how many browsers asked.
    if current_limiter().check('email_verification', user.id).allowed is False:
        flash('That has been sent a few times already. Try again in an hour.',
              'error')
    elif not user.email:
        flash('Add an email address first.', 'error')
    elif user.email_verified_at:
        flash('That address is already confirmed.', 'success')
    else:
        try:
            send_verification(user)
            flash('Sent. Check your inbox for the confirmation link.', 'success')
        except (EmailError, identity.IdentityError):
            flash('That email could not be sent. Try again shortly.', 'error')
    return redirect(url_for('settings.index'))


@bp.route('/sessions/revoke', methods=['POST'])
def revoke_sessions():
    """Sign out everywhere, including here.

    The opposite decision from `change_password` above, and the difference is
    what the person asked for. "Sign out everywhere" that quietly kept the
    session it was clicked from would not have done what it says — and the
    situation in which somebody reaches for this button is precisely the one
    where "everywhere" needs to mean everywhere, because they do not know which
    of the live sessions is the one they should be worried about.

    So the session is cleared and they sign in again. One inconvenience, no
    ambiguity.
    """
    user = current_user()
    identity.revoke_all_credentials(user)
    audit.record(EVENT_SESSIONS_REVOKED, household_id=user.household_id,
                 actor_user_id=user.id, entity_type='user', entity_id=user.id,
                 metadata={'username': user.username})
    session.clear()
    flash('Signed out everywhere. Every API token for this account has stopped '
          'working too.', 'success')
    return redirect(url_for('auth.login'))


# ---------------------------------------------------------------------------
# API tokens  [Phase 10.5 — the web half of Phase 10's /api/v1/auth/tokens]
# ---------------------------------------------------------------------------

@bp.route('/tokens', methods=['POST'])
def token_create():
    """Issue a token and show it exactly once.

    The same service `/api/v1/auth/tokens` calls, which is what makes the two
    surfaces agree by construction rather than by anybody maintaining them in
    step. This route reads a form and shapes a redirect; it decides nothing.
    """
    user = current_user()
    ttl = (request.form.get('ttl_days') or '').strip()
    try:
        _token, plaintext = api_tokens.issue(
            require_household(), user,
            name=request.form.get('name', ''),
            scopes=request.form.getlist('scopes'),
            ttl_days=int(ttl) if ttl.isdigit() and int(ttl) > 0 else None)
    except api_tokens.ApiTokenError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings.index'))

    session['_new_api_token'] = plaintext
    # The plaintext is deliberately absent from the metadata. It is a bearer
    # credential and the audit log is the one table nothing ever deletes from --
    # identical reasoning to `household.invite_create`.
    audit.record(EVENT_API_TOKEN_ISSUED, entity_type='api_token',
                 entity_id=_token.id,
                 metadata={'name': _token.name, 'scopes': _token.scope_list()})
    flash('Token created. Copy it now — it is not shown again.', 'success')
    return redirect(url_for('settings.index'))


@bp.route('/tokens/<int:token_id>/revoke', methods=['POST'])
def token_revoke(token_id):
    try:
        api_tokens.revoke(require_household(), token_id)
    except api_tokens.ApiTokenError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings.index'))

    audit.record(EVENT_API_TOKEN_REVOKED, entity_type='api_token',
                 entity_id=token_id)
    flash('Token revoked. Anything using it stops working immediately.',
          'success')
    return redirect(url_for('settings.index'))


# ---------------------------------------------------------------------------
# Taking your data out, and closing the account  [Phase 10.7]
# ---------------------------------------------------------------------------

@bp.route('/export')
def export_data():
    """Download everything this installation holds, as one JSON file.

    A GET, and it is worth saying why that is acceptable here when the rest of
    this blueprint is careful about it. GET means a browser can be pointed at it
    and will save the file, which is the whole interaction — a POST would need a
    form and would then have to stream a download from a redirect. It reads and
    changes nothing, so the usual objection to a state-changing GET does not
    apply.

    What it *is* is a complete copy of a household's finances leaving the system
    in one request, so it is audited. That is the one control this route has,
    and it is deliberate: an export is the highest-value thing a stolen session
    can do short of changing the password, and it should not be the only such
    action that leaves no trace.
    """
    user = current_user()
    payload = account_lifecycle.export_account(user)

    audit.record(EVENT_ACCOUNT_EXPORTED, household_id=user.household_id,
                 actor_user_id=user.id, entity_type='household',
                 entity_id=user.household_id,
                 metadata={'counts': payload['counts']})

    stamp = datetime.utcnow().strftime('%Y%m%d')
    response = current_app.response_class(
        json.dumps(payload, indent=2, default=str),
        mimetype='application/json')
    response.headers['Content-Disposition'] = (
        f'attachment; filename=dough-export-{stamp}.json')
    # Never cached. It is the most sensitive response this application produces,
    # and a shared browser must not be able to restore it from history.
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/delete', methods=['GET'])
def delete_account_confirm():
    """The confirmation screen, built from what the deletion would actually do.

    A separate page rather than a JavaScript `confirm()`: this is irreversible,
    and the person deserves to see whether they are closing their own account or
    dissolving a household with somebody else's records in it. Those are very
    different acts and the difference is not visible from the button.

    The counts come from `deletion_preview`, so the warning is generated by the
    same code that performs the removal. A warning maintained separately from
    the operation drifts, and it drifts toward understating.
    """
    user = current_user()
    return render_template('delete_account.html', user=user,
                           preview=account_lifecycle.deletion_preview(user))


@bp.route('/delete', methods=['POST'])
def delete_account():
    """Close the account for good.

    Three things stand between a stray click and an irreversible deletion, and
    each catches a different mistake:

    1. **CSRF**, as everywhere — this one is application-wide and not special.
    2. **The current password.** The same reasoning as `/settings/password`, and
       more so: a session is not proof that this person is here, and an unlocked
       screen must not be enough to destroy somebody's financial history.
    3. **Typing the username.** Deliberate friction, and the one control aimed
       at the *account holder* rather than at an attacker. Somebody who has
       already decided to delete will type it in five seconds; somebody who
       arrived here by misreading a link will not have their data gone because
       a confirm dialog was one keystroke from an accepted default.
    """
    user = current_user()

    if not verify_password(user.password_hash, request.form.get('password', '')):
        flash('That password is not right. Nothing has been deleted.', 'error')
        return redirect(url_for('settings.delete_account_confirm'))

    if request.form.get('confirm_username', '').strip() != user.username:
        flash('Type your username exactly to confirm. Nothing has been deleted.',
              'error')
        return redirect(url_for('settings.delete_account_confirm'))

    try:
        summary = account_lifecycle.delete_account(user)
    except account_lifecycle.AccountLifecycleError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('settings.delete_account_confirm'))

    # Cleared rather than left to expire. The row backing this session no longer
    # exists, so every subsequent request would fail resolving the user -- and
    # `session.clear()` is what turns that into a clean signed-out state instead
    # of an error page.
    session.clear()
    if summary['last_member']:
        flash('Your account and all of its data have been deleted.', 'success')
    else:
        flash('Your account has been deleted. The household you shared '
              'remains, along with its financial records.', 'success')
    return redirect(url_for('core.dashboard'))
