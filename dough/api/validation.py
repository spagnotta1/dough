"""Reading a JSON body without trusting any of it.

Allowed:   flask, dough.api.errors, stdlib
Must not:  models, dough.services, dough.blueprints, app

## Why not a schema library

`pydantic` or `marshmallow` would do this and more. Neither is added, for two
reasons that are about this codebase rather than about those libraries.

The first is that the validation surface here is small and shallow -- nine
resources, mostly flat objects of strings, numbers and dates -- and a dependency
earns its place by removing more code than it adds. The second matters more: the
error shape is already fixed by `dough/api/errors.py`, and both libraries would
need an adapter translating their error structures into `details`. That adapter
is most of what is in this file, so the library would buy the declarations and
leave the awkward part behind.

If the surface grows past what this file can hold clearly, that is the signal to
revisit -- and this note is here so the decision gets reopened rather than
inherited.

## Absent is not empty

Every helper distinguishes three states, and PATCH semantics depend on it:

    field absent      -> leave the stored value alone
    field present, null -> clear it (where the column is nullable)
    field present, value -> set it

`required()` raises when absent. `optional()` returns the `MISSING` sentinel,
which is not `None`, because `None` is a value a caller can legitimately send.
A route that treated them the same would make it impossible to clear a note,
or -- worse, and this is the bug this design is written against -- would blank
every field a PATCH did not mention.
"""

from __future__ import annotations

from datetime import datetime

from flask import request

from dough.api.errors import BadRequest, ValidationError

__all__ = [
    'MISSING',
    'body',
    'optional_bool',
    'optional_date',
    'optional_number',
    'optional_str',
    'require_bool',
    'require_date',
    'require_number',
    'require_str',
]


class _Missing:
    """The absence of a key, distinct from a null value. See the module docstring."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return 'MISSING'


MISSING = _Missing()


def body():
    """The request's JSON object, or refuse.

    `silent=True` rather than `force=True`. The existing routes use
    `get_json(force=True)`, which parses a body regardless of its Content-Type
    and raises a raw werkzeug 400 with an HTML body when it cannot -- the one
    response shape a JSON client cannot read. Here a bad body is an envelope
    `bad_request`, which is the whole reason a client can have one error path.

    A body that parses to a list or a string is refused too. Every write
    endpoint in v1 takes an object, and `data['id']` against a list raises a
    TypeError several frames later, which surfaces as a 500 for what is a
    client error.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise BadRequest('Expected a JSON object in the request body.')
    if not isinstance(payload, dict):
        raise BadRequest(
            'Expected a JSON object in the request body.',
            details={'body': f'Got a {type(payload).__name__}.'})
    return payload


def _present(data, field):
    return field in data


def require_str(data, field, *, max_length=None, choices=None, allow_empty=False):
    if not _present(data, field):
        raise ValidationError(f'{field} is required.',
                              details={field: 'This field is required.'})
    return _coerce_str(data[field], field, max_length=max_length,
                       choices=choices, allow_empty=allow_empty)


def optional_str(data, field, *, max_length=None, choices=None,
                 allow_empty=True, allow_null=True):
    if not _present(data, field):
        return MISSING
    value = data[field]
    if value is None:
        if allow_null:
            return None
        raise ValidationError(f'{field} may not be null.',
                              details={field: 'This field may not be null.'})
    return _coerce_str(value, field, max_length=max_length, choices=choices,
                       allow_empty=allow_empty)


def _coerce_str(value, field, *, max_length, choices, allow_empty):
    if not isinstance(value, str):
        raise ValidationError(f'{field} must be text.',
                              details={field: f'Got a {type(value).__name__}.'})
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f'{field} may not be empty.',
                              details={field: 'This field may not be empty.'})
    if choices and value not in choices:
        raise ValidationError(
            f'Unknown {field} {value!r}.',
            details={field: f'Expected one of: {", ".join(sorted(choices))}.'})
    if max_length and len(value) > max_length:
        # Truncated rather than refused. These are labels and descriptions, and
        # a client that sent a 300-character merchant name wants the row stored,
        # not a 422 it has no way to explain to the person who typed it.
        value = value[:max_length]
    return value


def require_number(data, field, *, minimum=None, maximum=None):
    if not _present(data, field):
        raise ValidationError(f'{field} is required.',
                              details={field: 'This field is required.'})
    return _coerce_number(data[field], field, minimum=minimum, maximum=maximum)


def optional_number(data, field, *, minimum=None, maximum=None, allow_null=True):
    if not _present(data, field):
        return MISSING
    value = data[field]
    if value is None:
        if allow_null:
            return None
        raise ValidationError(f'{field} may not be null.',
                              details={field: 'This field may not be null.'})
    return _coerce_number(value, field, minimum=minimum, maximum=maximum)


def _coerce_number(value, field, *, minimum, maximum):
    # `bool` is a subclass of `int` in Python, so `True` would otherwise arrive
    # as the amount 1. Rejected explicitly, because a client that sent a boolean
    # where money goes has a bug worth surfacing rather than rounding off.
    if isinstance(value, bool):
        raise ValidationError(f'{field} must be a number.',
                              details={field: 'Got a boolean.'})
    if isinstance(value, str):
        # Accepted deliberately. JSON has no decimal type, and a client that
        # cares about money -- which is every client of this application -- is
        # right to send "12.34" rather than a float that cannot represent it.
        try:
            value = float(value.strip())
        except (TypeError, ValueError):
            raise ValidationError(f'{field} must be a number.',
                                  details={field: f'Got {value!r}.'})
    if not isinstance(value, (int, float)):
        raise ValidationError(f'{field} must be a number.',
                              details={field: f'Got a {type(value).__name__}.'})
    value = float(value)
    if value != value or value in (float('inf'), float('-inf')):
        # NaN and the infinities survive `json.loads` and then poison every
        # aggregate they reach -- a single NaN amount makes a household's whole
        # net-worth figure NaN, silently and permanently.
        raise ValidationError(f'{field} must be a finite number.',
                              details={field: 'Got a non-finite value.'})
    if minimum is not None and value < minimum:
        raise ValidationError(f'{field} must be at least {minimum}.',
                              details={field: f'Got {value}.'})
    if maximum is not None and value > maximum:
        raise ValidationError(f'{field} must be at most {maximum}.',
                              details={field: f'Got {value}.'})
    return value


def require_bool(data, field):
    if not _present(data, field):
        raise ValidationError(f'{field} is required.',
                              details={field: 'This field is required.'})
    return _coerce_bool(data[field], field)


def optional_bool(data, field):
    if not _present(data, field):
        return MISSING
    return _coerce_bool(data[field], field)


def _coerce_bool(value, field):
    if isinstance(value, bool):
        return value
    # Strings are accepted because form-encoded clients and shell scripts have
    # no other way to say it. Anything outside the known spellings is refused
    # rather than treated as truthy -- `"false"` being true is a defect that
    # reads as correct code.
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes', 'on'):
            return True
        if lowered in ('false', '0', 'no', 'off'):
            return False
    raise ValidationError(f'{field} must be true or false.',
                          details={field: f'Got {value!r}.'})


def require_date(data, field):
    if not _present(data, field):
        raise ValidationError(f'{field} is required.',
                              details={field: 'This field is required.'})
    return _coerce_date(data[field], field)


def optional_date(data, field, *, allow_null=True):
    if not _present(data, field):
        return MISSING
    value = data[field]
    if value is None:
        if allow_null:
            return None
        raise ValidationError(f'{field} may not be null.',
                              details={field: 'This field may not be null.'})
    return _coerce_date(value, field)


def _coerce_date(value, field):
    """ISO `YYYY-MM-DD` only. See `pagination.date_arg` for why only one format."""
    if not isinstance(value, str):
        raise ValidationError(f'{field} must be a date as YYYY-MM-DD.',
                              details={field: f'Got a {type(value).__name__}.'})
    try:
        return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValidationError(f'{field} must be a date as YYYY-MM-DD.',
                              details={field: f'Got {value!r}.'})
