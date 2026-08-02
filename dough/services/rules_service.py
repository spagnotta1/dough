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

## Why a fresh household gets defaults rather than nothing

An empty rule set categorises every transaction as `Uncategorized`, which makes
a new account look broken on the first import — the dashboard is one grey bar and
no budget matches anything. `rules.DEFAULT_RULES` is deliberately small and
generic (five categories, no merchant anybody could recognise as another user's),
so seeding it discloses nothing while leaving the account usable.

Seeding happens on first *read*, not at registration. A household created by an
invitation, a test fixture, or a migration all reach this the same way, and
there is no registration path that can forget to call it.

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
from rules import DEFAULT_RULES, CategoryRules


def all_rules(seed=True):
    """This household's rules as `{category: [keyword, ...]}`, in priority order.

    Seeds the defaults on first read when the household has none — see the
    module docstring for why that is a read and not a registration step.
    """
    rows = _ordered()
    if not rows and seed:
        seed_defaults()
        rows = _ordered()

    grouped = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(row.keyword)
    return grouped


def as_engine(seed=True):
    """A `CategoryRules` over this household's rules.

    The one function `finance_sync`, the importer and the Rules page should all
    call. Returning the engine rather than the dict keeps every caller on one
    implementation of matching.
    """
    return CategoryRules(all_rules(seed=seed))


def categories():
    """Distinct category names, in priority order."""
    return list(all_rules().keys())


def seed_defaults():
    """Give a household with no rules the built-in starter set.

    A no-op when the household already has any rule, so calling it twice — or
    calling it on a household that has deliberately deleted everything and added
    one rule of their own — cannot resurrect the defaults.
    """
    if _count():
        return 0

    position = 0
    for category, keywords in DEFAULT_RULES.items():
        for keyword in keywords:
            db.session.add(CategoryRule(category=category, keyword=keyword,
                                        position=position))
            position += 1
    db.session.commit()
    return position


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


def _count():
    return int(db.session.query(func.count(CategoryRule.id)).scalar() or 0)


def _max_position():
    return db.session.query(func.max(CategoryRule.position)).scalar()


__all__ = ['all_rules', 'as_engine', 'categories', 'seed_defaults', 'add_rule',
           'remove_rule', 'remove_category', 'rename_category', 'reorder',
           'replace_all', 'rule_counts']
