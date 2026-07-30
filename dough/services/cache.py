"""A TTL cache that cannot serve one household another household's data.

## What this closes

`docs/security.md` SEC-0003, the highest-severity finding of the audit. `app.py`
used to hold three module-level dicts:

    _insight_cache = {'text': None, 'expires': 0}
    _brief_cache   = {'data': None, 'expires': 0, 'key': None}
    _wealth_cache  = {'data': None, 'expires': 0}

Process-global, keyed only by time. Their contents are generated English prose
about a specific family's spending — "you spent $412 on dining, mostly at
weekends". With one owner that is untidy. With households it is a disclosure of
one family's finances to another, served from cache, with no query ever reaching
the database to be filtered.

Phase 4 moved the storage behind `dough/ai/cache.py`, whose `CacheKey` carries a
required `scope` field, and left `AIService` filling it with a constant. This
module is the other end of that: the scope is now the household, resolved at
call time.

## Why the household is read on every call and never stored

A cache instance outlives any one request, so a household captured at
construction is a household that is wrong for every later caller. Reading it
per operation is what makes a single shared instance safe, and it means a
missing tenant context raises here — at the cache — rather than silently
producing an entry under a stale scope.

That is the property `tests/test_tenancy_boundary.py::
test_ai_caches_do_not_leak_between_households` asserts, and it asserts it
behaviourally: a value stored under household A must be invisible from
household B. Not by inspecting the shape of the keys, so that any correct
implementation satisfies it and an empty cache cannot make it pass vacuously.

Allowed:   dough.ai.cache, dough.tenancy, stdlib
Must not:  app, models, flask, anthropic
"""

from dough.ai.cache import Cache, CacheKey, MemoryCache
from dough.tenancy import require_household


def household_scope():
    """The current household as a cache scope string.

    Raises `TenantContextMissing` when there is none, rather than falling back
    to a shared scope. A shared fallback is exactly the bug this module exists
    to remove: it would be reached only in the situations nobody anticipated,
    which are the situations where two callers would collide.
    """
    return f'household:{require_household()}'


class TenantScopedTTLCache:
    """A small get/set/invalidate cache, namespaced by the current household.

    Wraps a `dough.ai.cache.Cache` rather than reimplementing expiry, so the
    Redis backend that interface exists for arrives here too, without this class
    changing. The narrower surface is on purpose: callers pass a plain surface
    name and never construct a `CacheKey`, so there is no way to spell a key
    that omits the scope.
    """

    def __init__(self, ttl=3600, backend=None, scope_provider=household_scope):
        self.ttl = ttl
        self._backend: Cache = backend if backend is not None else MemoryCache()
        self._scope_provider = scope_provider

    def _key(self, surface, variant=''):
        return CacheKey(scope=self._scope_provider(), surface=surface,
                        variant=variant)

    def get(self, surface, variant=''):
        """The cached value for this household, or None when absent or expired."""
        return self._backend.get(self._key(surface, variant))

    def set(self, surface, value, variant='', ttl=None):
        """Store `value` for this household. A ttl <= 0 stores nothing."""
        self._backend.set(self._key(surface, variant),
                          value, self.ttl if ttl is None else ttl)

    def invalidate(self, surface, variant=''):
        """Drop one entry for this household. Absent keys are not an error."""
        self._backend.invalidate(self._key(surface, variant))

    def clear(self, all_households=False):
        """Drop this household's entries, or every household's.

        `all_households` is the one operation that does not resolve a scope, so
        it is a keyword the caller has to type, not a default.
        """
        self._backend.clear(None if all_households else self._scope_provider())


__all__ = ['TenantScopedTTLCache', 'household_scope']
