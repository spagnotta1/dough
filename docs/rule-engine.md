# The categorization rule engine

How a transaction gets its category, and the one behaviour in here that
surprises people: **editing a rule rewrites the category on every transaction in
the household, including categories a person set by hand.**

This is a description of what the code does today. Nothing here is a proposal;
the change it argues for is in [Future work](#future-work-preserving-manual-overrides)
and has not been made.

## The pieces

| Where | What it owns |
| --- | --- |
| [rules.py](../rules.py) | `CategoryRules` — matching only. Takes `{category: [keyword, ...]}`, answers `get_category(description)`. Holds no state and knows nothing about households. |
| [dough/services/rules_service.py](../dough/services/rules_service.py) | The rows. Every read and write goes through `CategoryRule.query`, which is tenant-scoped. `as_engine()` hands back a `CategoryRules` over this household's rules, in priority order. |
| [dough/blueprints/rules.py](../dough/blueprints/rules.py) | The Rules page. Reads the form, calls the service, re-derives, redirects. |

A keyword is a plain substring, or a regular expression when wrapped in slashes
(`/amazon|amzn/`). `CategoryRule.position` orders them ascending, **lower wins**,
matching the page's "rules higher in the list win"; `get_category` takes the
first match, so it needs to know nothing about priority.

## The invariant

> A transaction's category is a pure function of its description and the current
> rule set.

That is what `_recategorize()` enforces, and it is worth stating plainly because
every consequence below — the good ones and the surprising one — follows from it
and from nothing else.

## `_recategorize()` recalculates the whole ledger, deliberately

[`_recategorize()`](../dough/blueprints/rules.py#L95) walks
`Transaction.query.all()`, asks the engine for each description's category,
writes back the ones that differ, and returns the count the flash message
reports.

It runs on every rule mutation:

| Trigger | Route |
| --- | --- |
| Add a keyword | `POST /rules` `action=add` |
| Remove a keyword | `POST /rules` `action=remove` |
| Delete a category | `POST /rules` `action=remove_category` |
| Rename a category | `POST /rules` `action=rename_category` |
| Accept an AI suggestion | `POST /rules/ai-apply` — an equivalent loop inlined at [rules.py:273](../dough/blueprints/rules.py#L273) |

Whole-ledger, rather than "the rows this keyword matched", is the fix rather than
laziness. A keyword-shaped query cannot answer the question correctly in either
direction:

- **It cannot match a `/regex/` rule at all.** `ILIKE '%/amazon|amzn/%'` looks
  for those literal slashes in the description and finds nothing, so removing a
  pattern rule used to leave every transaction it had categorised sitting under a
  rule that no longer existed.
- **It is too broad for a plain keyword.** A row matching the removed rule may
  still be claimed by a *surviving* one. Blanking it to `Uncategorized` threw
  away a correct categorisation.

Re-deriving is O(transactions) in Python. That is affordable because it runs on
an explicit rule edit, not on a page view, and the alternative is a query that is
subtly wrong on the two cases that matter most.

## The consequence: manual category assignments are overwritten

`Transaction.category` is a single `String(50)` column
([models.py:540](../models.py#L540)). **There is no provenance on it.** Nothing
in the schema distinguishes a category a rule derived from one a person chose in
the UI, so the re-derivation cannot tell them apart and does not try.

Categories set by hand through any of these are subject to being rewritten by the
next rule edit:

- `POST /update_category` — the per-row dropdown on the Transactions page
- `POST /update_categories_bulk` — the multi-select bulk action
- `PUT /transactions/<id>` with a `category` key
- `POST /api/v1/transactions/bulk` with `action=recategorize`

Worked example, and the shape of every report of this:

1. A transaction reads `SQ *BLUE BOTTLE`. No rule matches it, so it is
   `Uncategorized`.
2. Someone opens Transactions and sets it to `Coffee` by hand. Correct, and the
   only way to record it — there is no rule for it.
3. A week later they add an unrelated rule: `Gas` → `SHELL`.
4. `_recategorize()` runs over the whole ledger. `SQ *BLUE BOTTLE` still matches
   no rule, so the engine answers `Uncategorized`, which differs from `Coffee`,
   so it is written back. The hand-set category is gone, counted only as one
   number in "I recategorised 31 transactions".

The loss is silent in the sense that matters: the flash message reports a count,
never which rows or what they used to say, and nothing warns beforehand.

Two related notes for anyone reading the code:

- **The same applies to a *wrong* rule-derived category**, which is the point of
  the design. Fixing a miscategorised row by hand and leaving the rule alone is
  not durable; fixing the rule is.
- **`POST /rules/reorder` does not re-derive.** Reordering changes which rule
  wins a conflict, so the stored categories can disagree with what the current
  priority order would produce, until the next rule edit reconciles them. This
  is a real inconsistency, not a documented intent.

## Future work: preserving manual overrides

> **TODO — rule engine, future enhancement.** Preserve manual category
> assignments across re-derivation, or warn before a bulk rewrite. Not scheduled;
> this section is the specification for when it is.

The blocker is schema, not logic: with no provenance column there is no
information from which to decide what to keep. Any implementation starts by
recording where a category came from. Three options, roughly in order of cost:

1. **Warn, do not change.** Before committing, count the rows the re-derivation
   would change and confirm — "this will recategorise 31 transactions, 4 of them
   ones you set yourself". Cheapest, and it still needs option 2's column to say
   "4 of them".
2. **A provenance column.** `Transaction.category_source` — `'rule'` |
   `'manual'` | `'import'` — stamped by whoever sets the category, defaulting to
   `'rule'`. `_recategorize()` then skips `'manual'` rows. Needs a migration, a
   backfill decision for every existing row (they would have to be assumed
   `'rule'`, since the information to say otherwise was never recorded), and a
   way to release a row back to rule control.
3. **Per-transaction pinning in the UI.** Option 2 plus an explicit control — a
   lock on the row, and a way to see and clear every pinned transaction. Without
   the "see and clear" half, a mis-pinned row becomes a category no rule change
   can ever fix, which is a worse failure than the one being fixed.

Whichever is chosen, the invariant at the top of this document has to be restated
rather than quietly abandoned — "a pure function of description and rule set,
*except* for rows a person has claimed" — because the current wording is what
makes the two `/regex/` bugs above impossible to reintroduce.

## See also

- [ADR-0007](adr/0007-alembic-as-the-sole-schema-authority.md) — why a
  provenance column means a migration, and why that is the only way to add one.
- [dough/services/README.md](../dough/services/README.md) — the service layer's
  rules about what may import what.
- `tests/test_rules_tenancy.py` — the re-derivation behaviour under test,
  including the regex case.
