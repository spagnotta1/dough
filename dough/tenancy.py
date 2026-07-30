"""Which household's data is this, and who is allowed to ask for it.

Phase 5. Authentication (Phase 6) answers *who are you*; this module answers
*which rows may you see*. They are different questions and this file only
answers the second one — nothing here reads a session, a cookie or a user.
It is handed a household id by whichever entry point knows one, and everything
downstream inherits it.

## The two halves of the boundary

`tests/test_tenancy_boundary.py` was written in Phase 0, before any of this
existed, and it splits the contract deliberately:

1. **The ORM backstop.** A `do_orm_execute` handler adds
   `household_id = <current>` to every statement touching a tenant-scoped
   entity, and a `before_flush` handler stamps inserts and refuses cross-tenant
   writes. This is defense in depth.
2. **Explicit authorization.** Every route that resolves a caller-supplied id
   carries its own household predicate, via `get_owned`, and is tested with the
   backstop switched off.

Half 2 is the load-bearing one. Half 1 exists because the day someone adds a
raw `text()` query, a bulk `update()`, or a background job, half 2 is what they
will forget. Neither is allowed to be the only one.

## Why ContextVar and not flask.g

`flask.g` is bound to an application context, so anything reading it outside a
request either crashes or silently sees nothing. This application already has
four such callers: the sync scheduler's daemon thread, the `_due_for_scheduled_sync`
poll, Flask CLI commands, and Alembic. A tenancy mechanism that only works
inside a request is a tenancy mechanism with four holes in it.

`ContextVar` works in all of them, and has a property that matters more than
convenience here: **a new thread does not inherit the parent's context.**
`threading.Thread` starts with a fresh one. So a background worker cannot
accidentally run under whatever household the request that spawned it happened
to be serving — it gets no household at all, and the fail-closed rules below
turn that into an exception rather than a leak.

## Fail closed, and loudly

No tenant context does not mean "all rows" and does not mean "no rows".
It raises `TenantContextMissing`.

"All rows" is the leak. "No rows" is worse than it sounds: a broken tenant
context would be indistinguishable from an empty account, so it would be
reported as missing data, investigated as a data bug, and never recognised as
a security failure. An exception is the only outcome that cannot be mistaken
for something else.

Allowed:   sqlalchemy, werkzeug, stdlib
Must not:  app, models, flask (except the werkzeug abort used by get_owned)
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import Column, ForeignKey, Integer, event
from sqlalchemy.orm import Session, declared_attr, with_loader_criteria
from sqlalchemy.orm.attributes import get_history
from werkzeug.exceptions import NotFound

#: The household every statement in this context is confined to.
#: Unset (None) is an error at the point of use, never a wildcard.
_household: ContextVar[Optional[int]] = ContextVar('dough_household', default=None)

#: Whether the ORM backstop is switched off for this context. Set only by
#: `unscoped()`, which is an explicit, greppable statement that the caller has
#: taken responsibility for scoping.
_backstop_off: ContextVar[bool] = ContextVar('dough_backstop_off', default=False)


class TenancyError(Exception):
    """Base class, so a caller can catch the whole family."""


class TenantContextMissing(TenancyError):
    """A tenant-scoped operation ran with no household bound.

    Always a programming error at an entry point: some code path reached the
    database without passing through `tenant_scope`. The fix is to bind the
    context at that entry point, never to relax this check.
    """


class CrossTenantWrite(TenancyError):
    """A write would have created or moved a row into another household.

    Raised at flush time by the `before_flush` guard, for three shapes: an
    insert carrying a foreign `household_id`, an update to a row belonging to
    another household, and re-parenting a row from one household to another.
    """


# ---------------------------------------------------------------------------
# Reading and binding the context
# ---------------------------------------------------------------------------

def current_household() -> Optional[int]:
    """The bound household id, or None. Does not raise — see `require_household`."""
    return _household.get()


def require_household() -> int:
    """The bound household id, or raise.

    This is what authorization code should call. `current_household()` returning
    None is only interesting to code deciding whether to bind one.
    """
    hid = _household.get()
    if hid is None:
        raise TenantContextMissing(
            'No household is bound to this execution context. Every entry point '
            'that touches tenant data must run inside tenant_scope(household_id) '
            '— see dough/tenancy.py for the list of entry points and why this '
            'raises rather than returning an empty result.')
    return hid


def backstop_enabled() -> bool:
    """Whether the ORM filter is currently applied. False inside `unscoped()`."""
    return not _backstop_off.get()


@contextlib.contextmanager
def tenant_scope(household_id: int):
    """Bind `household_id` for the duration of the block.

    Nests: an inner scope restores the outer one on exit, which is what makes
    a per-household loop in the sync scheduler safe.
    """
    if household_id is None:
        raise ValueError(
            'tenant_scope(None) is not a way to see everything. Use unscoped() '
            'if that is genuinely what you mean, and say why in a comment.')
    token = _household.set(int(household_id))
    try:
        yield int(household_id)
    finally:
        _household.reset(token)


@contextlib.contextmanager
def unscoped():
    """Switch the *read* filter off for the block. The bound household stays bound.

    Three legitimate uses, and they are the only ones:

    - Migration, backfill and operational tooling, which must see every row.
    - Reading or writing the tenant registry itself (`Household`), which is not
      tenant-scoped because it *is* the tenant.
    - `find_owned`, which supplies its own explicit predicate and must not be
      seen to pass merely because the backstop was doing the work — that is the
      distinction `tests/test_tenancy_boundary.py` exists to enforce.

    **It does not switch off the write guard**, and the asymmetry is deliberate.
    An earlier version relaxed both, on the theory that `unscoped()` meant "I
    have taken responsibility". That was wrong for a reason worth recording,
    because it is invisible on inspection: SQLAlchemy *autoflushes before every
    query*. So a pending insert plus any read inside an `unscoped()` block — for
    instance `db.session.add(msg)` followed by `find_owned(Conversation, id)` in
    the chat-stream route — flushed that insert with the guard switched off, the
    row was never stamped, and it hit the database with a NULL household_id.
    Which is precisely the state `nullable=False` exists to make impossible.

    Nothing needs writes unscoped. The reads are the part that occasionally has
    to see across households; a write that genuinely targets another household
    is `tenant_scope(that_household)`, said out loud.

    Deliberately not spelled as a keyword argument on a query helper. It should
    be one greppable token, so `grep -rn 'unscoped()'` is a complete audit of
    everywhere the safety net is off.
    """
    token = _backstop_off.set(True)
    try:
        yield
    finally:
        _backstop_off.reset(token)


# ---------------------------------------------------------------------------
# The mixin
# ---------------------------------------------------------------------------

class TenantScopedMixin:
    """Marks a model as belonging to exactly one household.

    Declared with raw SQLAlchemy rather than `db.Column` so this module never
    imports `models` — models imports tenancy, and the reverse would be a cycle.

    `nullable=False` is the point of the whole exercise: a row with no household
    belongs to nobody and, under any filter that treats NULL as a wildcard,
    to everybody. The migration backfills before it applies the constraint.
    """

    @declared_attr
    def household_id(cls):  # noqa: N805 — declared_attr receives the class
        return Column(Integer, ForeignKey('households.id'),
                      nullable=False, index=True)

    @classmethod
    def scoped_query(cls):
        """`cls.query` with the household predicate written out explicitly.

        For code that wants half 2 of the contract without going through
        `get_owned` — a list endpoint, say. Costs one redundant clause when the
        backstop is on, which is the price of not depending on it.
        """
        from models import db
        return db.session.query(cls).filter(cls.household_id == require_household())


def apply_tenant_predicate(query, entities):
    """Add an explicit `household_id = <current>` for each tenant-scoped entity.

    Exists for the one place the `do_orm_execute` backstop cannot reach: see
    `models.TenantScopedQuery.count`. Lives here rather than in models.py so the
    policy — when to filter, what to filter on, when to raise — stays in one
    file, and models.py holds only the Flask-SQLAlchemy plumbing.
    """
    scoped = [e for e in entities
              if isinstance(e, type) and issubclass(e, TenantScopedMixin)]
    if not scoped or not backstop_enabled():
        return query
    household_id = require_household()
    for entity in scoped:
        query = query.filter(entity.household_id == household_id)
    return query


def tenant_scoped_models():
    """Every mapped class carrying a household, resolved at call time.

    Deliberately derived from the mapper registry rather than a hand-maintained
    list: a model added in a later phase is covered the moment it inherits the
    mixin, and `tools/verify_tenancy.py` cannot drift from what is actually
    mapped.
    """
    from models import db
    return sorted(
        (m.class_ for m in db.Model.registry.mappers
         if issubclass(m.class_, TenantScopedMixin)),
        key=lambda c: c.__tablename__)


# ---------------------------------------------------------------------------
# Explicit authorization
# ---------------------------------------------------------------------------

def find_owned(model, ident, *, options=None):
    """Load `ident` from `model`, or None unless the current household owns it.

    The sanctioned way for a route to resolve a caller-supplied id. Three
    properties, each of which a hand-written `Model.query.get(id)` gets wrong:

    - **It runs its own household predicate**, inside `unscoped()`, so it is
      not relying on the ORM backstop. This is deliberate: a route tested only
      with the backstop on is a route whose authorization is untested, and the
      backstop is exactly what a later `text()` query or background job will
      slip past.
    - **Not found and not yours are the same answer.** Distinguishing them
      confirms the row exists, which turns a sequential id into an oracle for
      how many transactions another household has.
    - **It queries rather than using `Session.get`**, which would hand back an
      object already in the identity map without going near the database — the
      one case where the backstop does nothing at all.
    """
    from models import db

    household_id = require_household()
    with unscoped():
        query = db.session.query(model).filter(
            model.id == ident, model.household_id == household_id)
        if options:
            query = query.options(*options)
        return query.first()


def get_owned(model, ident, *, options=None):
    """`find_owned`, but aborts with 404 rather than returning None.

    The right default for a route: the miss and the refusal produce the same
    response, so neither the caller nor a log reader can tell them apart.
    Use `find_owned` where the route already renders its own not-found body.
    """
    row = find_owned(model, ident, options=options)
    if row is None:
        raise NotFound(
            f'No {model.__name__} with id {ident!r} in this household.')
    return row


def owns(row) -> bool:
    """Whether the current household owns an already-loaded row.

    For the case where the object arrived from somewhere other than an id in
    the URL — a relationship traversal, a service return value — and the
    business layer wants to say so out loud before acting on it.
    """
    if row is None:
        return False
    return getattr(row, 'household_id', None) == require_household()


# ---------------------------------------------------------------------------
# The ORM backstop
# ---------------------------------------------------------------------------

def _statement_touches_tenant_data(execute_state) -> bool:
    """Whether this statement involves any tenant-scoped mapper.

    The check matters as much for what it lets through as for what it catches.
    `AppUser.query.filter_by(username=...)` runs during login, before any
    household is known, and `Household.query.get(...)` runs while resolving one.
    Raising on those would make signing in impossible.
    """
    return any(issubclass(mapper.class_, TenantScopedMixin)
               for mapper in execute_state.all_mappers)


@event.listens_for(Session, 'do_orm_execute')
def _apply_tenant_filter(execute_state):
    """Confine every SELECT/UPDATE/DELETE to the bound household."""
    # A column load is a deferred/expired attribute being refreshed on an object
    # already loaded and already filtered. There is no entity to constrain, and
    # constraining it breaks refresh.
    if execute_state.is_column_load:
        return
    if not _statement_touches_tenant_data(execute_state):
        return
    if not backstop_enabled():
        return

    # Relationship loads are *not* skipped, though the usual recipe skips them.
    # propagate_to_loaders carries the criteria to lazy loads emitted from the
    # same query, but not to one emitted later from a detached-then-merged
    # object or from a second session. Applying it again costs a redundant AND
    # and closes that gap.
    household_id = require_household()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            # `household_id` is a plain int closure variable, which SQLAlchemy's
            # lambda caching extracts as a bound parameter and re-reads on every
            # execution. Anything richer than an int here would be baked into
            # the cached statement and serve one household another's rows --
            # which is why this stays an int and the test suite asserts two
            # scopes in sequence see different data.
            lambda cls: cls.household_id == household_id,
            include_aliases=True))


@event.listens_for(Session, 'before_flush')
def _guard_tenant_writes(session, flush_context, instances):
    """Stamp inserts with the current household; refuse anything cross-tenant.

    Runs before the INSERT/UPDATE is emitted, so a rejected write never reaches
    the database and the caller's `rollback()` has something to roll back.

    Note what is *not* here: a `backstop_enabled()` check. Writes are guarded
    even inside `unscoped()` — see that function for the autoflush bug that
    caused, and why the read filter and this guard are not the same switch.
    """
    scoped_new = [o for o in session.new if isinstance(o, TenantScopedMixin)]
    scoped_dirty = [o for o in session.dirty if isinstance(o, TenantScopedMixin)]
    scoped_deleted = [o for o in session.deleted if isinstance(o, TenantScopedMixin)]
    if not (scoped_new or scoped_dirty or scoped_deleted):
        return

    household_id = require_household()

    for obj in scoped_new:
        if obj.household_id is None:
            # The ordinary path: application code never sets household_id, and
            # never has to remember to.
            obj.household_id = household_id
        elif obj.household_id != household_id:
            raise CrossTenantWrite(
                f'{type(obj).__name__} was created with household_id='
                f'{obj.household_id} while household {household_id} is bound.')

    for obj in scoped_dirty:
        history = get_history(obj, 'household_id')
        if history.deleted and history.deleted[0] != history.added[0]:
            raise CrossTenantWrite(
                f'{type(obj).__name__} id={getattr(obj, "id", "?")} was '
                f're-parented from household {history.deleted[0]} to '
                f'{history.added[0]}. Rows do not move between households; '
                f'copy and delete if that is really the intent.')
        if obj.household_id != household_id:
            raise CrossTenantWrite(
                f'{type(obj).__name__} id={getattr(obj, "id", "?")} belongs to '
                f'household {obj.household_id}, not the bound household '
                f'{household_id}.')

    for obj in scoped_deleted:
        if obj.household_id != household_id:
            raise CrossTenantWrite(
                f'{type(obj).__name__} id={getattr(obj, "id", "?")} belongs to '
                f'household {obj.household_id} and cannot be deleted by '
                f'household {household_id}.')


__all__ = [
    'CrossTenantWrite',
    'TenancyError',
    'TenantContextMissing',
    'TenantScopedMixin',
    'backstop_enabled',
    'current_household',
    'find_owned',
    'get_owned',
    'owns',
    'require_household',
    'tenant_scope',
    'tenant_scoped_models',
    'unscoped',
]
