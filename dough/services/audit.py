"""The audit trail: recording it, reading it, and keeping it honest.

Allowed: sqlalchemy, models, dough.tenancy, flask.request, stdlib
Must not: app, dough.blueprints, anthropic, Flask response helpers

`flask.request` is the one Flask import here, and only to read provenance (the
address and user agent) when there happens to be a request. Every function works
without one -- the scheduler and the sync engine call `record()` from a worker
thread -- which is why the request is consulted through `has_request_context()`
rather than assumed.

Three properties this module exists to guarantee:

1. **Append-only.** A `before_flush` hook raises on any UPDATE or DELETE of an
   `AuditEvent`, so the guarantee holds against code that never calls this
   module at all. Documented-only append-only is not a property, it is a hope.

2. **Recording never breaks the thing being recorded.** `record()` swallows its
   own failures and logs them. A member removal that succeeded and then raised
   because the audit insert hit a constraint would leave the caller with a
   traceback for an operation that actually happened -- worse than a missing
   audit row, and much harder to reason about afterwards.

3. **Nothing sensitive is stored.** `metadata` goes through a redactor with a
   deny-list of key names and a value scanner for things that look like
   credentials. What the audit log needs is that an event happened and to what;
   it does not need the payload.
"""

from __future__ import annotations

import json
import logging
import re

from flask import has_request_context, request
from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger('dough.audit')

#: Metadata keys that are never stored, whatever their value. Matched
#: case-insensitively as a substring, so `plaid_access_token` and `ACCESS_TOKEN`
#: are both caught by `token`.
REDACTED_KEYS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'key',
    'authorization', 'cookie', 'session', 'access', 'refresh', 'credential',
    'account_number', 'acct', 'iban', 'routing', 'ssn', 'card', 'cvv', 'pan',
    'prompt', 'completion', 'message', 'content', 'answer', 'reply',
)

#: Values that look like a credential regardless of the key they arrived under.
#: Deliberately crude -- a false positive costs a redacted metadata value, a
#: false negative costs a secret in a table that is never deleted.
_SECRETISH = re.compile(
    r'(sk-[A-Za-z0-9_\-]{16,})'            # Anthropic / OpenAI style
    r'|(access-(sandbox|development|production)-[0-9a-f\-]{8,})'   # Plaid
    r'|(\b\d{12,19}\b)'                    # bare card/account-length digits
)

REDACTED = '[redacted]'

#: How much of a metadata value is kept. An audit row is not a place to put a
#: paragraph, and a bounded column is one fewer way for a caller to turn the
#: audit log into storage.
MAX_VALUE_CHARS = 200


def _redact_value(value):
    if isinstance(value, str):
        if _SECRETISH.search(value):
            return REDACTED
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in list(value)[:20]]
    return _redact_value(str(value))


def redact(metadata):
    """Return a copy of `metadata` safe to persist.

    Exported and tested directly, because the interesting cases are the ones no
    call site produces today -- a future caller passing the whole Plaid response
    is exactly who this is for.
    """
    if not metadata:
        return {}
    out = {}
    for key, value in metadata.items():
        name = str(key)
        if any(bad in name.lower() for bad in REDACTED_KEYS):
            out[name] = REDACTED
        else:
            out[name] = _redact_value(value)
    return out


def _provenance():
    """(ip, user_agent) for this call, or (None, None) outside a request.

    The address comes from `dough.auth.client_address`, which honours
    TRUSTED_PROXIES rather than believing `X-Forwarded-For`. Recording an
    attacker-chosen address in an audit log would make the log actively
    misleading, which is worse than leaving it null.
    """
    if not has_request_context():
        return None, None
    from dough.auth import client_address
    agent = request.headers.get('User-Agent')
    return client_address(), (agent[:255] if agent else None)


def record(event_type, *, household_id=None, actor_user_id=None,
           entity_type=None, entity_id=None, metadata=None, commit=True):
    """Append one event. Returns the row, or None if it could not be written.

    `household_id` and `actor_user_id` default to whatever the current context
    knows. Pass them explicitly for events about somebody else (a removal names
    the removed member as the entity, the acting owner as the actor) and for
    events with no context at all (a failed login).

    `commit=False` joins the caller's transaction, so an event describing a
    change can be committed atomically with it. The default commits, because
    most callers are recording something that has already happened.
    """
    from models import AUDIT_EVENT_TYPES, AuditEvent, db

    if event_type not in AUDIT_EVENT_TYPES:
        # A typo here is silent data loss at exactly the moment somebody is
        # trying to reconstruct an incident, so it is loud in development and
        # still non-fatal in production.
        raise ValueError(f'unknown audit event type: {event_type!r}')

    # Everything below is inside the guard, not just the write. Resolving the
    # context can fail on its own: `current_household()` needs an application
    # context, and `dough/ai/service.py` calls this from a unit test that has
    # never built one. An audit helper that raises while working out *who* to
    # attribute an event to has failed in exactly the way the docstring
    # promises it cannot.
    try:
        if household_id is None or actor_user_id is None:
            from dough.tenancy import current_household
            if household_id is None:
                household_id = current_household()
            if actor_user_id is None and has_request_context():
                from flask import session
                actor_user_id = session.get('user_id')

        ip, agent = _provenance()
        payload = redact(metadata)

        row = AuditEvent(
            household_id=household_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip,
            user_agent=agent,
            metadata_json=json.dumps(payload, default=str) if payload else None,
        )
        db.session.add(row)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return row
    except Exception:
        # See the module docstring: the audit trail must not be able to fail the
        # operation it is describing.
        logger.exception('audit: failed to record %s', event_type)
        if commit:
            try:
                db.session.rollback()
            except Exception:
                # No session to roll back -- there was no application context in
                # the first place. Nothing was written, so nothing to undo.
                pass
        return None


def recent(*, limit=100, event_type=None, actor_user_id=None):
    """This household's events, newest first.

    The only read path, deliberately. `AuditEvent` is not covered by the ORM
    tenant backstop (its `household_id` is nullable), so the filter here is the
    isolation -- and one function is a boundary that can be reviewed, whereas
    "remember to filter" spread across callers is not.

    Rows with a NULL household are never returned: they belong to no tenant, and
    an operator reading the database is their audience.
    """
    from models import AuditEvent

    from dough.tenancy import require_household, unscoped

    household_id = require_household()
    with unscoped():
        # `unscoped()` because AuditEvent is not a scoped model -- without it the
        # backstop is not involved either way, but being explicit keeps this
        # inside the audited set of relaxations rather than looking like an
        # oversight. The filter below is what does the work.
        query = (AuditEvent.query
                 .filter(AuditEvent.household_id == household_id))
        if event_type is not None:
            query = query.filter(AuditEvent.event_type == event_type)
        if actor_user_id is not None:
            query = query.filter(AuditEvent.actor_user_id == actor_user_id)
        return (query.order_by(AuditEvent.created_at.desc(),
                               AuditEvent.id.desc())
                .limit(limit).all())


class AuditImmutableError(RuntimeError):
    """Raised on any attempt to modify or delete a recorded event."""


@event.listens_for(Session, 'before_flush')
def _audit_is_append_only(session, flush_context, instances):
    """The append-only guarantee, enforced where it cannot be bypassed.

    A `before_flush` hook rather than a database trigger because SQLite triggers
    would not survive the `batch_alter_table` rebuilds this schema has already
    needed, and because the error should name the application concept rather
    than surface as an opaque IntegrityError.

    It does mean an operator with a SQL prompt can still edit the table. That is
    true of any application-level control and is the honest boundary: this stops
    the *application* from rewriting its own history, including by accident.
    """
    from models import AuditEvent

    for obj in session.dirty:
        if isinstance(obj, AuditEvent) and session.is_modified(obj):
            raise AuditImmutableError(
                'audit events are append-only; '
                f'attempted to modify event {getattr(obj, "id", "?")}')
    for obj in session.deleted:
        if isinstance(obj, AuditEvent):
            raise AuditImmutableError(
                'audit events are append-only; '
                f'attempted to delete event {getattr(obj, "id", "?")}')
