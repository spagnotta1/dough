"""One response shape, for every endpoint under `/api/v1`.

Allowed:   flask, dough.logging, stdlib
Must not:  models, dough.services, dough.blueprints, app

## Why an envelope at all

The endpoints that predate this phase return whatever shape was convenient at
the time. `/api/holdings` returns a bare object, `/api/log/entries` a bare
array, `/update_category` returns `{'success': ...}` with **200 on failure**,
and the three AI surfaces return `{'available': false}` for what are really five
different conditions. Each is reasonable alone. Together they mean a client
cannot write one function that says "did this work, and if not, why" — it needs
a branch per endpoint, and every new endpoint is a new branch.

That is survivable when the only client is a page that was written alongside the
route. It stops being survivable the moment a second client exists that ships on
someone else's release schedule, which is the thing this phase is for.

So: two shapes, and only two.

    {"success": true,  "data": ..., "meta": {...}}
    {"success": false, "error": {"code", "message", "details"}, "meta": {...}}

`success` is redundant with the HTTP status and is here anyway. Clients do get
written against `response.data.success` before anyone reads the status code, and
a redundant boolean costs nothing next to the class of bug where a proxy turns a
502 into something with a 200 on it.

## What `meta` is for

`request_id` is the same value `dough/logging.py` puts on every log line for the
request and returns in `X-Request-ID`. That is the entire point of emitting it
in the body too: a person reporting a problem can read it off a screen, and it
resolves to the exact log lines. The header alone is invisible to anyone not
holding a debugger.

`api_version` is in every response so a client can assert on it. A client that
silently talks to a version it was not built for is the failure this phase is
supposed to make impossible, and the cheapest way to catch it is for the version
to be present in something the client already parses.

## Never `jsonify(model)`

Every function here takes data the caller has already shaped. Serialization
belongs to the resource module, which knows what its contract promised;
reflecting a model would make every column addition a silent, unreviewed change
to the public API.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import jsonify

from dough.logging import current_trace_id

__all__ = [
    'API_VERSION',
    'created',
    'error_body',
    'meta',
    'no_content',
    'ok',
    'pagination_meta',
]

#: The version this module speaks. One constant rather than a literal in each
#: response, so `/api/v2` is a second module with a second value rather than a
#: search-and-replace across every route.
API_VERSION = 'v1'


def meta(**extra):
    """The `meta` block every response carries.

    `request_id` may be None outside a request context -- a service calling this
    directly in a test, say. It is omitted rather than emitted as null, because
    a client checking `if (meta.request_id)` should not have to also check for
    the string "None".
    """
    block = {
        'api_version': API_VERSION,
        'timestamp': datetime.now(timezone.utc).isoformat(
            timespec='milliseconds').replace('+00:00', 'Z'),
    }
    trace = current_trace_id()
    if trace:
        block['request_id'] = trace
    block.update(extra)
    return block


def pagination_meta(page, page_size, total):
    """The pagination block, for collection endpoints.

    Lives in `meta` rather than wrapping `data`, so `data` is always the
    resource and never a container that a client has to unwrap differently
    depending on whether the endpoint happened to paginate.

    `has_next` and `has_prev` are computed here rather than left to the client.
    Every client would otherwise reimplement `page * page_size < total`, and the
    off-by-one in that expression is not hypothetical -- it is the reason the
    last page of a list silently repeats on some clients and not others.
    """
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return {
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_prev': page > 1,
    }


def ok(data, status=200, **meta_extra):
    """A success response. `data` is whatever the resource promised."""
    return jsonify({'success': True, 'data': data,
                    'meta': meta(**meta_extra)}), status


def created(data, location=None, **meta_extra):
    """201, with `Location` when the new resource has a URL worth naming.

    Returning the created entity in the body as well as its location is
    deliberate: a client that has just POSTed a transaction wants the server's
    id, its normalized amount and its assigned category without a second round
    trip, and on a mobile connection that round trip is the expensive part.
    """
    response, status = ok(data, status=201, **meta_extra)
    if location:
        response.headers['Location'] = location
    return response, status


def no_content():
    """204 for a successful delete.

    The one place the envelope is deliberately absent, because 204 means there
    is no body and a body containing `{"success": true}` would be a 200 wearing
    the wrong number. Clients are told this in `docs/api/README.md` rather than
    left to discover that one endpoint parses differently.
    """
    return '', 204


def error_body(code, message, details=None):
    """The `error` shape, without the response wrapper.

    Separated so `dough/api/errors.py` can build a body and choose its own
    status, and so a test can assert on the shape without a response object.
    """
    error = {'code': code, 'message': message}
    if details:
        error['details'] = details
    return {'success': False, 'error': error, 'meta': meta()}
