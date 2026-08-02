"""Access to the current household's `CategoryRules` engine.

## What this module used to be, and why it changed

It held **one `CategoryRules` per process**, backed by a single
`category_rules.json` at the repo root. That was a faithful extraction of what
`create_app()` used to close over, and its docstring said so plainly: "the rules
have always been backed by a single `category_rules.json`, so the persisted
state was already process-wide."

Process-wide was the bug. With households, one rule set for the whole
installation means the second family to sign in reads the first family's rules —
their gym, their student-loan servicer, their subscriptions — and any edit they
make rewrites them. Phase 11A.1 moved the rules into `models.CategoryRule`, one
tenant-scoped row per (category, keyword), with
`dough/services/rules_service.py` owning the reads and writes.

So there is **no cache here any more**, and that is deliberate. A cached engine
is a cached *household*, and the one thing this module must not do is hand one
household's rules to another. `rules_service.as_engine()` issues one indexed
query against a table holding at most a few hundred rows per household; caching
it would trade a real isolation guarantee for a saving nobody measured.

`reset_category_rules()` is kept as a no-op rather than removed. Its callers'
intent — "forget any cached rules" — is now satisfied by there being no cache,
and deleting the name would fail those callers for a reason unrelated to what
they are testing.

## What must not change

`finance_sync/repository.py` builds its rules per sync rather than holding one,
so a rule edited in the Rules page applies to the next sync without a restart.
That is still true and still important. It now resolves them per sync inside the
household's scope, which additionally means a sync categorises with *that
household's* rules rather than whichever set was loaded at boot.

Allowed:   `rules`, sibling services, models, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from dough.services.rules_service import as_engine


def get_category_rules():
    """The current household's rules, as a `CategoryRules` engine.

    Requires a tenant scope, and raising without one is the point: a call from
    outside a household is a call that cannot know whose rules it wants.
    """
    return as_engine()


def reset_category_rules():
    """Kept for callers that expect a cache to clear. There is no cache now.

    See the module docstring. A no-op rather than a removal, so that a caller
    expressing "start from clean rules" keeps working while testing what it
    meant to test.
    """
    return None


__all__ = ['get_category_rules', 'reset_category_rules']
