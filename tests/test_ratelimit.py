"""The rate-limit abstraction: the policies, the backend, and the seam.
[Phase 10.5 — SEC-0018]

What this file is for, and what it deliberately is not:

- It asserts on the **declared policies**, so a limit loosened by accident fails
  a test rather than surfacing in a bill. That is only possible because
  `POLICIES` is a table rather than numbers scattered across five call sites.
- It asserts the **backend contract**, so a Redis implementation added later has
  something to be correct against. `MemoryBackend` is the only implementation
  today; the tests are written against `RateLimitBackend`'s two methods.
- It does **not** assert that any particular route is limited. Those live with
  the routes (`tests/test_identity.py`), because "is `/forgot-password` limited"
  is a question about that route and "does a limiter work" is a question about
  this module.

`RATELIMIT_ENABLED` is False under TestingConfig so the ~890 tests that predate
this phase can make as many requests as they like. Every test here that needs
the limiter live turns it on explicitly, which is the only way it is exercised
at all.
"""

import pytest

from dough.services.ratelimit import (Limiter, MemoryBackend, POLICIES,
                                      build_backend, policy_for)


class FakeClock:
    """A hand-cranked clock, so no test sleeps.

    A limiter is defined entirely in terms of elapsed time, so testing it
    against the real clock means either sleeping for the window (a 3600-second
    test) or shrinking the window until it no longer resembles the thing being
    tested. Injecting the clock is what makes the window boundary itself
    assertable.
    """

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------------------
# The policies
# ---------------------------------------------------------------------------

def test_every_policy_declares_a_limit_a_window_and_a_scope():
    for name, policy in POLICIES.items():
        assert policy.name == name, 'the table key must match the policy name'
        assert policy.limit > 0
        assert policy.window_seconds > 0
        assert policy.per, f'{name} does not say what its key identifies'
        assert policy.description, f'{name} has no stated reason'


def test_authentication_policies_are_stricter_than_api_policies():
    """The tiering, asserted as a relationship rather than as magic numbers.

    Comparing the tiers is what stays true when somebody legitimately retunes a
    number. Pinning `password_reset.limit == 5` would fail on every honest
    adjustment and say nothing about whether the *shape* is still right — which
    is the only thing worth defending here: an auth limit as loose as an API
    limit is an auth limit that has stopped working.
    """
    assert policy_for('password_reset').limit < policy_for('api').limit
    assert policy_for('register').limit < policy_for('api').limit
    assert policy_for('ai').limit < policy_for('api').limit
    # Writes cost a database write on a single-writer SQLite file, so they are
    # held below general traffic.
    assert policy_for('api_write').limit < policy_for('api').limit


def test_the_ai_daily_budget_is_below_a_full_day_of_the_hourly_one():
    """An hourly limit alone permits 24x its own number.

    That is the bill this policy is actually about. If the daily ceiling ever
    rises above what the hourly one already allows, it has stopped being a
    ceiling and is just a larger number sitting next to a smaller one.
    """
    hourly, daily = policy_for('ai'), policy_for('ai_daily')
    assert daily.window_seconds == 86400
    assert daily.limit < hourly.limit * (86400 / hourly.window_seconds)


def test_ai_and_api_are_scoped_to_different_things():
    """Cost-aware means per household; process-aware means per token.

    They are not interchangeable. A per-token AI budget lets one household spend
    without limit by issuing tokens; a per-household API limit throttles the
    person who owns a misbehaving client along with the client.
    """
    assert policy_for('ai').per == 'household'
    assert policy_for('ai_daily').per == 'household'
    assert policy_for('api').per == 'token'
    assert policy_for('api_write').per == 'token'


def test_an_unknown_policy_raises_rather_than_defaulting():
    """A typo'd name must not become an unlimited route.

    Defaulting to something permissive is the failure this whole module exists
    to prevent, and it is the one nothing downstream would report.
    """
    with pytest.raises(KeyError, match='Unknown rate-limit policy'):
        policy_for('no_such_policy')


#: Policies that have a call site today. The rest are declared for SEC-0018's
#: sake and are not enforced yet — see the `POLICIES` docstring for why that is
#: a decision about the *backend* rather than an unfinished wiring job.
ENFORCED = {'register', 'password_reset', 'password_reset_account',
            'email_verification'}


def test_the_declared_policies_match_their_call_sites():
    """Which policies actually limit something, pinned as data.

    Two failures this catches, in opposite directions and both silent:

    - A policy in `ENFORCED` that loses its call site is a limit the security
      documentation claims and the application no longer applies.
    - A policy outside it that gains one is a limit now being enforced by a
      per-process, restart-clearing backend on a surface where SEC-0018 says
      that is not good enough — which is a decision, not something to discover
      later from a graph.

    Matched against the *call*, not against the bare string. `'ai'` and `'api'`
    are ordinary words that appear all over this package — in paths, in adapter
    names, in `dough/ai/` — so a plain search for the quoted policy name reports
    every one of them as enforced, which is the reassuring direction to be wrong
    in.
    """
    import ast
    import os

    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dough')
    declarations = os.path.join(root, 'services', 'ratelimit.py')

    #: Functions whose first argument is a policy name.
    limiter_calls = {'check', 'peek', 'reset', '_limited'}

    named = set()
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            path = os.path.join(dirpath, filename)
            if not filename.endswith('.py') or path == declarations:
                continue
            with open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(
                    func, 'id', '')
                if name not in limiter_calls:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value in POLICIES:
                    named.add(first.value)

    assert named == ENFORCED, (
        f'policies with call sites are {sorted(named)}, expected {sorted(ENFORCED)}.\n'
        'If a policy was deliberately wired or unwired, update ENFORCED here '
        'and the POLICIES docstring, and say so in docs/security.md SEC-0018.')


def test_login_is_deliberately_absent_from_the_table():
    """`dough.auth.LoginThrottle` owns it, and does something this cannot.

    Two buckets over one attempt — one keyed on address, one on account, with
    different windows and a deliberate difference in what each is allowed to
    *say*. Folding it into a generic policy would lose the part that matters:
    an address lock may explain itself, an account lock may not, because saying
    anything specific confirms the username exists.
    """
    assert 'login' not in POLICIES


# ---------------------------------------------------------------------------
# The backend contract
# ---------------------------------------------------------------------------

def test_incr_counts_within_a_window():
    clock = FakeClock()
    backend = MemoryBackend(clock=clock)

    counts = [backend.incr('k', 60)[0] for _ in range(3)]
    assert counts == [1, 2, 3]


def test_peek_does_not_spend():
    """A page rendering "23 of 60 used" must not consume one by asking."""
    backend = MemoryBackend(clock=FakeClock())
    backend.incr('k', 60)

    assert backend.peek('k', 60)[0] == 1
    assert backend.peek('k', 60)[0] == 1
    assert backend.incr('k', 60)[0] == 2


def test_the_count_resets_when_the_window_rolls_over():
    clock = FakeClock()
    backend = MemoryBackend(clock=clock)
    backend.incr('k', 60)
    backend.incr('k', 60)

    clock.advance(61)
    assert backend.incr('k', 60)[0] == 1


def test_keys_do_not_share_a_counter():
    backend = MemoryBackend(clock=FakeClock())
    backend.incr('a', 60)
    backend.incr('a', 60)
    assert backend.incr('b', 60)[0] == 1


def test_reset_clears_one_key_or_everything():
    backend = MemoryBackend(clock=FakeClock())
    backend.incr('a', 60)
    backend.incr('b', 60)

    backend.reset('a')
    assert backend.peek('a', 60)[0] == 0
    assert backend.peek('b', 60)[0] == 1

    backend.reset()
    assert backend.peek('b', 60)[0] == 0


def test_expired_entries_are_swept_so_the_dict_stays_bounded():
    """Memory is the one resource this backend can exhaust.

    Sweeping on a counter rather than on a timer, because a timer means a
    thread. The assertion is that the dict does not grow with the number of
    distinct keys ever seen — only with the number seen inside one window.
    """
    clock = FakeClock()
    backend = MemoryBackend(clock=clock)

    for i in range(MemoryBackend.SWEEP_EVERY):
        backend.incr(f'key-{i}', 60)
    clock.advance(61)
    # The call that crosses the sweep boundary clears everything expired.
    for i in range(MemoryBackend.SWEEP_EVERY):
        backend.incr('live', 60)

    assert len(backend._counts) < MemoryBackend.SWEEP_EVERY


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------

def test_a_decision_refuses_once_the_limit_is_passed():
    clock = FakeClock()
    limiter = Limiter(MemoryBackend(clock=clock))
    limit = policy_for('password_reset').limit

    for _ in range(limit):
        assert limiter.check('password_reset', '1.2.3.4').allowed

    refused = limiter.check('password_reset', '1.2.3.4')
    assert not refused.allowed
    assert refused.remaining == 0
    assert refused.retry_after > 0, (
        'a refusal that does not say when to come back produces clients that poll')


def test_refused_attempts_still_count():
    """Otherwise a caller sits at the boundary forever.

    A limiter that stops counting once the limit is reached lets somebody
    refresh into the next window the instant it opens, at exactly the rate the
    limit was supposed to prevent.
    """
    clock = FakeClock()
    backend = MemoryBackend(clock=clock)
    limiter = Limiter(backend)
    limit = policy_for('register').limit

    for _ in range(limit + 5):
        limiter.check('register', '1.2.3.4')

    assert backend.peek('register:1.2.3.4', 3600)[0] == limit + 5


def test_two_policies_over_one_identity_do_not_share_a_counter():
    """The daily AI budget must not be spent by the hourly one.

    The policy name is part of the key for exactly this reason — without it,
    sixty hourly calls would exhaust a three-hundred-call daily ceiling.
    """
    clock = FakeClock()
    limiter = Limiter(MemoryBackend(clock=clock))

    for _ in range(policy_for('ai').limit):
        limiter.check('ai', 'household:1')

    assert not limiter.check('ai', 'household:1').allowed
    assert limiter.check('ai_daily', 'household:1').allowed


def test_two_identities_under_one_policy_do_not_share_a_counter():
    """One household must not be able to exhaust another's AI budget."""
    limiter = Limiter(MemoryBackend(clock=FakeClock()))

    for _ in range(policy_for('ai').limit):
        limiter.check('ai', 'household:1')

    assert not limiter.check('ai', 'household:1').allowed
    assert limiter.check('ai', 'household:2').allowed


def test_a_disabled_limiter_allows_everything_and_says_so():
    limiter = Limiter(MemoryBackend(clock=FakeClock()), enabled=False)
    for _ in range(policy_for('password_reset').limit * 10):
        decision = limiter.check('password_reset', '1.2.3.4')
        assert decision.allowed
        assert decision.retry_after == 0


def test_reset_clears_a_bucket_after_a_success():
    """Mirrors `LoginThrottle.record_success`: succeeding ends the lockout.

    A legitimate user must not be held to a limit they filled by mistyping.
    """
    limiter = Limiter(MemoryBackend(clock=FakeClock()))
    for _ in range(policy_for('password_reset').limit + 1):
        limiter.check('password_reset', '1.2.3.4')
    assert not limiter.check('password_reset', '1.2.3.4').allowed

    limiter.reset('password_reset', '1.2.3.4')
    assert limiter.check('password_reset', '1.2.3.4').allowed


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_the_memory_backend_is_honest_about_what_it_is():
    """SEC-0010: per process, cleared on restart. Not hidden behind a name."""
    assert build_backend({'RATELIMIT_BACKEND': 'memory'}).name == 'memory'


def test_redis_is_named_and_refused_rather_than_silently_ignored():
    """An operator who sets this has decided they want a shared limiter.

    Falling back to memory would give them a limit that does not span the
    workers they configured it for — precisely the failure they were trying to
    avoid, arriving silently. The message names the interface to implement.
    """
    with pytest.raises(NotImplementedError) as excinfo:
        build_backend({'RATELIMIT_BACKEND': 'redis'})
    assert 'RateLimitBackend' in str(excinfo.value)
    assert 'incr' in str(excinfo.value)


def test_an_unknown_backend_name_raises():
    with pytest.raises(ValueError, match='Unknown RATELIMIT_BACKEND'):
        build_backend({'RATELIMIT_BACKEND': 'memcached'})


def test_the_backend_interface_is_what_a_redis_implementation_must_satisfy():
    """Two methods and a reset. Nothing assumes a lock, a process, or a dict.

    Written as an assertion rather than left to a docstring so that a third
    method added to `MemoryBackend` and used from `Limiter` — which would make
    the interface no longer implementable against Redis — fails here.
    """
    from dough.services.ratelimit import RateLimitBackend

    contract = {name for name in vars(RateLimitBackend) if not name.startswith('_')}
    assert contract == {'name', 'incr', 'peek', 'reset'}


def test_the_limiter_is_installed_per_application(app):
    """Not a module global — the suite builds many applications in one process.

    A shared counter would let one test exhaust another's allowance, which is
    the same reasoning that puts `LoginThrottle` on `app.extensions`.
    """
    from dough.services.ratelimit import current_limiter

    with app.test_request_context('/'):
        assert current_limiter() is app.extensions['dough_ratelimit']
