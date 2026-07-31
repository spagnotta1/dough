"""The versioned HTTP API.  [Phase 10]

    dough/api/
      envelope.py    one response shape
      errors.py      one failure vocabulary
      pagination.py  one way to page and sort
      validation.py  one way to read a body
      guard.py       bearer authentication, and the hooks that defer to it
      v1/            the resources

## What this package is for

Everything before Phase 10 served one client, which was a set of Jinja templates
written alongside the routes they call. That client could know that
`/update_category` returns `{'success': true}` while `/api/holdings` returns a
bare object and `/api/log/entries` returns a bare array, because the same person
wrote both ends in the same afternoon.

A second client cannot know that. It ships on a different schedule, it cannot be
updated in lockstep, and every inconsistency becomes a permanent branch in its
code. So `/api/v1` is a contract: one envelope, one error vocabulary, one
pagination convention, and — the part that actually matters — the same service
layer underneath as the web UI, so the two cannot answer differently.

## What it is not

It is not a rewrite of the existing endpoints. `/api/holdings`, `/api/log/*`,
`/api/chat_stream` and the rest are unchanged and still serve the web UI. They
are the previous contract and they keep working; `docs/api/README.md` records
which of them `/api/v1` supersedes and what a client should move to. Breaking
them to make this package tidier would be this phase causing exactly the kind of
disruption it exists to prevent.

## Registration order matters, once

`install_guard` must run before `create_app` registers its authentication and
CSRF hooks, because both of those ask questions whose answer changes when a
bearer token is present. `register` may run whenever. See `guard.py`.
"""

from dough.api import errors, guard, v1

#: Re-exported so `app.py` reads as a list of decisions rather than a list of
#: import paths.
install_guard = guard.install
install_error_handlers = errors.install
is_api_request = errors.is_api_request
bearer_actor = guard.bearer_actor

__all__ = [
    'bearer_actor',
    'install_error_handlers',
    'install_guard',
    'is_api_request',
    'register',
]


def register(app):
    """Mount every served API version.

    One call rather than one per version, so adding `/api/v2` is a line here and
    a package beside `v1/` — and, importantly, so nothing can serve a version
    that this function does not name.
    """
    v1.register(app)
