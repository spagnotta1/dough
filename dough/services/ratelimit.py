"""Rate limiting: the interface, the policies, and a backend for one process.
[Phase 10.5 — addresses SEC-0018]

Allowed:   flask.current_app, models, sqlalchemy, stdlib
Must not:  app, render_template, url_for, redirect, flash, jsonify, anthropic,
           blueprints

`current_app` is read only by `current_limiter()`. `check()` and `hit()` take
their key as an argument, so the limiter is callable from a worker thread and
from a test with no request.

## What this is and is not

It is the *seam*. SEC-0018 records why `/api/v1` has no limit today, and the
reason is not that nobody thought of it: the throttle this application already
has is in-memory and per-process (SEC-0010), so extending that to the API would
extend a control that resets on restart and does not span workers — the
appearance of a limit rather than one.

That argument is about the **backend**, not about the interface. So the
interface exists now, every call site is written against it, the policies are
declared and tested, and `MemoryBackend` is what stands behind them until a
shared store arrives. Swapping in Redis is then a `RATELIMIT_BACKEND` change and
one new class, rather than finding every call site under deadline.

`MemoryBackend`'s limitations are real and are not hidden. `Limiter.backend.name`
is `'memory'`, `/health/ready` does not claim otherwise, and SEC-0010's entry
covers this the same way it covers `LoginThrottle`.

## Why a fixed window and not a token bucket

The window resets on a boundary, so a caller can spend the whole allowance at
the end of one window and again at the start of the next — twice the nominal rate
across that seam. That is a known and accepted property.

The alternative that fixes it is a sliding log, which is what `LoginThrottle`
already does: it keeps a timestamp per attempt. That is affordable for login,
where the counts are single digits, and not for an AI budget of hundreds per day
per household, where the memory is a list that grows with traffic. A fixed window
is two integers whatever the limit is.

## Why policies are declared here and not passed in

A limit written at its call site is a limit nobody can review as a set. Reading
`POLICIES` shows that authentication is strict, AI is measured in tens per hour
and API traffic in hundreds — which is the comparison that says whether the
numbers are sane, and it is invisible when they live in five different files.

It is also what makes them testable: `tests/test_ratelimit.py` asserts on the
declared policies rather than on a route, so a policy loosened by accident fails
a test rather than being discovered in a bill.
"""

from __future__ import annotations

import threading
import time

from flask import current_app

__all__ = [
    'Decision',
    'Limiter',
    'MemoryBackend',
    'POLICIES',
    'Policy',
    'RateLimitBackend',
    'current_limiter',
    'policy_for',
]


class Policy:
    """How many, over how long, and what the refusal is called.

    `scope` names what the key must identify, and it is documentation with a
    test behind it rather than a hint: a policy declared `per='account'` whose
    call site passes an IP address is a limit that does not do what its name
    says, and `tests/test_ratelimit.py` checks the pairing.
    """

    __slots__ = ('name', 'limit', 'window_seconds', 'per', 'description')

    def __init__(self, name, limit, window_seconds, per, description):
        self.name = name
        self.limit = limit
        self.window_seconds = window_seconds
        self.per = per
        self.description = description

    def __repr__(self):
        return (f'<Policy {self.name} {self.limit}/{self.window_seconds}s '
                f'per {self.per}>')


class Decision:
    """The answer to `check()`: allowed, how many left, when the window resets.

    `retry_after` is present on a refusal because the caller needs it for a
    `Retry-After` header, and a limit that does not say when to come back
    produces clients that poll — which is more load than the limit was
    imposed to prevent.
    """

    __slots__ = ('allowed', 'policy', 'remaining', 'reset_at', 'retry_after')

    def __init__(self, allowed, policy, remaining, reset_at, retry_after):
        self.allowed = allowed
        self.policy = policy
        self.remaining = remaining
        self.reset_at = reset_at
        self.retry_after = retry_after

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        return (f'<Decision {"allow" if self.allowed else "refuse"} '
                f'{self.policy} remaining={self.remaining}>')


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

#: Every limit this application declares, in one readable table.
#:
#: **Declared is not the same as enforced, and the difference is deliberate.**
#: The first four policies have call sites today (`/register`,
#: `/forgot-password` twice, and the verification re-send). `ai`, `ai_daily`,
#: `api` and `api_write` do not: they are the shapes SEC-0018 describes, written
#: now so that the numbers are reviewable as a set and so that wiring them is a
#: one-line change per route rather than a design exercise.
#:
#: They are not wired yet for the reason SEC-0018 gives, which is about the
#: backend rather than the interface: `MemoryBackend` is per process and resets
#: on restart, so enforcing an AI budget with it would produce a ceiling that a
#: restart clears and a second worker doubles — the appearance of a cost control
#: rather than one, on the surface where the cost is real money.
#:
#: `tests/test_ratelimit.py::test_the_declared_policies_match_their_call_sites`
#: pins which of the two each policy is, so a policy that quietly acquires or
#: loses a call site is a failing test rather than a silent change in what this
#: application actually limits.
#:
#: The three tiers exist because the thing being protected differs. Authentication
#: is protecting a *credential*, so it is strict and the cost of a false positive
#: is a person waiting. AI is protecting a *bill*, so the window is long and the
#: count is what a household plausibly uses in a day rather than what one page
#: needs. The API is protecting the *process* — one worker, per OPS-0012 — so it
#: is generous enough that no legitimate client notices and low enough that one
#: token cannot saturate it.
#:
#: `login` is deliberately absent. `dough.auth.LoginThrottle` owns that, it is
#: shared between the web and API sign-in surfaces, and it does something this
#: cannot: two buckets, one keyed on address and one on account, with different
#: windows and a deliberate difference in what each is allowed to say. Replacing
#: it with a generic policy would lose that, so it stays where it is and this
#: table covers everything else.
POLICIES = {
    p.name: p for p in (
        Policy('password_reset', limit=5, window_seconds=3600, per='address',
               description='Requests for a reset link, per source address. Low '
                           'because each one sends mail to somebody who did not '
                           'ask for it.'),
        Policy('password_reset_account', limit=3, window_seconds=3600,
               per='account',
               description='Reset links for one account, however many addresses '
                           'ask. Stops one mailbox being flooded by a request '
                           'loop from many sources.'),
        Policy('register', limit=5, window_seconds=3600, per='address',
               description='New accounts from one source address. Each one '
                           'creates a household, so this is the only unauth '
                           'route that can grow the database.'),
        Policy('email_verification', limit=5, window_seconds=3600,
               per='account',
               description='Re-sends of a verification link, per account.'),
        Policy('ai', limit=60, window_seconds=3600, per='household',
               description='Model calls per household per hour. Cost-aware: '
                           'every one of these spends credits, and the ceiling '
                           'is per household so one cannot spend another\'s.'),
        Policy('ai_daily', limit=300, window_seconds=86400, per='household',
               description='The same budget over a day. An hourly limit alone '
                           'permits 24x its own number, which is the bill this '
                           'is actually about.'),
        Policy('api', limit=600, window_seconds=3600, per='token',
               description='General /api/v1 traffic, per token rather than per '
                           'user: revoking a misbehaving client must not '
                           'throttle the person who owns it.'),
        Policy('api_write', limit=120, window_seconds=3600, per='token',
               description='Unsafe methods, which cost a database write on a '
                           'single-writer SQLite file.'),
    )
}


def policy_for(name):
    """The named policy. Unknown names raise rather than defaulting.

    Defaulting to a permissive policy would make a typo'd name an *unlimited*
    route, which is the failure this whole module exists to prevent and the one
    nothing downstream would report.
    """
    try:
        return POLICIES[name]
    except KeyError:
        raise KeyError(
            f'Unknown rate-limit policy {name!r}. Declared policies are '
            f'{sorted(POLICIES)}.') from None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class RateLimitBackend:
    """Two methods, and a Redis implementation is both of them.

    `peek` answers without spending; `incr` spends and answers. They are separate
    because the call sites differ: a route that refuses before doing work wants
    `incr` (the attempt itself is the thing being counted), while a page that
    renders "23 of 60 used today" wants `peek` and must not consume one by
    asking.

    A Redis backend implements `incr` as `INCR` + `EXPIRE` in one pipeline and
    `peek` as `GET` + `TTL`. Nothing in the interface assumes a lock, a process,
    or a data structure, which is the property that makes it swappable.
    """

    name = 'base'

    def incr(self, key, window_seconds, *, now=None):
        """Count one hit. Returns `(count_after, reset_at)`."""
        raise NotImplementedError

    def peek(self, key, window_seconds, *, now=None):
        """Read the count without spending. Returns `(count, reset_at)`."""
        raise NotImplementedError

    def reset(self, key=None):
        """Clear one key, or everything. For tests and for an operator."""
        raise NotImplementedError


class MemoryBackend(RateLimitBackend):
    """One process, one dict, cleared on restart.

    Honest about what it is — see the module docstring and SEC-0010. It is
    correct for a single-worker deployment, which is the one this application
    documents (OPS-0012), and it is the appearance of a limit rather than a limit
    for anything else.

    Locked, because `finance_sync`'s scheduler runs on a background thread and
    the AI policies are the ones both it and a request can reach. Read-modify-
    write on a plain dict across two threads loses increments, and a limiter that
    undercounts fails open.
    """

    name = 'memory'

    #: Expired entries are swept every N calls rather than on a timer. A timer
    #: means a thread; this costs one modulo on a counter and bounds the dict to
    #: roughly the number of distinct keys seen within one window, which for
    #: per-household and per-token keys is small by construction.
    SWEEP_EVERY = 512

    def __init__(self, clock=time.time):
        self._clock = clock
        self._counts = {}
        self._lock = threading.Lock()
        self._calls = 0

    def _window_start(self, now, window_seconds):
        """Which fixed window `now` falls in. See the module docstring."""
        return int(now // window_seconds) * window_seconds

    def _sweep(self, now):
        stale = [k for k, (_, reset_at) in self._counts.items() if reset_at <= now]
        for key in stale:
            self._counts.pop(key, None)

    def incr(self, key, window_seconds, *, now=None):
        now = now if now is not None else self._clock()
        reset_at = self._window_start(now, window_seconds) + window_seconds
        with self._lock:
            self._calls += 1
            if self._calls % self.SWEEP_EVERY == 0:
                self._sweep(now)
            count, existing_reset = self._counts.get(key, (0, reset_at))
            if existing_reset <= now:
                # The window rolled over between this call and the last one.
                count, existing_reset = 0, reset_at
            count += 1
            self._counts[key] = (count, existing_reset)
            return count, existing_reset

    def peek(self, key, window_seconds, *, now=None):
        now = now if now is not None else self._clock()
        reset_at = self._window_start(now, window_seconds) + window_seconds
        with self._lock:
            count, existing_reset = self._counts.get(key, (0, reset_at))
            if existing_reset <= now:
                return 0, reset_at
            return count, existing_reset

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._counts.clear()
            else:
                self._counts.pop(key, None)


def build_backend(config):
    """Pick a backend from configuration.

    `redis` is named and refused rather than silently ignored. An operator who
    sets `RATELIMIT_BACKEND=redis` has decided they want a shared limiter, and
    falling back to memory would give them a limit that does not span the workers
    they configured it for — which is precisely the failure they were trying to
    avoid, arriving silently.
    """
    name = (config.get('RATELIMIT_BACKEND') or 'memory').strip().lower()
    if name == 'memory':
        return MemoryBackend()
    if name == 'redis':
        raise NotImplementedError(
            'RATELIMIT_BACKEND=redis is not implemented yet. The interface is '
            'RateLimitBackend (dough/services/ratelimit.py) — implement incr() '
            'and peek() against Redis and register it here. Until then, use '
            'memory and read SEC-0010 for what that does not cover.')
    raise ValueError(
        f'Unknown RATELIMIT_BACKEND {name!r}; expected memory or redis.')


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------

class Limiter:
    """What call sites use. Holds a backend and the policy table.

    Installed on `app.extensions['dough_ratelimit']` by `init_app`, per
    application rather than as a module global — the same reasoning as the login
    throttle's: the suite builds many applications in one process, and a shared
    counter would let one test exhaust another's allowance.
    """

    def __init__(self, backend, *, enabled=True):
        self.backend = backend
        self.enabled = enabled

    @classmethod
    def init_app(cls, app, backend=None):
        limiter = cls(backend or build_backend(app.config),
                      enabled=app.config.get('RATELIMIT_ENABLED', True))
        app.extensions['dough_ratelimit'] = limiter
        return limiter

    @staticmethod
    def _key(policy, identity):
        # The policy name is part of the key so two policies over the same
        # identity -- `ai` and `ai_daily` over one household -- cannot share a
        # counter. Without it the daily budget would be spent by the hourly one.
        return f'{policy.name}:{identity}'

    def check(self, policy_name, identity, *, now=None):
        """Spend one and say whether it was allowed.

        Spending on refusal as well as on success is deliberate: an attacker
        who is being refused is still making requests, and a limiter that stops
        counting once the limit is reached lets a caller sit exactly at the
        boundary forever, refreshing into the next window the instant it opens.
        """
        policy = policy_for(policy_name)
        if not self.enabled:
            return Decision(True, policy.name, policy.limit, None, 0)

        count, reset_at = self.backend.incr(
            self._key(policy, identity), policy.window_seconds, now=now)
        now = now if now is not None else time.time()
        remaining = max(0, policy.limit - count)
        allowed = count <= policy.limit
        return Decision(allowed, policy.name, remaining, reset_at,
                        0 if allowed else max(1, int(reset_at - now)))

    def peek(self, policy_name, identity, *, now=None):
        """The same answer, without spending. For rendering a usage figure."""
        policy = policy_for(policy_name)
        if not self.enabled:
            return Decision(True, policy.name, policy.limit, None, 0)

        count, reset_at = self.backend.peek(
            self._key(policy, identity), policy.window_seconds, now=now)
        now = now if now is not None else time.time()
        remaining = max(0, policy.limit - count)
        return Decision(count < policy.limit, policy.name, remaining, reset_at,
                        0 if count < policy.limit else max(1, int(reset_at - now)))

    def reset(self, policy_name=None, identity=None):
        """Clear a counter. Called after a *successful* authentication, and by tests.

        Mirrors `LoginThrottle.record_success`: succeeding clears the bucket, so
        a legitimate user is not held to a limit they filled by mistyping.
        """
        if policy_name is None:
            self.backend.reset()
            return
        self.backend.reset(self._key(policy_for(policy_name), identity))


def current_limiter():
    """The limiter this application installed.

    Mirrors `current_ai()` and `current_email()`. A missing entry is a wiring
    bug: building a default here would silently give a route its own private
    counter, which is a limit that never fires.
    """
    limiter = current_app.extensions.get('dough_ratelimit')
    if limiter is None:
        raise RuntimeError(
            'No rate limiter installed. Limiter.init_app(app) must run in '
            'create_app().')
    return limiter
