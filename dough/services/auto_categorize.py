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

## What it costs, and why that is the right trade

The automatic pass reads the **whole** uncategorized backlog on the **deep**
model — see `AUTO_MAX_BATCHES` and the `categorize` role in
`dough/ai/catalog.py` for each half of that. On a first connection it is a
household's entire history, which is minutes of work.

Saying so is the other half of the principle above. `run_once` takes an
`on_progress` callback, the scheduler publishes those frames on
`/api/sync/status`, and `static/js/categorizing.js` draws them — so "Dough is
doing something on your behalf" is a bar with a count on it rather than a
ledger that is half-categorized for reasons the user cannot see.

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

#: The batch cap for an *automatic* pass: `None`, meaning read everything.
#:
#: This used to be 3, on the reasoning that an unasked pass should not spend
#: somebody's API budget walking a whole ledger when its typical job is the
#: handful of merchants a household started using since the last sync. The
#: reasoning was sound about the typical case and wrong about the promise. The
#: page says Dough "categorizes what arrives on its own", and a cap makes that
#: true of the first 360 descriptions and quietly false after them — the user
#: is not told which transactions were never read, only that some rows are
#: still bare. A tester cannot tell that from a model that could not place
#: them, and either way the answer they reach for is the Analyze button this
#: whole feature exists to remove.
#:
#: What keeps the cost bounded is not the cap, it is the gate in
#: `finance_sync/scheduler.py`: nothing new imported means the pass does not
#: run at all. So the work is bounded by what a bank actually delivered, and a
#: description the model cannot place is paid for once rather than on every
#: refresh forever. The batching that remains is a context-window constraint —
#: 4,000 descriptions do not fit in one prompt — not a budget.
#:
#: There is still a ceiling; it is just no longer here. `dough/services/
#: ratelimit.py` allows a household 60 model calls an hour, so a pass past
#: sixty batches — 7,200 distinct uncategorized descriptions — runs out of
#: budget rather than out of cap. `analyze` treats that as any other batch
#: failure: it keeps what the earlier batches produced and reports `partial`,
#: which the dialog turns into "open Rules and press Analyze to finish it".
#: That is the honest shape for a limit nobody in UAT has come within an order
#: of magnitude of, and it is worth knowing that a first connection on that
#: scale will also have spent the household's hourly allowance.
AUTO_MAX_BATCHES = None
AUTO_FIRST_RUN_MAX_BATCHES = None


#: Phase labels a caller may see on `Progress.phase`. They are the three things
#: this pass actually does, in order, and the UI renders one sentence per phase
#: rather than a spinner that means "something is happening".
READING = 'reading'
APPLYING = 'applying'
DONE = 'done'


@dataclass
class Progress:
    """How far the pass has got, for something outside it to render.

    An automatic pass over a first-time connection reads a household's entire
    history on the deep model, and that is minutes rather than seconds. The
    pass was already unattended-safe; what it was not was *legible*. A user who
    had just linked their bank saw a ledger where some rows had categories and
    some did not, with nothing on screen to say the difference was "not read
    yet" rather than "could not be read".

    So the pass reports as it goes. The counts are transactions rather than
    descriptions on purpose: descriptions are the unit the batching works in,
    but "412 of 1,290 transactions" is a sentence about the user's money, and
    "3 of 11 batches" is a sentence about our implementation.

    `total` is fixed when the pass starts and never moves — a progress bar
    whose denominator grows is worse than no progress bar.
    """

    phase: str = READING
    #: True when this is the household's first pass, i.e. their whole history.
    first_run: bool = False
    batches_done: int = 0
    batches_total: int = 0
    descriptions_done: int = 0
    descriptions_total: int = 0
    transactions_done: int = 0
    transactions_total: int = 0
    #: Filled in as the applying phase finishes, so the finished dialog can
    #: report the outcome from the same object it was reporting progress from.
    rules_added: int = 0
    transactions_categorized: int = 0

    @property
    def percent(self) -> int:
        """0-100, measuring **reading** only.

        Applying is one pass over the ledger and is quick and unsplittable;
        giving it a share of the bar would mean inventing a number for it. The
        bar therefore fills as the model reads and sits full while the results
        are written, with the phase label carrying that distinction. A bar that
        reaches 100% and a dialog that has not closed yet is honest; a bar that
        stalls at 90% for a reason we made up is not.
        """
        if not self.transactions_total:
            return 100 if self.phase == DONE else 0
        read = round(100 * self.transactions_done / self.transactions_total)
        return max(0, min(100, read))

    def as_dict(self) -> dict:
        return {'phase': self.phase, 'first_run': self.first_run,
                'batches_done': self.batches_done,
                'batches_total': self.batches_total,
                'descriptions_done': self.descriptions_done,
                'descriptions_total': self.descriptions_total,
                'transactions_done': self.transactions_done,
                'transactions_total': self.transactions_total,
                'rules_added': self.rules_added,
                'transactions_categorized': self.transactions_categorized,
                'percent': self.percent}


def _report(on_progress, progress: Progress) -> None:
    """Hand `progress` to the caller's callback, swallowing anything it raises.

    Progress reporting is decoration on a pass that must never fail. A UI
    callback that throws — a scheduler whose state lock is gone, a test double
    with the wrong signature — must not cost the household its categorization.
    """
    if on_progress is None:
        return
    try:
        on_progress(progress)
    except Exception:
        logger.warning('progress callback failed', exc_info=True)


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


def analyze(*, model=None, role: str = 'suggest',
            surface: str = 'rules_ai_suggest',
            max_batches: Optional[int] = MAX_BATCHES,
            batch_size: int = BATCH_SIZE, on_progress=None,
            first_run: bool = False) -> Analysis:
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
    `max_batches` — or without a ceiling at all when that is `None`, which is
    what the automatic pass asks for. The batching is a context-window
    constraint, not a sampling strategy: each batch knows its position and the
    categories its predecessors proposed, so the merged output reads as one
    analysis rather than twelve unrelated ones.

    A failure part-way through returns what earlier batches produced rather
    than nothing — the suggestions already in hand are good, and discarding
    them would put the user right back in that loop. Only a failure on the
    *first* batch sets `error`.

    `role` names the tier in `dough/ai/catalog.py` this analysis is worth. It
    is a parameter rather than a constant because the two callers are not the
    same job: a person pressing Analyze is waiting on the answer and gets
    `suggest`, while the unattended pass gets `categorize` — the deep model,
    for the reasons recorded next to that role. An explicit `model` still wins
    over both, so the picker on the Rules page keeps working.

    `surface` travels with it and is the same distinction spent rather than
    modelled: it is what `AIService._require_budget` reads to charge the
    unattended pass to `ai_categorize` instead of the interactive `ai` budget,
    and what every log line and audit row for these calls is tagged with.

    `on_progress` is called with a `Progress` once before the first batch and
    once after each one, so a caller can render how far along the read is. It
    is never called after this function returns; the applying phase belongs to
    whoever is orchestrating, and `run_once` reports it.
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
    # 200-row cap. Now that the automatic pass reads everything it decides two
    # smaller things: which rules the user sees first on the Rules page, and —
    # when a run does get cut short — that what it missed is the long tail
    # rather than the household's weekly grocery shop.
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
    # `None` means no ceiling. `batches[:None]` is already the whole list, so
    # only the "was it cut short?" test needs to know the difference.
    analyzed = batches[:max_batches]
    capped = max_batches is not None and len(batches) > max_batches

    raw_suggestions: List[dict] = []
    proposed_cats: List[str] = []
    result = Analysis(total_descriptions=len(descriptions))

    # The denominator the progress bar will use, computed once from what this
    # pass is actually going to read — `analyzed`, not `batches`. A capped run
    # that reports its total as the whole ledger would show a bar that stops
    # at a fraction and calls itself finished.
    progress = Progress(
        first_run=first_run, batches_total=len(analyzed),
        descriptions_total=sum(len(b) for b in analyzed),
        transactions_total=sum(d['count'] for b in analyzed for d in b))
    _report(on_progress, progress)

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
                model=model, role=role, max_tokens=8000,
                metadata={'surface': surface})
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
        progress.batches_done = result.batches
        progress.descriptions_done = result.analyzed_descriptions
        progress.transactions_done += sum(d['count'] for d in batch)
        _report(on_progress, progress)
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
    #: True when this was the household's first pass — their whole history
    #: rather than whatever one sync brought in. The finished dialog reads
    #: differently for it, because it is the only pass the user was watching.
    first_run: bool = False


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


def run_once(*, model=None, on_progress=None) -> AutoRun:
    """Analyze what is uncategorized and apply the result. Never raises.

    The automatic pass, called from the sync scheduler once a sync has imported
    transactions. Everything it decides is reversible: the rules it writes are
    tagged `source='ai'`, labelled on the Rules page, and removable in one
    action by `rules_service.clear_auto()`.

    It declines to run in the two cases where running would be wrong rather
    than merely unhelpful — nothing is uncategorized, or the model is not
    configured — and reports why, so the caller can log something truthful
    instead of an empty success.

    ## It reads all of it, on the deep model

    Both of those are one decision seen from two sides. This pass is the only
    thing standing between a freshly linked bank and a ledger of
    `Uncategorized` rows; it runs unattended; and what it writes is not an
    answer somebody reads once but the rule set every later categorization is
    derived from. Neither a sample of the backlog nor the cheap model produces
    something a household can trust without checking — and a user who has to
    check has not been saved the work. See `AUTO_MAX_BATCHES` and the
    `categorize` role in `dough/ai/catalog.py`.

    A caller that wants to show its progress passes `on_progress`. It is called
    with a `Progress` through the reading phase, again when applying starts,
    and once more when the pass is over — including on every skipped path, so
    a UI that opened a dialog when categorizing started always receives the
    event that closes it.
    """
    def finish(run: AutoRun, progress: Progress) -> AutoRun:
        progress.phase = DONE
        progress.rules_added = run.rules_added
        progress.transactions_categorized = run.transactions_categorized
        _report(on_progress, progress)
        return run

    if not is_enabled():
        return finish(AutoRun(skipped=True,
                              reason='turned off for this household'),
                      Progress())

    uncategorized = Transaction.query.filter_by(category='Uncategorized').count()
    if not uncategorized:
        return finish(AutoRun(skipped=True, reason='nothing uncategorized'),
                      Progress())

    ai = current_ai()
    if not ai.is_available:
        return finish(AutoRun(skipped=True, reason='no model configured'),
                      Progress())

    # A household with no rules is looking at its whole history for the first
    # time; one that already has rules is looking at whatever arrived since the
    # last sync. Both read all of what they are given now, but the difference
    # is still worth knowing: it is what the dialog says to somebody who has
    # just linked a bank and is watching minutes of work go by, as against the
    # routine pass they will never see.
    first_run = not rules_service.categories()
    max_batches = (AUTO_FIRST_RUN_MAX_BATCHES if first_run
                   else AUTO_MAX_BATCHES)

    # Held so the phases after the read keep reporting against the same totals
    # the bar has spent the last few minutes filling towards.
    latest = Progress(first_run=first_run)

    def track(progress: Progress) -> None:
        nonlocal latest
        latest = progress
        _report(on_progress, progress)

    analysis = analyze(model=model, role='categorize',
                       surface='auto_categorize', max_batches=max_batches,
                       on_progress=track, first_run=first_run)
    if analysis.error:
        return finish(AutoRun(skipped=True, reason=analysis.error,
                              first_run=first_run), latest)
    if not analysis.suggestions:
        return finish(AutoRun(skipped=True, reason='no rules could be derived',
                              partial=analysis.partial,
                              first_run=first_run), latest)

    latest.phase = APPLYING
    _report(on_progress, latest)

    incoming = [(s['category'], s['keywords']) for s in analysis.suggestions]
    try:
        added, changed = apply(incoming, source='ai')
    except Exception as exc:
        # A failed automatic pass must leave the ledger exactly as it was and
        # must not take the sync down with it. `apply` rolled back; there is
        # nothing to repair, only something to report.
        logger.exception('automatic categorization failed to apply')
        return finish(AutoRun(skipped=True, reason=str(exc),
                              first_run=first_run), latest)

    remaining = Transaction.query.filter_by(category='Uncategorized').count()
    return finish(AutoRun(rules_added=added, transactions_categorized=changed,
                          remaining_uncategorized=remaining,
                          partial=analysis.partial,
                          first_run=first_run), latest)


__all__ = ['Analysis', 'AutoRun', 'Progress', 'analyze', 'apply',
           'recategorize', 'run_once', 'is_enabled', 'set_enabled',
           'READING', 'APPLYING', 'DONE',
           'BATCH_SIZE', 'MAX_BATCHES', 'AUTO_MAX_BATCHES',
           'AUTO_FIRST_RUN_MAX_BATCHES']
