"""Access to the shared `CategoryRules` engine.

`create_app()` used to build one `CategoryRules()` and close over it, which is
why the Rules page and the CSV importer could reach it and nothing else could.
This module holds it instead, behind a lazy accessor.

**Lifetime.** The instance is now one per *process* rather than one per Flask
app. In production those are the same thing — one `create_app()` per worker —
and the rules have always been backed by a single `category_rules.json` at the
repo root, so the persisted state was already process-wide. The only observable
change is that a second `create_app()` in one process reuses the first
instance's in-memory dict instead of re-reading the file. `reset_category_rules`
exists for the case where a test needs the old behaviour back.

**What must not change.** `finance_sync/repository.py` builds its *own* fresh
`CategoryRules()` for every sync, deliberately, so that a rule edited in the
Rules page is picked up by the next sync without a restart. It must not be
switched to this accessor — doing so would pin a sync's categorization to
whatever was loaded at boot, silently, and the symptom (a newly added rule not
applying to synced transactions but applying to CSV imports) is very hard to
attribute. There is a test for this; see Phase 3.5 in the plan.

Allowed:   `rules`, stdlib
Must not:  app, models, Flask anything, anthropic
"""

from rules import CategoryRules

_rules = None


def get_category_rules():
    """Return the process-wide `CategoryRules`, constructing it on first use.

    Lazy rather than built at import: constructing it reads
    `category_rules.json` off disk, and an import-time file read would happen
    before `create_app()` had a chance to fail for a better reason.
    """
    global _rules
    if _rules is None:
        _rules = CategoryRules()
    return _rules


def reset_category_rules():
    """Drop the cached instance so the next call re-reads the rules file."""
    global _rules
    _rules = None
