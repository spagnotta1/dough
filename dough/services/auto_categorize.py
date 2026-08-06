"""Deriving category rules from a household's own descriptions. [UAT round 1]

This is the analysis that `/rules/ai-suggest` used to hold inline, lifted out of
the route so that something other than a button press can run it. The route
still exists and still behaves the same way; it now calls `analyze()` and
`apply()` here, and so does the automatic pass that runs after a sync.

## Why it moved

UAT testers connected a bank, watched their transactions arrive, and then had
to go to a second page, press Analyze, wait, read a stack of cards and press
Accept before their ledger meant anything. Every one of those steps is the
application asking the user to authorize work it already knew it needed to do.
The rules it proposes are derived from the household's own descriptions, and
the accept-all button was being pressed essentially every time.

So the same analysis now runs on its own after a sync imports transactions that
no rule claims. `finance_sync/scheduler.py` owns that trigger, the thread it
runs on and the household it runs for — this module is synchronous and assumes
it is already inside an app context and a tenant scope, which is what keeps it
callable from both a request and a worker.

## What "automatic" is allowed to mean

An automatic pass writes rules with `source='ai'` and re-derives the ledger. It
is not a silent operation: the rules it wrote are labelled on the Rules page,
the count is reported to the user when they next look, and
`rules_service.clear_auto()` removes all of them and only them. The design
principle is that Dough may act without asking, but never without saying.

Allowed:   models, `rules`, `dough.ai`, `dough.tenancy`, sibling services,
           SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import case, func

from dough.ai import persona
from dough.ai.errors import AIError
from dough.ai.service import current_ai
from dough.services import rules_service, transfers

from dough.tenancy import current_household, unscoped

from models import Household, Transaction, db
#: Aliased for the same reason `dough/blueprints/rules.py` aliases it: local
#: names called `matches` shadow a bare import inside these functions.
from rules import matches as keyword_matches

logger = logging.getLogger(__name__)

#: How many distinct descriptions go to the model in one request, and how many
#: requests one analysis will make. [Phase 11A.3]
#:
#: One analysis walks the whole uncategorized ledger. The batching is only a
#: context-window concern — 4,000 descriptions do not fit in one prompt, and a
#: model asked to categorize that many in one reply truncates its JSON.
#: `MAX_BATCHES` bounds what a single analysis can spend; past that the
#: remainder is reported honestly instead of being silently dropped.
BATCH_SIZE = 120
MAX_BATCHES = 12

#: The batch cap for an *automatic* pass, which is deliberately lower.
#:
#: A user who pressed Analyze is watching, has chosen to spend the call, and
#: wants their whole backlog read. An automatic pass runs every time a sync
#: brings in something new, unasked, and its typical job is a handful of
#: merchants a household started using since the last sync. Twelve batches is
#: the right ceiling for the first case and a way to spend somebody's API
#: budget on nothing in the second.
#:
#: The first automatic run on a fresh connection is the exception — that one is
#: a real backlog — so `run_once` raises its own cap when the household has no
#: rules at all.
AUTO_MAX_BATCHES = 3
AUTO_FIRST_RUN_MAX_BATCHES = MAX_BATCHES


@dataclass
class Analysis:
    """What one analysis produced, and how much of the ledger it read."""

    suggestions: List[dict] = field(default_factory=list)
    analyzed_descriptions: int = 0
    total_descriptions: int = 0
    batches: int = 0
    #: True when the analysis did not reach every uncategorized description,
    #: whether because the batch cap cut it off or a batch failed part-way.
    partial: bool = False
    #: Set when the *first* batch failed, which is the only case with nothing
    #: to show. Later failures keep what earlier batches produced.
    error: Optional[str] = None
    #: Whether that error was a configuration problem (no API key) rather than
    #: a call that went wrong, which the route turns into a 503.
    error_is_configuration: bool = False

    @property
    def skipped_descriptions(self) -> int:
        return self.total_descriptions - self.analyzed_descriptions


def analyze(*, model=None, max_batches: int = MAX_BATCHES,
            batch_size: int = BATCH_SIZE) -> Analysis:
    """Read every uncategorized description and propose rules for them.

    ## One analysis, not a sample  [Phase 11A.3]

    This used to send the 200 most frequent descriptions in a single call and
    ask for "5-15 high-confidence rules". Both halves capped it: a household
    with 900 distinct uncategorized descriptions never showed the model 700 of
    them, and the model was told to stop at fifteen rules for the 200 it did
    see. The user pressed Analyze, accepted everything, saw most of the ledger
    still uncategorized, and pressed Analyze again — the loop this was
    reported for.

    It walks the whole uncategorized ledger in batches of `batch_size`, up to
    `max_batches`, and merges the results. The batching is a context-window
    constraint, not a sampling strategy: each batch knows its position and the
    categories its predecessors proposed, so the merged output reads as one
    analysis rather than twelve unrelated ones.

    A failure part-way through returns what earlier batches produced rather
    than nothing — the suggestions already in hand are good, and discarding
    them would put the user right back in that loop. Only a failure on the
    *first* batch sets `error`.
    """
    ai = current_ai()
    if not ai.is_available:
        return Analysis(error='ANTHROPIC_API_KEY not configured',
                        error_is_configuration=True)

    # Netting first, so the model is never asked to write a rule for a movement
    # the arithmetic has already settled. Both halves of a matched pair become
    # `Transfer` here and drop out of the uncategorized set below.
    transfers.net_out_transfers()

    # Ordered by frequency, so batch one holds the merchants the household
    # actually spends at. That ordering used to be the whole defence against a
    # 200-row cap; it now only decides which rules the user sees first — and,
    # for an automatic pass with a low cap, which merchants are worth the call.
    rows = (db.session.query(Transaction.description,
                             func.count(Transaction.id).label('n'))
            .filter(Transaction.category == 'Uncategorized')
            .group_by(Transaction.description)
            .order_by(func.count(Transaction.id).desc())
            .all())
    if not rows:
        return Analysis()

    descriptions = [{'description': r[0], 'count': int(r[1])} for r in rows]
    existing_cats = rules_service.categories()

    batches = [descriptions[i:i + batch_size]
               for i in range(0, len(descriptions), batch_size)]
    analyzed = batches[:max_batches]
    capped = len(batches) > max_batches

    raw_suggestions: List[dict] = []
    proposed_cats: List[str] = []
    result = Analysis(total_descriptions=len(descriptions))

    for index, batch in enumerate(analyzed, start=1):
        try:
            # The model comes from the picker, so it is user-controlled; the
            # catalog resolves an unknown id to the default rather than letting
            # it reach the provider.
            data, _ = ai.generate_json(
                messages=[{'role': 'user',
                           'content': persona.rules_suggest_prompt(
                               existing_cats, batch,
                               batch=(index, len(analyzed)),
                               covered=proposed_cats)}],
                # Room for a full batch's worth of rules. The old 2,000 was
                # sized for the fifteen the prompt asked for, and a longer
                # reply would have been truncated into an `AIResponseError`.
                model=model, role='suggest', max_tokens=8000,
                metadata={'surface': 'rules_ai_suggest'})
        except AIError as exc:
            logger.warning('rule analysis batch %s/%s failed: %s',
                           index, len(analyzed), exc)
            if not raw_suggestions:
                result.error = exc.user_message
                result.error_is_configuration = _is_configuration_error(exc)
                return result
            result.partial = True
            break
        except Exception as exc:
            logger.error('rule analysis unexpected error: %s', exc)
            if not raw_suggestions:
                result.error = f'Unexpected error: {exc}'
                return result
            result.partial = True
            break

        result.analyzed_descriptions += len(batch)
        result.batches += 1
        batch_suggestions = data.get('suggestions', []) or []
        raw_suggestions.extend(batch_suggestions)
        for suggestion in batch_suggestions:
            category = (suggestion.get('category') or '').strip()
            if category and category not in proposed_cats:
                proposed_cats.append(category)

    result.partial = result.partial or capped
    result.suggestions = _enrich(raw_suggestions, existing_cats)
    return result


def _is_configuration_error(exc) -> bool:
    from dough.ai.errors import AIConfigurationError
    return isinstance(exc, AIConfigurationError)


def _enrich(raw_suggestions, existing_cats) -> List[dict]:
    """Attach real match counts and example descriptions to each suggestion.

    Grouped by category rather than one card per rule. The model returns one
    suggestion per keyword and often several for the same category, which the
    page rendered as two separate "Shopping" cards each labelled "new
    category" — two cards proposing the same category, each claiming to invent
    it. Accepting one then made the other's badge a lie.

    Matched against *distinct descriptions* rather than transaction rows. The
    row-scan version was O(keywords x transactions) and was affordable only
    because the keyword count was capped at twenty; an analysis that returns
    two hundred rules over a ledger of forty thousand rows is eight million
    comparisons. Descriptions repeat heavily, and the answer depends on nothing
    else about the row, so the counts travel with the group.
    """
    ledger = _description_counts()
    existing_lower = {c.lower() for c in existing_cats}

    grouped = {}
    for suggestion in raw_suggestions:
        category = (suggestion.get('category') or '').strip()
        keyword = (suggestion.get('keyword') or '').strip()
        reason = (suggestion.get('reason') or '').strip()
        if not category or not keyword:
            continue

        # Case-insensitive, so "shopping" and "Shopping" land on one card
        # instead of two. The first spelling seen wins the display name.
        key = category.lower()
        entry = grouped.setdefault(key, {
            'category': category, 'keywords': [], 'reason': reason,
            'total_count': 0, 'uncat_count': 0, 'examples': [],
            'is_new': key not in existing_lower,
        })
        if keyword in entry['keywords']:
            continue
        entry['keywords'].append(keyword)
        if reason and not entry['reason']:
            entry['reason'] = reason

        # `rules.matches` rather than a local re-implementation, so a card's
        # count is produced by the same matcher that will categorize the rows
        # once the card is accepted. An invalid regex matches nothing there and
        # nothing here, instead of raising in one and being caught in the other.
        for description, total, uncategorized in ledger:
            if not keyword_matches(keyword, description):
                continue
            entry['total_count'] += total
            entry['uncat_count'] += uncategorized
            if len(entry['examples']) < 3 and description not in entry['examples']:
                entry['examples'].append(description)

    # A suggestion matching nothing is dropped rather than shown at zero. The
    # model occasionally proposes a plausible-looking pattern that no
    # description satisfies — an over-escaped regex, or a merchant it inferred
    # rather than read — and a card offering to categorize nothing is noise the
    # user has to evaluate and reject by hand.
    enriched = [e for e in grouped.values() if e['total_count'] > 0]
    enriched.sort(key=lambda e: -e['uncat_count'])
    return enriched


def _description_counts():
    """`[(description, transactions, uncategorized), ...]` for the household.

    One GROUP BY instead of loading the ledger, because the only things the
    suggestion enrichment needs from a transaction are its description and
    whether it is still uncategorized.
    """
    rows = (db.session.query(
                Transaction.description,
                func.count(Transaction.id),
                func.sum(case((Transaction.category == 'Uncategorized', 1),
                              else_=0)))
            .group_by(Transaction.description).all())
    return [(description, int(total or 0), int(uncategorized or 0))
            for description, total, uncategorized in rows]


def apply(incoming, *, source='user'):
    """Write rules and re-derive the ledger once. Returns `(added, changed)`.

    `incoming` is `[(category, [keyword, ...]), ...]`.

    **The re-derivation happens once, after every rule is written.** Accepting
    six suggestions used to mean six requests, each re-deriving the whole
    ledger: O(6 × transactions) to reach a state that one pass computes exactly
    as well, because the final categories depend only on the final rule set. It
    also made "Accept all" non-atomic — a failure on the fourth card left three
    rules applied and the page showing six as accepted.

    Rules land at the TOP of the priority order so an accepted suggestion beats
    the rules that were miscategorizing those transactions. Reversed within
    each category because each insert shifts the previous one down, and without
    it a card's keywords would land in the order the user did not choose.
    """
    added = 0
    for category, keywords in incoming:
        for keyword in reversed(keywords):
            if rules_service.add_rule(category, keyword, first=True,
                                      source=source) is not None:
                added += 1
    if not added:
        return 0, 0
    return added, recategorize()


def recategorize():
    """Re-derive every transaction's category from the current rules.

    Returns how many rows changed.

    Whole-ledger rather than "the rows this keyword matched", and that is the
    fix rather than laziness. A keyword-shaped query cannot answer the question
    correctly in either direction:

    - It cannot match a `/regex/` rule at all — `ILIKE '%/amazon|amzn/%'` looks
      for those literal slashes in the description and finds nothing, so
      removing a pattern rule left every transaction it had categorized sitting
      under a rule that no longer exists.
    - It is too broad for a plain keyword, because a row matching the removed
      rule may still be claimed by a *surviving* one. Blanking it to
      `Uncategorized` threw away a correct categorization.

    Re-deriving is O(transactions) in Python, which is affordable here: this
    runs on a rule edit or after a sync, not on a page view, and the
    alternative is a query that is subtly wrong on the two cases that matter
    most.

    ## What this costs: manual category assignments do not survive

    The invariant is that a category is a pure function of the description and
    the current rule set. `Transaction.category` carries no provenance, so a
    category a person set by hand is indistinguishable from one a rule derived,
    and this rewrites it like any other.

    That was already true of every rule edit. Auto-categorization makes it
    matter more, because the re-derivation now also happens on a schedule
    rather than only when somebody clicked something — a hand-fixed row can be
    overwritten by a pass the user did not initiate. `docs/rule-engine.md`
    holds the worked example and the three options for a `category_source`
    column, and is the specification for that work.

    ## The transfer pass runs after, always

    Rules answer from the description alone, and no description can prove that
    money moved between two accounts the household owns —
    `dough/services/transfers.py` explains why. So the rule pass runs first and
    `net_out_transfers()` runs over its output, relabelling both halves of
    every matched pair.

    Running it here rather than only at import is what keeps it idempotent: the
    rule pass has just reset every row from the current rules, so a pair that
    no longer exists loses the label instead of keeping it forever.

    Raises whatever the commit raised, after rolling back. Callers that must
    not fail (the automatic pass) catch it; the route reports it as a flash.
    """
    engine = rules_service.as_engine()
    changed = 0
    for transaction in Transaction.query.all():
        category = engine.get_category(transaction.description)
        if transaction.category != category:
            transaction.category = category
            changed += 1
    changed += transfers.net_out_transfers(commit=False)
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return changed


@dataclass
class AutoRun:
    """What one automatic pass did, for the UI to report."""

    rules_added: int = 0
    transactions_categorized: int = 0
    remaining_uncategorized: int = 0
    #: True when the analysis could not read the whole backlog, so the user is
    #: told that pressing Analyze on the Rules page will finish the job rather
    #: than being left wondering why some rows are still bare.
    partial: bool = False
    skipped: bool = False
    reason: Optional[str] = None


def _household():
    """This household's row, or None outside a tenant scope.

    `unscoped()` because `Household` is the tenant registry rather than tenant
    data — it carries no `household_id` to filter on, and the id being looked up
    came from the scope itself.
    """
    household_id = current_household()
    if household_id is None:
        return None
    with unscoped():
        return db.session.get(Household, household_id)


def is_enabled() -> bool:
    """Whether this household wants Dough categorizing on its own.

    Defaults to True when there is no household row to ask — the flag is a
    user's choice, and its absence is not a choice to opt out.
    """
    household = _household()
    return True if household is None else bool(household.auto_categorize_enabled)


def set_enabled(enabled: bool) -> None:
    """Turn automatic categorization on or off for this household."""
    household = _household()
    if household is None:
        return
    household.auto_categorize_enabled = bool(enabled)
    db.session.commit()


def run_once(*, model=None) -> AutoRun:
    """Analyze what is uncategorized and apply the result. Never raises.

    The automatic pass, called from the sync scheduler once a sync has imported
    transactions. Everything it decides is reversible: the rules it writes are
    tagged `source='ai'`, labelled on the Rules page, and removable in one
    action by `rules_service.clear_auto()`.

    It declines to run in the two cases where running would be wrong rather
    than merely unhelpful — nothing is uncategorized, or the model is not
    configured — and reports why, so the caller can log something truthful
    instead of an empty success.
    """
    if not is_enabled():
        return AutoRun(skipped=True, reason='turned off for this household')

    uncategorized = Transaction.query.filter_by(category='Uncategorized').count()
    if not uncategorized:
        return AutoRun(skipped=True, reason='nothing uncategorized')

    ai = current_ai()
    if not ai.is_available:
        return AutoRun(skipped=True, reason='no model configured')

    # A household with no rules is looking at its whole history for the first
    # time; one that already has rules is looking at whatever arrived since the
    # last sync. Those are different sizes of job and get different budgets.
    first_run = not rules_service.categories()
    max_batches = (AUTO_FIRST_RUN_MAX_BATCHES if first_run
                   else AUTO_MAX_BATCHES)

    analysis = analyze(model=model, max_batches=max_batches)
    if analysis.error:
        return AutoRun(skipped=True, reason=analysis.error)
    if not analysis.suggestions:
        return AutoRun(skipped=True, reason='no rules could be derived',
                       partial=analysis.partial)

    incoming = [(s['category'], s['keywords']) for s in analysis.suggestions]
    try:
        added, changed = apply(incoming, source='ai')
    except Exception as exc:
        # A failed automatic pass must leave the ledger exactly as it was and
        # must not take the sync down with it. `apply` rolled back; there is
        # nothing to repair, only something to report.
        logger.exception('automatic categorization failed to apply')
        return AutoRun(skipped=True, reason=str(exc))

    remaining = Transaction.query.filter_by(category='Uncategorized').count()
    return AutoRun(rules_added=added, transactions_categorized=changed,
                   remaining_uncategorized=remaining,
                   partial=analysis.partial)


__all__ = ['Analysis', 'AutoRun', 'analyze', 'apply', 'recategorize',
           'run_once', 'is_enabled', 'set_enabled',
           'BATCH_SIZE', 'MAX_BATCHES', 'AUTO_MAX_BATCHES']
