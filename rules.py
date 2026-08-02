"""Category matching: given a rule set and a description, name the category.

## What changed in Phase 11A.1, and why

This module used to own **storage as well as matching**, and it stored rules in
`category_rules.json` — one file at the repo root, shared by every household in
the installation. There was no `household_id` in this file, in
`dough/services/categorization.py`, or on the Rules page, so the second family to
sign in saw the first family's rules. Not a leak through a missing filter: the
rules were never tenanted, because there was only ever one rule set.

Storage is now `models.CategoryRule`, one tenant-scoped row per
(category, keyword), and `dough/services/rules_service.py` owns reading and
writing it. What is left here is the part that was always correct and always
worth keeping framework-free: **the matching**.

That split is what lets `finance_sync/repository.py` categorise a synced
transaction, a test assert on a pattern, and the Rules page preview all use one
implementation of "does this keyword match this description".

## The rule set it takes

An ordered mapping of `{category: [keyword, ...]}`. Order is priority — the
first category with a matching keyword wins, which is what the Rules page means
by "rules higher in the list win". A `dict` is ordered in every Python this runs
on, so the caller's ordering is preserved without a special type.

A keyword wrapped in slashes is a regular expression: `/amazon|amzn/` matches
either. Anything else is a case-insensitive substring.

Allowed:   stdlib
Must not:  app, models, flask, anthropic, dough.*
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

#: What a description gets when nothing matches. The ledger, the importer and
#: the Rules page all compare against this string, so it is named once.
UNCATEGORIZED = 'Uncategorized'

#: The rule set a household starts with. Small on purpose: a default that tries
#: to be comprehensive is a default that miscategorises confidently, and the
#: Rules page's AI suggestions are the intended way to grow it.
#:
#: `migrations/versions/20260802_08_category_rules.py` carries its own copy
#: rather than importing this one — a migration must keep behaving the way it
#: did the day it was written.
DEFAULT_RULES: Dict[str, List[str]] = {
    'Student Loan': ['First Tech FCU', 'FIRSTMARK'],
    'Investments': ['VANGUARD BUY'],
    'Credit Card': ['CAPITAL ONE', 'CHASE CREDIT CRD'],
    'Auto Loan': ['JPMorgan Chase'],
    'Income': ['TEVA PHARMA'],
}


def is_pattern(keyword: str) -> bool:
    """Whether `keyword` is a `/regex/` rather than a substring.

    The length check matters: `/` alone and `//` both start and end with a
    slash, and stripping them leaves an empty pattern that matches everything —
    which would put every transaction in one category.
    """
    return (isinstance(keyword, str) and len(keyword) > 2
            and keyword.startswith('/') and keyword.endswith('/'))


def matches(keyword: str, description: str) -> bool:
    """Whether one keyword matches one description.

    An invalid regex matches nothing rather than raising. A user typing a rule
    into a text box will produce `/(unclosed/` sooner or later, and the failure
    should be that their rule does not fire — not a 500 on the import that
    happens to run next.
    """
    if not keyword or not description:
        return False
    if is_pattern(keyword):
        try:
            return bool(re.search(keyword[1:-1], description, re.IGNORECASE))
        except re.error:
            return False
    return keyword.upper() in description.upper()


class CategoryRules:
    """Matching over an ordered rule set. Holds no storage of its own.

    Constructed with the rules rather than loading them, which is the whole of
    the Phase 11A.1 change to this class: a rule set now arrives from a
    household-scoped query instead of from a process-wide file, and this class
    cannot tell the difference or reach the wrong one.
    """

    def __init__(self, rules: Optional[Dict[str, List[str]]] = None):
        self.rules: Dict[str, List[str]] = (
            {category: list(keywords) for category, keywords in rules.items()}
            if rules else {})

    # -- matching -------------------------------------------------------------

    def get_category(self, description: str) -> str:
        """The first category whose keyword matches, or `Uncategorized`."""
        for category, keywords in self.rules.items():
            for keyword in keywords:
                if matches(keyword, description):
                    return category
        return UNCATEGORIZED

    def explain(self, description: str):
        """`(category, keyword)` for the rule that won, or `(None, None)`.

        The Rules page's "test a description" box needs to show *why* something
        categorised the way it did, and a bare category name cannot answer "which
        of my nine Subscriptions patterns caught this?".
        """
        for category, keywords in self.rules.items():
            for keyword in keywords:
                if matches(keyword, description):
                    return category, keyword
        return None, None

    def get_all_rules(self) -> Dict[str, List[str]]:
        return self.rules

    def categories(self) -> List[str]:
        return list(self.rules)

    # -- in-memory edits ------------------------------------------------------
    #
    # These mutate the local copy only. Persistence belongs to
    # `dough/services/rules_service.py`, which is the one thing that knows which
    # household it is writing for. A caller that edits an instance and expects
    # the change to survive the request is the bug this separation prevents.

    def add_rule(self, category: str, keyword: str) -> None:
        """Append a keyword, keeping the category where it is in the order."""
        if not category or not keyword:
            return
        self.rules.setdefault(category, [])
        if keyword not in self.rules[category]:
            self.rules[category].append(keyword)

    def add_rule_first(self, category: str, keyword: str) -> None:
        """Add a keyword and move its category to the front of the order.

        Rules are evaluated in order, so this is how a rule is made to win over
        every existing one.
        """
        if not category or not keyword:
            return
        existing = list(self.rules.get(category, []))
        if keyword not in existing:
            existing.append(keyword)
        self.rules = {category: existing,
                      **{c: k for c, k in self.rules.items() if c != category}}

    def remove_rule(self, category: str, keyword: str) -> bool:
        """Remove one keyword. Returns whether anything was removed.

        The return value is not decoration. The Rules page used to report
        "Rule removed" unconditionally, including when it had removed nothing —
        see `remove_category` for the bug that hid behind that.

        A category left with no keywords is dropped, because a category that
        matches nothing is not a rule.
        """
        if category not in self.rules or keyword not in self.rules[category]:
            return False
        self.rules[category].remove(keyword)
        if not self.rules[category]:
            del self.rules[category]
        return True

    def remove_category(self, category: str) -> bool:
        """Remove a category and every keyword in it.

        **This did not exist**, and its absence was a user-visible bug. The
        Rules page's "Delete category" button posted `action=remove` with a
        `category` and no `keyword`, so the route called
        `remove_rule(category, None)`; that tested `None in [...]`, found
        nothing, and returned silently — after which the route flashed "Rule
        removed" regardless. Deleting a category appeared to work and never did.
        """
        if category not in self.rules:
            return False
        del self.rules[category]
        return True

    def reorder(self, new_order: List[str]) -> None:
        """Reorder categories. Names not listed keep their relative order, last."""
        reordered = {category: self.rules[category] for category in new_order
                     if category in self.rules}
        for category, keywords in self.rules.items():
            if category not in reordered:
                reordered[category] = keywords
        self.rules = reordered


__all__ = ['CategoryRules', 'DEFAULT_RULES', 'UNCATEGORIZED', 'matches',
           'is_pattern']
