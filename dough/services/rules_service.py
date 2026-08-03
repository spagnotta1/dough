"""Category rules, per household. [Phase 11A.1]

The storage half of what `rules.py` used to do alone. That module now owns
matching and holds no state; this one owns the rows, and every query it issues
goes through `CategoryRule.query`, which is `TenantScopedQuery`.

## What this fixes

`category_rules.json` was one file at the repo root shared by the whole
installation. No `household_id` existed anywhere in the rules path, so a second
household saw the first household's rules — not through a leaky filter, but
because there was only one rule set. A rule set names the merchants somebody
actually pays, so that is a disclosure of personal financial data.

## Why a fresh household gets nothing  [Phase 11A.2]

It used to get `rules.DEFAULT_RULES`, seeded on first read, and this module
claimed that set was "deliberately small and generic (five categories, no
merchant anybody could recognise as another user's)". That claim was false. The
list named the developer's credit union, student-loan servicer, broker, card
issuers, auto lender and employer, and every household that opened the Rules
page received a copy. A second account signed in and read the first account's
banks — which is the disclosure `20260802_08_category_rules` was written to
stop, surviving the fix because that revision moved the rows and left the
content alone.

So there is no seeding. A new household has no rules, and the on-ramp is
`/rules/ai-suggest`, which derives rules from the household's *own* transaction
descriptions. That is both safer and better: it cannot disclose anyone else's
merchants, and it proposes rules for the merchants this household actually has.

The empty default is also what lets "clear all" mean it. Nothing re-seeds, so a
household that clears its rules stays cleared without a marker column to
remember the intent.

## Ordering

`CategoryRule.position` is ascending and **lower wins**, matching the Rules
page's "rules higher in the list win". `as_engine()` returns the rules already
in that order, so `CategoryRules.get_category` — which takes the first match —
needs to know nothing about priority.

Allowed:   models, `rules`, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from sqlalchemy import func

from models import CategoryRule, db
from rules import CategoryRules


def all_rules():
    """This household's rules as `{category: [keyword, ...]}`, in priority order.

    Returns `{}` for a household that has none, which is the ordinary state of a
    new account — see the module docstring for why nothing is seeded into it.
    """
    rows = _ordered()

    grouped = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(row.keyword)
    return grouped


def as_engine():
    """A `CategoryRules` over this household's rules.

    The one function `finance_sync`, the importer and the Rules page should all
    call. Returning the engine rather than the dict keeps every caller on one
    implementation of matching.

    An engine over no rules is valid and answers `Uncategorized` for everything,
    which is the correct answer for a household that has not written a rule yet.
    """
    return CategoryRules(all_rules())


def categories():
    """Distinct category names, in priority order."""
    return list(all_rules().keys())


def clear_all():
    """Delete every rule this household has. Returns how many rows went.

    The whole-category `remove_category` repeated for all of them, and worth its
    own function because "start over" is a real intention: a household that
    inherited rules it did not write — or that grew a set it no longer trusts —
    should not have to delete them one category at a time.

    Nothing re-seeds afterwards. That is a property of `DEFAULT_RULES` being
    empty rather than something this function arranges, and it is the reason
    this can be a plain delete instead of a delete plus a marker recording that
    the household meant it.

    The caller is responsible for re-deriving categories afterwards; with no
    rules left, every transaction resolves to `Uncategorized`.
    """
    rows = CategoryRule.query.all()
    for row in rows:
        db.session.delete(row)
    if rows:
        db.session.commit()
    return len(rows)


def add_rule(category, keyword, *, first=False):
    """Add one keyword to a category. Returns the row, or None if it existed.

    `first` puts the new rule at the top of the priority order, which is what
    the AI-apply path wants: a suggestion the user just accepted should win over
    whatever was already miscategorising those transactions.
    """
    category = (category or '').strip()
    keyword = (keyword or '').strip()
    if not category or not keyword:
        return None

    existing = CategoryRule.query.filter_by(category=category,
                                            keyword=keyword).first()
    if existing is not None:
        return None

    if first:
        # Everything shifts down by one rather than the new row taking a
        # negative position: positions stay non-negative and dense, which keeps
        # `reorder` and any future "move up" operation simple to reason about.
        for row in CategoryRule.query.all():
            row.position += 1
        position = 0
    else:
        position = (_max_position() or 0) + 1

    row = CategoryRule(category=category, keyword=keyword, position=position)
    db.session.add(row)
    db.session.commit()
    return row


def remove_rule(category, keyword):
    """Delete one keyword from one category. Returns whether a row went.

    The boolean is what lets the route say "removed" only when something was.
    """
    row = CategoryRule.query.filter_by(category=category,
                                       keyword=keyword).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def remove_category(category):
    """Delete a category and every keyword in it. Returns how many rows went.

    **The operation the Rules page's "Delete category" button never performed.**
    It posted `action=remove` with a category and no keyword, the route called
    `remove_rule(category, None)`, that matched nothing, and the page then
    reported success. The button appeared to work and never did.
    """
    rows = CategoryRule.query.filter_by(category=category).all()
    for row in rows:
        db.session.delete(row)
    if rows:
        db.session.commit()
    return len(rows)


def rename_category(old, new):
    """Move every keyword from one category name to another. Returns the count.

    Merges rather than colliding when `new` already exists: a keyword that would
    duplicate an existing (category, keyword) pair is dropped instead of
    tripping the unique index, because a user renaming `Food` onto `Groceries`
    means "merge these" and should not get an IntegrityError.
    """
    new = (new or '').strip()
    if not new or new == old:
        return 0

    taken = {row.keyword for row in
             CategoryRule.query.filter_by(category=new).all()}
    moved = 0
    for row in CategoryRule.query.filter_by(category=old).all():
        if row.keyword in taken:
            db.session.delete(row)
            continue
        row.category = new
        taken.add(row.keyword)
        moved += 1
    db.session.commit()
    return moved


def reorder(category_order):
    """Rewrite `position` so categories fall in the given order.

    Positions are reassigned densely from zero across the whole household, in
    category blocks. Categories not named keep their relative order and follow.
    """
    current = all_rules()
    ordered = [c for c in category_order if c in current]
    ordered += [c for c in current if c not in ordered]

    rows = {(row.category, row.keyword): row for row in CategoryRule.query.all()}
    position = 0
    for category in ordered:
        for keyword in current[category]:
            row = rows.get((category, keyword))
            if row is not None:
                row.position = position
                position += 1
    db.session.commit()
    return ordered


def replace_all(rules):
    """Replace this household's entire rule set. Returns the number written.

    For the importer and for tests. Deliberately explicit rather than a merge:
    "set the rules to exactly this" is a different intention from "add these",
    and having one function do both depending on a flag is how the wrong one
    gets called.
    """
    CategoryRule.query.delete()
    position = 0
    for category, keywords in (rules or {}).items():
        for keyword in keywords:
            db.session.add(CategoryRule(category=category, keyword=keyword,
                                        position=position))
            position += 1
    db.session.commit()
    return position


def rule_counts():
    """`{category: keyword_count}`, for a page that shows rule sizes."""
    rows = (db.session.query(CategoryRule.category,
                             func.count(CategoryRule.id))
            .group_by(CategoryRule.category).all())
    return {category: int(count) for category, count in rows}


# ── internals ───────────────────────────────────────────────────────────────

def _ordered():
    return (CategoryRule.query
            .order_by(CategoryRule.position.asc(), CategoryRule.id.asc())
            .all())


def _max_position():
    return db.session.query(func.max(CategoryRule.position)).scalar()


__all__ = ['all_rules', 'as_engine', 'categories', 'clear_all', 'add_rule',
           'remove_rule', 'remove_category', 'rename_category', 'reorder',
           'replace_all', 'rule_counts']
