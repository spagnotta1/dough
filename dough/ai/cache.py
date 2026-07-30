"""Caching for AI results, behind an interface Redis can implement later.

## What this replaces, and why it is a security fix in waiting

`app.py` held three module-level dicts:

    _insight_cache = {'text': None, 'expires': 0}
    _brief_cache   = {'data': None, 'expires': 0, 'key': None}
    _wealth_cache  = {'data': None, 'expires': 0}

Process-global, and keyed only by time (the brief also by date window). With one
owner that is merely untidy. **The moment Phase 5 adds households it is a
cross-tenant data leak**: household B's dashboard would render household A's
cached AI paragraph about A's spending, because the cache has no idea whose
money it is describing. It is the highest-severity finding in the audit.

Phase 4 does not add tenancy, so it cannot fix that yet — and this module does
not pretend to. What it does is make the fix a one-line change instead of an
archaeology exercise:

- **Every key goes through `CacheKey`**, which has a `scope` field. Today
  `AIService` fills it with `GLOBAL_SCOPE`; in Phase 5 it becomes
  `current_household()` and every cache entry is namespaced at once.
- **`scope` is not optional and has no default.** A caller cannot forget it.
  When Phase 5 changes what fills it, anything that failed to be updated is a
  type error, not a silent leak.

`tests/test_ai_adapter.py` asserts the three dicts are gone from `app.py` and
that two different scopes cannot read each other's entries.

## Why not just use functools.lru_cache

It has no TTL, no invalidation, no scope, and it keys on arguments — which for
these calls are multi-kilobyte JSON snapshots. The interesting part of caching
here is exactly the parts `lru_cache` does not model.

Allowed:   stdlib only
Must not:  app, models, flask, anthropic, other dough packages
"""

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

#: The scope used while the app is single-tenant. Phase 5 replaces the *value*
#: passed by AIService, not the shape -- see the module docstring.
GLOBAL_SCOPE = 'global'


@dataclass(frozen=True)
class CacheKey:
    """A scoped, namespaced cache key.

    `scope` first and required: it is the tenancy boundary, and putting it in
    the leading position of the string form means a scope's entries are
    contiguous under any prefix scan a Redis implementation wants to do.
    """

    scope: str
    surface: str
    variant: str = ''

    def __post_init__(self):
        if not self.scope:
            raise ValueError(
                'CacheKey.scope may not be empty -- it is the tenancy boundary. '
                'Pass GLOBAL_SCOPE explicitly if there is genuinely no tenant.')
        if not self.surface:
            raise ValueError('CacheKey.surface may not be empty')

    def __str__(self):
        return f'{self.scope}:{self.surface}:{self.variant}'


class Cache(ABC):
    """A TTL cache. Implementations must be safe to call from any thread.

    Deliberately tiny. `get`/`set`/`invalidate`/`clear` is everything the AI
    layer needs, and a small interface is what makes a Redis implementation a
    contained piece of work rather than a rewrite.
    """

    @abstractmethod
    def get(self, key: CacheKey) -> Optional[Any]:
        """The cached value, or None when absent or expired."""

    @abstractmethod
    def set(self, key: CacheKey, value: Any, ttl: float) -> None:
        """Store `value` for `ttl` seconds. A ttl <= 0 stores nothing."""

    @abstractmethod
    def invalidate(self, key: CacheKey) -> None:
        """Drop one entry. Absent keys are not an error."""

    @abstractmethod
    def clear(self, scope: Optional[str] = None) -> None:
        """Drop every entry, or every entry in one scope.

        The scoped form is what Phase 6 needs on logout and Phase 5 on a
        household's data changing underneath a cached paragraph.
        """


class MemoryCache(Cache):
    """In-process dict with expiry. The default, and correct for one worker.

    Locked because the sync scheduler runs on a background thread and could
    plausibly warm a cache while a request reads it. `dict` operations are
    individually atomic under the GIL, but "read, check expiry, delete" is not,
    and the lock costs nothing at this call volume.

    Entries expire lazily on read. A sweeper thread would be the wrong trade:
    there are at most a handful of keys per scope, so the memory a stale entry
    holds is bounded and small, and a background thread is a thing that can go
    wrong at 3am.
    """

    def __init__(self, time_source=time.monotonic):
        # monotonic, not time.time: a clock adjustment (NTP step, DST on a badly
        # configured host) must not make a cached entry appear to live for hours
        # or expire instantly.
        self._now = time_source
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, key):
        text = str(key)
        with self._lock:
            entry = self._entries.get(text)
            if entry is None:
                return None
            expires, value = entry
            if self._now() >= expires:
                del self._entries[text]
                return None
            return value

    def set(self, key, value, ttl):
        if ttl <= 0:
            return
        with self._lock:
            self._entries[str(key)] = (self._now() + ttl, value)

    def invalidate(self, key):
        with self._lock:
            self._entries.pop(str(key), None)

    def clear(self, scope=None):
        with self._lock:
            if scope is None:
                self._entries.clear()
                return
            prefix = f'{scope}:'
            for text in [k for k in self._entries if k.startswith(prefix)]:
                del self._entries[text]

    # -- introspection, for tests and a future /healthz -----------------------

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def keys(self):
        with self._lock:
            return sorted(self._entries)


class NullCache(Cache):
    """Caches nothing. For tests that must see every call reach the adapter."""

    def get(self, key):
        return None

    def set(self, key, value, ttl):
        pass

    def invalidate(self, key):
        pass

    def clear(self, scope=None):
        pass


__all__ = ['Cache', 'CacheKey', 'MemoryCache', 'NullCache', 'GLOBAL_SCOPE']
