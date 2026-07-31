"""One way to page, sort and filter a collection. The same way, every time.

Allowed:   flask, sqlalchemy, dough.api.errors, stdlib
Must not:  models, dough.services, dough.blueprints, app

## Why this is a module and not four lines in each resource

The existing routes each invented their own. `/transactions` reads `page` and
`per_page` and silently rewrites `per_page` to 50 unless it is one of four
values it happens to like. `/api/log/entries` returns everything, unpaged,
forever. `/api/conversations` returns everything, unpaged, ordered by a column
the caller cannot change. None is wrong; no two are the same.

For a page written next to its route that is a non-issue -- the page knows. For
a client on another release schedule it is nine separate contracts to learn and
nine places for a mistake to be endpoint-specific. So every collection under
`/api/v1` takes the same four parameters and answers with the same pagination
block, and a client that can page transactions can page anything.

## The parameters

    ?page=1&page_size=50&sort=date&order=desc

`page` is 1-based, because that is what the `page` in a URL means to everybody
who is not writing the loop. `page_size` is bounded by `MAX_PAGE_SIZE`, and the
bound is enforced by clamping rather than by refusing: a client asking for 5,000
rows has made a judgement about its own memory, not an error, and a 422 there
would be this API being pedantic at the one moment a caller is trying to be
efficient. An unparseable value *is* refused, because that is a client bug and
silently substituting a default hides it.

`sort` is validated against a per-resource allow-list, never interpolated. It
reaches a SQL `ORDER BY`, so an unchecked value is an injection surface; an
allow-list of column objects means the request never carries anything but a key
into a dictionary the route wrote.

## Filtering

Filters are per-resource and declared by the resource, because "which fields can
you filter transactions on" is a domain question and does not generalize. What
*is* standardized is the spelling: `?category=Dining`, `?date_from=`/`?date_to=`
for ranges, `?q=` for free text. `docs/api/README.md` states the convention so a
resource added later matches it without having to guess.
"""

from __future__ import annotations

from datetime import datetime

from flask import request

from dough.api.errors import ValidationError

__all__ = [
    'DEFAULT_PAGE_SIZE',
    'MAX_PAGE_SIZE',
    'PageRequest',
    'apply_ordering',
    'date_arg',
    'int_arg',
    'page_request',
    'str_arg',
]

#: What a caller gets when it does not say. Fifty rows is roughly one screen of
#: transactions on a phone and two on a desktop, which is the size that makes a
#: naive client that never asks for page 2 still look basically correct.
DEFAULT_PAGE_SIZE = 50

#: The ceiling. Two hundred rows of transactions is about 40KB of JSON, which is
#: a reasonable worst case for a mobile connection to be asked to hold. Callers
#: wanting the whole ledger want `/api/v1/transactions/export`, which streams.
MAX_PAGE_SIZE = 200


class PageRequest:
    """A parsed, validated page/sort request.

    Carries the resolved values rather than the raw arguments, so a route cannot
    accidentally read `request.args['page']` again and get the unvalidated one.
    """

    __slots__ = ('page', 'page_size', 'sort', 'order')

    def __init__(self, page, page_size, sort, order):
        self.page = page
        self.page_size = page_size
        self.sort = sort
        self.order = order

    @property
    def offset(self):
        return (self.page - 1) * self.page_size

    def __repr__(self):
        return (f'PageRequest(page={self.page}, page_size={self.page_size}, '
                f'sort={self.sort!r}, order={self.order!r})')


def page_request(*, sortable, default_sort, default_order='desc'):
    """Parse `page`, `page_size`, `sort` and `order` from the query string.

    `sortable` is the resource's allow-list -- a mapping of public field name to
    the column to order by. Public name, not column name: `?sort=account` orders
    by `Transaction.account_name`, and the client is not made to know that the
    column was named before anybody thought about an API.
    """
    page = int_arg('page', default=1, minimum=1)
    page_size = int_arg('page_size', default=DEFAULT_PAGE_SIZE, minimum=1)
    # Clamped, not refused -- see the module docstring. The distinction is
    # between a caller making a choice we will not honour in full and a caller
    # sending something that is not a number.
    page_size = min(page_size, MAX_PAGE_SIZE)

    sort = (request.args.get('sort') or default_sort).strip()
    if sort not in sortable:
        raise ValidationError(
            f'Cannot sort by {sort!r}.',
            details={'sort': f'Expected one of: {", ".join(sorted(sortable))}.'})

    order = (request.args.get('order') or default_order).strip().lower()
    if order not in ('asc', 'desc'):
        raise ValidationError(
            f'Unknown sort order {order!r}.',
            details={'order': "Expected 'asc' or 'desc'."})

    return PageRequest(page, page_size, sort, order)


def apply_ordering(query, page, sortable):
    """Order `query` per the request, with the primary key as a tiebreak.

    The tiebreak is not cosmetic. Paging an unstable sort is the classic
    silently-wrong result: two transactions on the same date have no defined
    order, so the database may return them differently for page 1 and page 2,
    and a row is duplicated on one page and missing from the other. Nothing
    fails; the client simply shows the wrong ledger. Appending a unique column
    makes the total order deterministic, which is what makes paging correct
    rather than merely functional.
    """
    column = sortable[page.sort]
    ordered = column.asc() if page.order == 'asc' else column.desc()
    entity = _entity_of(query)
    tiebreak = getattr(entity, 'id', None) if entity is not None else None
    if tiebreak is None or tiebreak is column:
        return query.order_by(ordered)
    return query.order_by(ordered, tiebreak.desc())


def _entity_of(query):
    """The model a query selects from, or None if it is not a simple select."""
    try:
        descriptions = query.column_descriptions
    except AttributeError:
        return None
    for description in descriptions:
        entity = description.get('entity')
        if entity is not None:
            return entity
    return None


# ---------------------------------------------------------------------------
# Query-string coercion
#
# Every one of these refuses rather than substituting a default when the value
# is present and unparseable. `?page=abc` is a client bug, and a silent fallback
# to page 1 turns it into "the API keeps returning the first page", which is
# reported as a server problem and investigated in the wrong place entirely.
# ---------------------------------------------------------------------------

def int_arg(name, *, default=None, minimum=None, maximum=None):
    raw = request.args.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f'{name} must be a whole number.',
                              details={name: f'Got {raw!r}.'})
    if minimum is not None and value < minimum:
        raise ValidationError(f'{name} must be at least {minimum}.',
                              details={name: f'Got {value}.'})
    if maximum is not None and value > maximum:
        raise ValidationError(f'{name} must be at most {maximum}.',
                              details={name: f'Got {value}.'})
    return value


def str_arg(name, *, default=None, choices=None, max_length=None):
    raw = request.args.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        # An explicitly empty value means "no filter", which is not the same as
        # the parameter being absent for a caller that builds its query string
        # by concatenation. Both land on the default.
        return default
    if choices and value not in choices:
        raise ValidationError(
            f'Unknown {name} {value!r}.',
            details={name: f'Expected one of: {", ".join(sorted(choices))}.'})
    if max_length and len(value) > max_length:
        value = value[:max_length]
    return value


def date_arg(name, *, default=None):
    """An ISO `YYYY-MM-DD` query argument, as a `date`.

    One format, and it is the unambiguous one. Accepting `MM/DD/YYYY` as well
    would mean this API silently disagrees with itself about `03/04/2026`
    depending on which client sent it, which is the kind of defect that shows up
    as a spending report being wrong by one month.
    """
    raw = request.args.get(name)
    if not raw:
        return default
    try:
        return datetime.strptime(raw.strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValidationError(f'{name} must be a date as YYYY-MM-DD.',
                              details={name: f'Got {raw!r}.'})
