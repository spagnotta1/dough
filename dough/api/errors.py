"""What can go wrong, said the same way every time.

Allowed:   flask, werkzeug, dough.api.envelope, stdlib
Must not:  models, dough.services, dough.blueprints, app

## Two things a client needs, and they are not the same thing

A failure has to answer two different audiences at once, and the endpoints
predating this phase mostly answer neither cleanly:

- **Code**, for the client. `code` is a closed vocabulary from `ErrorCode`
  below. A client switches on it. It never changes wording, because wording is
  not something a client can depend on and a client that string-matches
  `"No question provided"` breaks the day somebody improves the sentence.
- **Message**, for the person. Written to be shown. It says what to do where
  there is something to do.

`details` carries the field-level breakdown for `validation_error` and nothing
else, so a form can highlight the offending input rather than showing one
sentence about a form with nine fields on it.

## Why the status code and the code are both here

They carry different amounts of information and the mapping is deliberately not
one-to-one in the direction that matters. Three distinct conditions answer 403 —
a CSRF failure, an insufficient token scope, and a non-owner attempting an
owner-only action — and a client should react differently to each: reload,
reissue the token with more scope, and tell the user to ask an owner. One status
cannot say that; one code can. So `status` is the coarse signal for anything
sitting between the client and this application, and `code` is the fine one.

## Fail closed on the message, not on the code

`ApiError.user_message` is always safe to show. `_unexpected` deliberately does
*not* put an exception's text in the body -- an exception message is written by
whoever was nearest the failure, often quoting a value, and the values here are
financial. The detail goes to the log with the trace id that is in the response.
"""

from __future__ import annotations

from flask import request
from werkzeug.exceptions import HTTPException

from dough.api.envelope import error_body

__all__ = [
    'ApiError',
    'Conflict',
    'ErrorCode',
    'Forbidden',
    'NotFound',
    'RateLimited',
    'ServiceUnavailable',
    'Unauthenticated',
    'ValidationError',
    'api_error_response',
    'install',
    'is_api_request',
]

#: The URL prefix that selects this error shape. A prefix rather than a
#: blueprint check because a 404 for an unrouted path belongs to no blueprint,
#: and `/api/v1/transctions` -- the typo a client will actually make -- has to
#: answer in the envelope or the client cannot parse the thing telling it about
#: its typo.
API_PREFIX = '/api/v1'


class ErrorCode:
    """Every machine-readable code this API emits.

    A class of constants rather than an enum, matching how `models.py` declares
    the audit event vocabulary. What matters is that the set is closed and
    written in one place: `tests/test_api_errors.py` asserts every code reachable
    from a route appears here, so a route inventing `'txn_bad'` in a string
    literal fails rather than quietly entering the contract.
    """

    #: The request was syntactically wrong -- unparseable JSON, a missing body.
    #: Distinct from `validation_error`: this one means we could not get as far
    #: as looking at the fields.
    BAD_REQUEST = 'bad_request'
    #: The request parsed and its fields are wrong. Always carries `details`.
    VALIDATION_ERROR = 'validation_error'
    #: No credential, or one that is not usable. 401.
    UNAUTHENTICATED = 'unauthenticated'
    #: A credential that is real but does not cover this. 403.
    FORBIDDEN = 'forbidden'
    #: Authenticated, but this token's scopes do not include what is needed.
    #: Separate from `forbidden` because the fix is different and mechanical:
    #: reissue the token with the scope named in `details`.
    INSUFFICIENT_SCOPE = 'insufficient_scope'
    #: A session-authenticated unsafe request arrived without a valid token.
    #: Never returned to a bearer client, which does not participate in CSRF.
    CSRF_FAILED = 'csrf_failed'
    #: No such resource, or not this household's. The two are the same answer
    #: on purpose -- see `dough.tenancy.find_owned`.
    NOT_FOUND = 'not_found'
    #: The request is valid but conflicts with the resource's state: editing a
    #: synchronized holding, a duplicate import.
    CONFLICT = 'conflict'
    #: Too many attempts. Carries `retry_after` in details where one is known.
    RATE_LIMITED = 'rate_limited'
    #: A dependency this endpoint needs is not configured or not answering --
    #: the AI provider, most often.
    SERVICE_UNAVAILABLE = 'service_unavailable'
    #: Anything unhandled. Never carries detail; the trace id in `meta` is how
    #: an operator joins it to the log line that does.
    INTERNAL_ERROR = 'internal_error'
    #: The method is not allowed on this path.
    METHOD_NOT_ALLOWED = 'method_not_allowed'
    #: The upload exceeded `MAX_CONTENT_LENGTH`.
    PAYLOAD_TOO_LARGE = 'payload_too_large'


#: Werkzeug raises these for conditions no application code sees. Mapping them
#: means a 405 from the router answers in the envelope like everything else,
#: rather than being the one response a client has to parse differently.
_HTTP_STATUS_CODES = {
    400: (ErrorCode.BAD_REQUEST, 'The request could not be understood.'),
    401: (ErrorCode.UNAUTHENTICATED, 'Authentication is required.'),
    403: (ErrorCode.FORBIDDEN, 'You do not have permission to do that.'),
    404: (ErrorCode.NOT_FOUND, 'No such resource.'),
    405: (ErrorCode.METHOD_NOT_ALLOWED,
          'That method is not allowed on this endpoint.'),
    409: (ErrorCode.CONFLICT, 'That conflicts with the current state.'),
    413: (ErrorCode.PAYLOAD_TOO_LARGE, 'The request body was too large.'),
    415: (ErrorCode.BAD_REQUEST, 'That content type is not supported.'),
    422: (ErrorCode.VALIDATION_ERROR, 'Some fields were not acceptable.'),
    429: (ErrorCode.RATE_LIMITED, 'Too many requests. Try again shortly.'),
    503: (ErrorCode.SERVICE_UNAVAILABLE,
          'That feature is temporarily unavailable.'),
}


def is_api_request():
    """Whether this request should be answered in the v1 envelope.

    Read by `app.py`'s error handlers, which serve both the HTML application and
    this API from the same registrations. Guarded against being called outside a
    request, because an error handler is exactly the place where the thing that
    failed may be the request context itself.
    """
    try:
        return request.path.startswith(API_PREFIX)
    except RuntimeError:
        return False


class ApiError(Exception):
    """A refusal with a code, a status, and a message written for a person.

    Raised by resource modules and by the services they call, caught by the
    handler `install()` registers. Raising rather than returning is what lets a
    validation helper five frames down refuse without every intermediate frame
    having to check and propagate a return value -- which is the pattern that
    produces the "it returned None and we carried on" class of bug.
    """

    status = 500
    code = ErrorCode.INTERNAL_ERROR
    message = 'Something went wrong.'

    def __init__(self, message=None, *, details=None, code=None, status=None):
        super().__init__(message or self.message)
        self.user_message = message or self.message
        self.details = details
        if code:
            self.code = code
        if status:
            self.status = status

    def to_response(self):
        return error_body(self.code, self.user_message, self.details), self.status


class ValidationError(ApiError):
    """422. One or more fields were unacceptable.

    422 rather than 400, and the distinction is worth keeping: 400 says the
    request was malformed, 422 says it was well-formed and wrong. A client can
    retry a 422 after fixing a field; a 400 usually means its request builder is
    broken. Collapsing them loses the only signal that distinguishes a user
    error from a client bug.
    """

    status = 422
    code = ErrorCode.VALIDATION_ERROR
    message = 'Some fields were not acceptable.'


class BadRequest(ApiError):
    status = 400
    code = ErrorCode.BAD_REQUEST
    message = 'The request could not be understood.'


class Unauthenticated(ApiError):
    status = 401
    code = ErrorCode.UNAUTHENTICATED
    message = 'Authentication is required.'


class Forbidden(ApiError):
    status = 403
    code = ErrorCode.FORBIDDEN
    message = 'You do not have permission to do that.'


class InsufficientScope(ApiError):
    status = 403
    code = ErrorCode.INSUFFICIENT_SCOPE
    message = 'This token does not carry the scope needed for that.'


class NotFound(ApiError):
    status = 404
    code = ErrorCode.NOT_FOUND
    message = 'No such resource.'


class Conflict(ApiError):
    status = 409
    code = ErrorCode.CONFLICT
    message = 'That conflicts with the current state of the resource.'


class RateLimited(ApiError):
    status = 429
    code = ErrorCode.RATE_LIMITED
    message = 'Too many requests. Try again shortly.'


class ServiceUnavailable(ApiError):
    status = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    message = 'That feature is temporarily unavailable.'


def api_error_response(status, code=None, message=None, details=None):
    """Build an envelope error response from a status code.

    The bridge `app.py` uses: its handlers already know the status and a message
    written for the HTML page, and this turns that into the API shape without
    the handlers needing to learn two vocabularies.
    """
    from flask import jsonify

    default_code, default_message = _HTTP_STATUS_CODES.get(
        status, (ErrorCode.INTERNAL_ERROR, 'Something went wrong.'))
    body = error_body(code or default_code, message or default_message, details)
    return jsonify(body), status


def install(app):
    """Register the handlers that own the `/api/v1` failure surface.

    Only `ApiError` is registered here. Everything else -- 404, 403, 413, the
    unhandled catch-all -- is already registered in `app.py` for the HTML
    application, and those handlers call `is_api_request()` to choose a shape.
    Registering a second set here would mean two places decide what a 404 is,
    and Flask's most-specific-wins resolution would make which one runs depend
    on registration order rather than on anything a reader could see.
    """

    @app.errorhandler(ApiError)
    def _api_error(error):
        # Logged at warning rather than error: an ApiError is the application
        # refusing on purpose. A 422 for a bad date is not an incident, and
        # logging it as one is how error logs become unreadable. The genuinely
        # unexpected path is app.py's catch-all, which logs with exc_info.
        app.logger.warning(
            'api refusal', extra={'api_code': error.code,
                                  'status': error.status})
        body, status = error.to_response()
        from flask import jsonify
        return jsonify(body), status

    @app.errorhandler(HTTPException)
    def _http_exception(error):
        """Werkzeug's own exceptions, in the envelope when they are ours.

        `abort(404)` from `dough.tenancy.get_owned` arrives here, as does a 405
        from the router. Anything not under `/api/v1` is handed straight back so
        the HTML application's handlers and Flask's defaults behave exactly as
        they did before this phase.
        """
        if not is_api_request():
            return error
        code, default_message = _HTTP_STATUS_CODES.get(
            error.code, (ErrorCode.INTERNAL_ERROR, 'Something went wrong.'))
        # `get_owned` raises NotFound with a description naming the model and
        # the id -- useful in a log, an information leak in a body, since it
        # confirms the shape of what was looked for. The generic message is
        # used for 404 regardless of what the raiser wrote.
        message = default_message if error.code == 404 else (
            getattr(error, 'description', None) or default_message)
        return api_error_response(error.code, code, message)
