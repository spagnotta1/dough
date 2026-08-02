"""Taking your data out, and taking your account away.
[Phase 10.7 — the erasure and portability half of the privacy policy]

Allowed:   models, sqlalchemy, dough.services.*, dough.tenancy, stdlib
Must not:  app, flask.request, render_template/url_for/redirect/jsonify,
           blueprints

No request objects and no `url_for`. Both operations are things a person may
eventually want to run from a shell, a scheduled job, or a support tool against
an account that cannot sign in — and an implementation that needs a request
context cannot be used from any of those.

## Why deletion is not `membership.remove_member`

They look like the same operation and are not, in three ways that matter:

- `remove_member` **refuses self-removal**, deliberately: an owner removing
  themselves would 500 on the next request, and "log out" is what they wanted.
  Deleting your own account is precisely self-removal, so it needs its own path
  rather than an exception carved into that rule.
- `remove_member` **deletes nothing but the login**, because the household's
  financial data belongs to the household and the person leaving does not take
  it. That is right when somebody leaves a shared household and wrong when the
  last member closes the account, at which point nothing is left to own it.
- Deletion has to reach **rows `remove_member` never touches**: API tokens,
  outstanding verification and reset tokens, and — in the last-member case —
  every tenant-scoped table.

## The two cases, and why the distinction is not a convenience

**You are the last member.** The household is deleted, and with it every
tenant-scoped row: transactions, budgets, holdings, connections, chat history,
sync records, snapshots. There is nobody left who could have a claim on it.

**Somebody else is still in the household.** Only your personal rows go. The
financial data stays, because it is the household's — the other members entered
some of it, they are still using it, and deleting a shared ledger because one of
two people left would destroy data belonging to somebody who did not ask for
anything. `templates/privacy.html` states this in those terms, and the two have
to keep saying the same thing.

**A sole owner with other members is refused.** Not a technical limitation: the
application's standing invariant is that a household always has an owner
(`membership._commit_keeping_an_owner`), and silently promoting somebody would
hand a person administrative control of a household because somebody else
quit. The refusal names the fix.

## What deletion does not remove, stated plainly

- **Audit events.** `dough/services/audit.py` enforces append-only with a
  `before_flush` hook, so these rows cannot be deleted or edited by any code
  path in this application — which is the property that makes the audit trail
  worth having. They record that events happened and to which household; they
  hold no financial data, and the redactor keeps credentials out of them. This
  is disclosed in the privacy policy rather than quietly true.
- **Backups.** Snapshots taken before the deletion still contain the data until
  they age out of the retention window (`BACKUP_KEEP`). Also disclosed.

Neither is a bug to fix later without deciding something first: purging audit
rows would remove the record that a deletion occurred, and rewriting backups
would defeat the point of having them.
"""

from __future__ import annotations

import datetime as dt
import logging

logger = logging.getLogger('dough.account_lifecycle')

__all__ = [
    'AccountLifecycleError',
    'delete_account',
    'deletion_preview',
    'export_account',
]

#: Every tenant-scoped model, and the key its rows appear under in an export.
#:
#: Written out rather than derived from `TenantScopedMixin.__subclasses__()`,
#: which was the first implementation and was wrong in the direction that does
#: not fail: a subclass whose module happens not to be imported yet is simply
#: absent from that list, so the export silently omits a table and the deletion
#: silently leaves one behind. A literal list is checked by
#: `tests/test_account_lifecycle.py::test_every_tenant_scoped_model_is_accounted_for`,
#: which *does* walk the subclasses — so a new model added without touching this
#: file fails a test instead of quietly escaping both operations.
_SCOPED_TABLES = (
    ('transactions', 'Transaction'),
    ('budgets', 'Budget'),
    ('log_entries', 'LogEntry'),
    ('account_balances', 'AccountBalance'),
    ('conversations', 'Conversation'),
    ('chat_messages', 'ChatMessage'),
    ('recurring_dismissals', 'RecurringDismissal'),
    ('holdings', 'Holding'),
    ('connections', 'InstitutionConnection'),
    ('financial_accounts', 'FinancialAccount'),
    ('sync_runs', 'SyncRun'),
    ('sync_errors', 'SyncErrorLog'),
    ('portfolio_snapshots', 'PortfolioSnapshotRow'),
    ('household_invites', 'HouseholdInvite'),
    # [Phase 11A.1] Rules moved out of a shared JSON file into a tenant-scoped
    # table. They are the household's own data — a rule set names the merchants
    # they pay — so an export that omitted them would be incomplete and a
    # deletion that skipped them would leave that behind.
    ('category_rules', 'CategoryRule'),
)

#: Column names never included in an export, matched case-insensitively as a
#: substring. An export is a file the user downloads, mails to themselves, and
#: drops in a cloud folder; it must not be a way to extract a credential from
#: the database in plaintext-adjacent form.
#:
#: `auth_blob` is the encrypted Plaid access token. It would be useless without
#: the key, and it is still not going in a file somebody emails around.
_NEVER_EXPORTED = ('password_hash', 'auth_blob', 'token_hash', 'secret',
                   'access_token', 'api_key')


class AccountLifecycleError(Exception):
    """A deletion or export that must not proceed, with a reason for a person."""


def _models():
    """Imported inside the functions, as every service here does."""
    import models

    return models


def _is_exportable(column_name):
    lowered = column_name.lower()
    return not any(banned in lowered for banned in _NEVER_EXPORTED)


def _serialize(row):
    """One ORM row as a plain dict, minus anything credential-shaped.

    Reads the mapped columns rather than `__dict__`, so SQLAlchemy internal
    state does not leak into the file and an unloaded attribute is fetched
    rather than silently omitted.
    """
    from sqlalchemy import inspect as sa_inspect

    out = {}
    for column in sa_inspect(row.__class__).columns:
        if not _is_exportable(column.key):
            continue
        value = getattr(row, column.key, None)
        if isinstance(value, (dt.datetime, dt.date)):
            value = value.isoformat()
        elif isinstance(value, dt.timedelta):
            value = value.total_seconds()
        out[column.key] = value
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_account(user):
    """Everything this installation holds for `user`, as a JSON-ready dict.

    Household-wide rather than user-only, and that is the honest scope: the
    person is a member of the household, the household's ledger is the data they
    actually see in the application, and an export that returned only rows
    carrying their user id would be nearly empty — `Transaction` has no author
    column, because transactions belong to the household. Handing somebody a
    file called "your data" that omits every transaction they have ever looked at
    would be portability in name only.

    Must be called inside a `tenant_scope` for the user's household. The scoped
    queries below are the same ones the application uses, so the ORM tenant
    backstop applies: this cannot read another household's rows even if the
    caller passes the wrong user.
    """
    models = _models()

    household = models.db.session.get(models.Household, user.household_id)
    payload = {
        'export_version': 1,
        'generated_at': dt.datetime.utcnow().isoformat() + 'Z',
        'about': (
            'Everything Dough holds for your household. Financial records '
            'belong to the household rather than to one member, so this file '
            'covers the whole household you belong to. Credentials '
            '(password hashes, institution access tokens, API token hashes) '
            'are deliberately excluded.'),
        'account': {
            'username': user.username,
            'email': user.email,
            'role': user.role,
            'created_at': (user.created_at.isoformat()
                           if getattr(user, 'created_at', None) else None),
            'email_verified_at': (user.email_verified_at.isoformat()
                                  if user.email_verified_at else None),
        },
        'household': {
            'id': household.id if household else user.household_id,
            'name': household.name if household else None,
            'members': [
                {'username': m.username, 'email': m.email, 'role': m.role}
                for m in (household.members if household else [])
            ],
        },
        'data': {},
    }

    for key, model_name in _SCOPED_TABLES:
        model = getattr(models, model_name)
        payload['data'][key] = [_serialize(row) for row in model.query.all()]

    payload['counts'] = {key: len(rows) for key, rows in payload['data'].items()}
    return payload


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _household_members(household_id):
    models = _models()
    from dough.tenancy import unscoped

    with unscoped():
        return (models.AppUser.query
                .filter(models.AppUser.household_id == household_id)
                .all())


def deletion_preview(user):
    """What deleting `user` would remove, without removing it.

    The confirmation screen is built from this rather than from prose, so what
    somebody is told is generated by the same code that does the work. A warning
    maintained separately from the operation it warns about drifts, and the
    direction it drifts is "understates".
    """
    models = _models()

    members = _household_members(user.household_id)
    others = [m for m in members if m.id != user.id]
    last_member = not others

    counts = {}
    if last_member:
        for key, model_name in _SCOPED_TABLES:
            model = getattr(models, model_name)
            count = model.query.count()
            if count:
                counts[key] = count

    return {
        'last_member': last_member,
        'other_members': [m.username for m in others],
        'household_data_removed': last_member,
        'counts': counts,
        'blocked_reason': _blocked_reason(user, others),
    }


def _blocked_reason(user, others):
    """Why this deletion cannot proceed, or None."""
    models = _models()

    if not others:
        return None
    remaining_owners = [m for m in others if m.role == models.ROLE_OWNER]
    if user.role == models.ROLE_OWNER and not remaining_owners:
        return ("You are the only owner of a household with other members. "
                "Make somebody else an owner first, so the household is not "
                "left without one.")
    return None


def delete_account(user, *, actor_user_id=None):
    """Delete `user`, and the household's data when they are the last member.

    Returns the preview dict describing what was removed, so a caller can tell
    the person what happened rather than guessing.

    Must be called inside a `tenant_scope` for the user's household: the bulk
    deletes below go through the scoped query path, which is what guarantees
    they cannot reach another household's rows. That is deliberate — a
    `DELETE FROM transactions` written unscoped is one missing WHERE clause away
    from being the worst bug this application could have.
    """
    models = _models()
    from dough.services import audit
    from dough.tenancy import unscoped

    members = _household_members(user.household_id)
    others = [m for m in members if m.id != user.id]

    blocked = _blocked_reason(user, others)
    if blocked:
        raise AccountLifecycleError(blocked)

    summary = deletion_preview(user)
    household_id = user.household_id
    username = user.username

    # Recorded *before* the rows go, for two reasons. The household may not
    # exist afterwards, and `audit.record` defaults its household from the
    # current context — so an event written after the delete would either fail
    # or attach to nothing. It is also the ordering that survives a crash
    # halfway through: an audit row for a deletion that did not complete is
    # recoverable confusion; a completed deletion nobody recorded is not.
    audit.record(models.EVENT_ACCOUNT_DELETED,
                 household_id=household_id,
                 actor_user_id=actor_user_id or user.id,
                 entity_type='user', entity_id=user.id,
                 metadata={'username': username,
                           'last_member': summary['last_member'],
                           'household_id': household_id,
                           'counts': summary['counts']})

    # 1. The credentials, always. Deleting these first means that if anything
    #    below fails, the account is already unusable rather than half-deleted
    #    and still able to sign in.
    with unscoped():
        models.ApiToken.query.filter(
            models.ApiToken.user_id == user.id).delete(synchronize_session=False)
        models.EmailVerification.query.filter(
            models.EmailVerification.user_id == user.id).delete(
                synchronize_session=False)

    # 2. The household's data, only when nobody is left to own it.
    if summary['last_member']:
        for _key, model_name in _SCOPED_TABLES:
            model = getattr(models, model_name)
            # Scoped: `TenantScopedQuery` adds the household filter, so this is
            # a delete within one household by construction rather than by the
            # author remembering a WHERE clause.
            model.query.delete(synchronize_session=False)

    # 3. The login itself.
    with unscoped():
        models.db.session.delete(user)

    # 4. The household, once it is empty.
    if summary['last_member']:
        with unscoped():
            household = models.db.session.get(models.Household, household_id)
            if household is not None:
                models.db.session.delete(household)

    models.db.session.commit()
    logger.info('Deleted account %s (household=%s, last_member=%s)',
                username, household_id, summary['last_member'])
    return summary
