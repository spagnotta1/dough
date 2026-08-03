"""Categorization rules, including the AI suggest/apply pair."""


from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from sqlalchemy import case, func

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.services import rules_service, transfers
from dough.services.categorization import get_category_rules

from models import Transaction, db
#: Aliased: `test()` binds a local named `matches` for its result rows, and a
#: bare import would be shadowed inside that function.
from rules import matches as keyword_matches

bp = Blueprint('rules', __name__)

#: How many distinct descriptions go to the model in one request, and how many
#: requests one analysis will make. [Phase 11A.3]
#:
#: This used to be a single call over the 200 most frequent descriptions, with
#: a prompt asking for "5-15 high-confidence rules". A household with 900
#: distinct uncategorized descriptions therefore got, at best, fifteen rules
#: for its top 200 — so the user accepted everything, watched most of the
#: ledger stay uncategorized, and pressed Analyze again. And again. The button
#: was doing exactly what it was built to do; what it was built to do was a
#: sample.
#:
#: One analysis now walks the whole uncategorized ledger. The batching is only
#: a context-window concern — 4,000 descriptions do not fit in one prompt, and
#: a model asked to categorize that many in one reply truncates its JSON.
#: `MAX_BATCHES` bounds what a single click can spend; past that the remainder
#: is reported honestly instead of being silently dropped.
AI_BATCH_SIZE = 120
AI_MAX_BATCHES = 12

@bp.route('/rules', methods=['GET', 'POST'])
def index():
    """The Rules page. Every write goes through `rules_service`, per household.

    ## Two bugs this route used to have  [Phase 11A.1]

    **"Delete category" did nothing and said it worked.** The button posts
    `action=remove` with a category and no keyword, so this called
    `remove_rule(category, None)`. That tested `None in [...]`, matched nothing,
    and returned silently — after which the route flashed "Rule removed"
    unconditionally. There is a `remove_category` action now, and every branch
    reports what actually happened rather than what was attempted.

    **Deleting a keyword uncategorized too much.** The recategorize query
    matched `description ILIKE %keyword%`, which is wrong for a `/regex/` rule
    (it looked for the literal string `/amazon|amzn/` in descriptions and found
    nothing) and too broad for a plain one — it reset rows that a *different*
    surviving rule still claims. Both paths now re-derive the category from the
    remaining rules, which is the only answer that stays correct.
    """
    if request.method == 'POST':
        action = request.form.get('action')
        category = (request.form.get('category') or '').strip()
        keyword = (request.form.get('keyword') or '').strip()

        if action == 'add':
            if not category or not keyword:
                flash('A rule needs both a category and a keyword.', 'error')
            elif rules_service.add_rule(category, keyword) is None:
                flash(f'"{keyword}" is already a {category} rule.', 'info')
            else:
                changed = _recategorize()
                flash(f'Rule saved — I recategorized {changed} '
                      f'transaction{"" if changed == 1 else "s"} to match.',
                      'success')

        elif action == 'remove':
            if rules_service.remove_rule(category, keyword):
                changed = _recategorize()
                flash(f'Removed "{keyword}" — I recategorized {changed} '
                      f'transaction{"" if changed == 1 else "s"}.', 'success')
            else:
                flash('That rule was already gone.', 'info')

        elif action == 'remove_category':
            removed = rules_service.remove_category(category)
            if removed:
                changed = _recategorize()
                flash(f'Deleted {category} and its {removed} '
                      f'keyword{"" if removed == 1 else "s"} — I recategorized '
                      f'{changed} transaction{"" if changed == 1 else "s"}.',
                      'success')
            else:
                flash(f'There is no {category} rule to delete.', 'info')

        elif action == 'clear_all':
            removed = rules_service.clear_all()
            if removed:
                changed = _recategorize()
                flash(f'Cleared all {removed} rule{"" if removed == 1 else "s"} '
                      f'— {changed} transaction{"" if changed == 1 else "s"} '
                      f'went back to Uncategorized. Nothing will be seeded in '
                      f'their place; ask Dough to suggest rules when you are '
                      f'ready.', 'success')
            else:
                flash('There were no rules to clear.', 'info')

        elif action == 'rename_category':
            new_name = (request.form.get('new_category') or '').strip()
            moved = rules_service.rename_category(category, new_name)
            if moved:
                _recategorize()
                flash(f'Renamed {category} to {new_name}.', 'success')
            else:
                flash('Nothing to rename.', 'info')

        return redirect(url_for('rules.index'))

    rules = rules_service.all_rules()
    # Match counts, not label counts. `Transaction.category == name` answers a
    # different question — how many rows carry this label, which survives the
    # rule that wrote it — and a household carrying rules it never transacted
    # against saw healthy numbers beside rules matching nothing. See
    # `rules_service.match_counts`.
    matched = rules_service.match_counts()
    rule_stats = {category: matched.get(category, 0) for category in rules}
    uncategorized_count = Transaction.query.filter_by(
        category='Uncategorized').count()

    # A household with no rules and transactions to read gets the analysis
    # started for it, rather than a starter set written by somebody else.
    # [Phase 11A.2] This is what replaced `DEFAULT_RULES` — see `rules.py` for
    # what that used to contain and why seeding it was a disclosure.
    #
    # Three conditions, and each one is a way the automatic run would be wrong:
    # rules already exist (the household has answered this question), there is
    # nothing uncategorized (there would be nothing to analyze), or no API key
    # (the request would fail and the page would open on an error). The button
    # stays exactly where it is for every other case.
    autostart_ai = (not rules
                    and uncategorized_count > 0
                    and current_ai().is_available)

    return render_template('rules.html', rules=rules,
                           rule_stats=rule_stats,
                           uncategorized_count=uncategorized_count,
                           autostart_ai=autostart_ai)


def _recategorize():
    """Re-derive every transaction's category from the current rules.

    Returns how many rows changed, which is what the flash message reports.

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
    runs on an explicit rule edit, not on a page view, and the alternative is
    a query that is subtly wrong on the two cases that matter most.

    ## What this costs: manual category assignments do not survive

    The invariant is that a category is a pure function of the description and
    the current rule set. `Transaction.category` carries no provenance, so a
    category a person set by hand — through `/update_category`,
    `/update_categories_bulk`, `PUT /transactions/<id>` or the v1 bulk endpoint
    — is indistinguishable from one a rule derived, and this rewrites it like
    any other. Adding an unrelated rule can therefore silently undo hand
    categorization elsewhere in the ledger, reported only as a count in the
    flash message.

    That is the intended behaviour today and not an oversight: fixing a
    miscategorized row by hand is not durable, and fixing the rule is. It is
    still the thing people are surprised by.

    TODO (rule engine, future enhancement): preserve manual assignments across
    re-derivation, or warn before a bulk rewrite. The blocker is schema rather
    than logic — with no `category_source` column there is nothing to decide
    from. `docs/rule-engine.md` holds the worked example and the three options,
    and is the specification for that work.

    ## The transfer pass runs after, always

    Rules answer from the description alone, and no description can prove that
    money moved between two accounts the household owns —
    `dough/services/transfers.py` explains why. So the rule pass runs first and
    `net_out_transfers()` runs over its output, relabelling both halves of every
    matched pair.

    Running it here rather than only at import is what keeps it idempotent: the
    rule pass has just reset every row from the current rules, so a pair that no
    longer exists loses the label instead of keeping it forever.
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
        except Exception as exc:                       # pragma: no cover
            db.session.rollback()
            current_app.logger.error('recategorize failed: %s', exc)
            flash('I could not update your transactions.', 'error')
            return 0
    return changed

@bp.route('/rules/test', methods=['POST'])
def test():
    import re as _re
    keyword = (request.json or {}).get('keyword', '').strip()
    if not keyword:
        return jsonify({'matches': []})
    if keyword.startswith('/') and keyword.endswith('/') and len(keyword) > 2:
        pattern = keyword[1:-1]
        try:
            all_txns = Transaction.query.order_by(Transaction.date.desc()).limit(2000).all()
            matches = [t for t in all_txns if _re.search(pattern, t.description, _re.IGNORECASE)][:10]
        except Exception:
            matches = []
    else:
        matches = Transaction.query.filter(
            Transaction.description.ilike(f'%{keyword}%')
        ).order_by(Transaction.date.desc()).limit(10).all()
    return jsonify({'matches': [
        {'date': str(t.date), 'description': t.description,
         'amount': float(t.amount), 'category': t.category}
        for t in matches
    ]})

@bp.route('/rules/reorder', methods=['POST'])
def reorder():
    """Drag-to-reorder. Priority is `CategoryRule.position`, lower wins.

    The reordering itself moved into `rules_service` in Phase 11A.1: this used
    to rebuild the dict here and then call `rules_engine._save_rules()`, which
    wrote the shared JSON file — a private method, from a route, persisting one
    household's priority order for every household in the installation.
    """
    order = (request.json or {}).get('order', [])
    if not isinstance(order, list):
        return jsonify({'success': False, 'error': 'order must be a list'}), 400
    rules_service.reorder([str(name) for name in order])
    return jsonify({'success': True})

@bp.route('/rules/ai-suggest', methods=['POST'])
def ai_suggest():
    """Analyze **every** uncategorized description and return rule suggestions.

    ## One click, one analysis  [Phase 11A.3]

    This used to send the 200 most frequent descriptions in a single call and
    ask for "5-15 high-confidence rules". Both halves capped it: a household
    with 900 distinct uncategorized descriptions never showed the model 700 of
    them, and the model was told to stop at fifteen rules for the 200 it did
    see. The user pressed Analyze, accepted everything, saw most of the ledger
    still uncategorized, and pressed Analyze again — the loop this route was
    reported for.

    It now walks the whole uncategorized ledger in batches of
    `AI_BATCH_SIZE`, up to `AI_MAX_BATCHES`, and merges the results. The
    batching is a context-window constraint, not a sampling strategy: each
    batch knows its position and the categories its predecessors proposed, so
    the merged output reads as one analysis rather than twelve unrelated ones.

    A failure part-way through returns what earlier batches produced rather
    than nothing — the suggestions already in hand are good, and discarding
    them would put the user right back in the loop this was written to end.
    Only a failure on the *first* batch is an error response.
    """
    body = request.get_json(force=True) or {}
    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503

    # Netting first, so the model is never asked to write a rule for a movement
    # the arithmetic has already settled. Both halves of a matched pair become
    # `Transfer` here and drop out of the uncategorized set below.
    transfers.net_out_transfers()

    # Ordered by frequency, so batch one holds the merchants the household
    # actually spends at. That ordering used to be the whole defence against a
    # 200-row cap; it now only decides which rules the user sees first.
    rows = (db.session.query(Transaction.description,
                             func.count(Transaction.id).label('n'))
            .filter(Transaction.category == 'Uncategorized')
            .group_by(Transaction.description)
            .order_by(func.count(Transaction.id).desc())
            .all())
    if not rows:
        return jsonify({'suggestions': [], 'message': 'No uncategorized transactions found.'})

    descriptions  = [{'description': r[0], 'count': int(r[1])} for r in rows]
    existing_cats = list(get_category_rules().get_all_rules().keys())

    batches = [descriptions[i:i + AI_BATCH_SIZE]
               for i in range(0, len(descriptions), AI_BATCH_SIZE)]
    analyzed = batches[:AI_MAX_BATCHES]
    skipped = sum(len(b) for b in batches[AI_MAX_BATCHES:])

    raw_suggestions = []
    proposed_cats   = []
    analyzed_count  = 0
    done_batches    = 0
    failed          = False
    for index, batch in enumerate(analyzed, start=1):
        try:
            # The model comes from the picker, so it is user-controlled; the
            # catalog resolves an unknown id to the default rather than letting
            # it reach the provider. That replaces the old inline allow-set.
            data, _ = ai.generate_json(
                messages=[{'role': 'user',
                           'content': persona.rules_suggest_prompt(
                               existing_cats, batch,
                               batch=(index, len(analyzed)),
                               covered=proposed_cats)}],
                # Room for a full batch's worth of rules. The old 2,000 was
                # sized for the fifteen the prompt asked for, and a longer
                # reply would have been truncated into an `AIResponseError`.
                model=body.get('model'), role='suggest', max_tokens=8000,
                metadata={'surface': 'rules_ai_suggest'})
        except AIError as e:
            current_app.logger.warning('rules_ai_suggest batch %s/%s failed: %s',
                                       index, len(analyzed), e)
            if not raw_suggestions:
                return jsonify({'error': e.user_message}), 503 if isinstance(
                    e, AIConfigurationError) else 500
            failed = True
            break
        except Exception as e:
            current_app.logger.error('rules_ai_suggest unexpected error: %s', e)
            if not raw_suggestions:
                return jsonify({'error': f'Unexpected error: {e}'}), 500
            failed = True
            break

        analyzed_count += len(batch)
        done_batches += 1
        batch_suggestions = data.get('suggestions', []) or []
        raw_suggestions.extend(batch_suggestions)
        for s in batch_suggestions:
            cat = (s.get('category') or '').strip()
            if cat and cat not in proposed_cats:
                proposed_cats.append(cat)

    # Enrich each suggestion with real match counts and example descriptions.
    #
    # Grouped by category rather than one card per rule. The model returns one
    # suggestion per keyword and often several for the same category, which the
    # page rendered as two separate "Shopping" cards each labelled "new
    # category" — two cards proposing the same category, each claiming to
    # invent it. Accepting one then made the other's badge a lie.
    #
    # Matched against *distinct descriptions* rather than transaction rows.
    # The row-scan version was O(keywords x transactions) and was affordable
    # only because the keyword count was capped at twenty; an analysis that
    # returns two hundred rules over a ledger of forty thousand rows is eight
    # million comparisons. Descriptions repeat heavily, and the answer depends
    # on nothing else about the row, so the counts travel with the group.
    ledger = _description_counts()
    existing_lower = {c.lower() for c in existing_cats}

    grouped = {}
    for s in raw_suggestions:
        cat    = (s.get('category') or '').strip()
        kw     = (s.get('keyword')  or '').strip()
        reason = (s.get('reason')   or '').strip()
        if not cat or not kw:
            continue

        # Case-insensitive, so "shopping" and "Shopping" land on one card
        # instead of two. The first spelling seen wins the display name.
        key = cat.lower()
        entry = grouped.setdefault(key, {
            'category': cat, 'keywords': [], 'reason': reason,
            'total_count': 0, 'uncat_count': 0, 'examples': [],
            'is_new': key not in existing_lower,
        })
        if kw in entry['keywords']:
            continue
        entry['keywords'].append(kw)
        if reason and not entry['reason']:
            entry['reason'] = reason

        # `rules.matches` rather than a local re-implementation, so a card's
        # count is produced by the same matcher that will categorize the rows
        # once the card is accepted. An invalid regex matches nothing there and
        # nothing here, instead of raising in one and being caught in the other.
        for description, total, uncategorized in ledger:
            if not keyword_matches(kw, description):
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

    # What the analysis actually covered, so the page can say so. A user who
    # has been trained by the old behaviour to press Analyze repeatedly needs
    # to be told that this pass read everything — and told honestly when it
    # did not.
    return jsonify({
        'suggestions': enriched,
        'analyzed_descriptions': analyzed_count,
        'total_descriptions': len(descriptions),
        # Everything the analysis did not reach, whether because the batch cap
        # cut it off or because a batch failed part-way through.
        'skipped_descriptions': len(descriptions) - analyzed_count,
        'batches': done_batches,
        'partial': failed or skipped > 0,
    })


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

@bp.route('/rules/ai-apply', methods=['POST'])
def ai_apply():
    """Accept AI suggestions: add the rules, then recategorize once.

    Takes `keywords` (a list) or `keyword` (one), and `categories` (a list of
    `{category, keywords}`) for "Accept all". All three land on the same path.

    **The re-derivation happens once, after every rule is written.** Accepting
    six suggestions used to mean six requests, each re-deriving the whole
    ledger: O(6 × transactions) to reach a state that one pass computes exactly
    as well, because the final categories depend only on the final rule set. It
    also made "Accept all" non-atomic — a failure on the fourth card left three
    rules applied and the page showing six as accepted.
    """
    body = request.get_json(force=True) or {}

    # One shape internally, whichever shape arrived.
    if body.get('categories'):
        incoming = [(str(c.get('category') or '').strip(),
                     [str(k).strip() for k in (c.get('keywords') or []) if str(k).strip()])
                    for c in body['categories']]
    else:
        category = (body.get('category') or '').strip()
        keywords = body.get('keywords') or ([body.get('keyword')]
                                            if body.get('keyword') else [])
        incoming = [(category, [str(k).strip() for k in keywords if str(k).strip()])]

    incoming = [(c, k) for c, k in incoming if c and k]
    if not incoming:
        return jsonify({'error': 'Missing category or keyword'}), 400

    # Added at the TOP of the priority order so an accepted suggestion beats the
    # rules that were miscategorizing those transactions. Reversed within each
    # category because each insert shifts the previous one down, and without it
    # a card's keywords would land in the order the user did not choose.
    added = 0
    for category, keywords in incoming:
        for keyword in reversed(keywords):
            if rules_service.add_rule(category, keyword, first=True) is not None:
                added += 1

    if not added:
        return jsonify({'ok': True, 'applied_count': 0, 'added': 0,
                        'message': 'Those rules already exist.'})

    # Re-derive from the whole rule set rather than matching the keywords here.
    # The old version applied the new rule directly to every row it matched,
    # which ignored priority: a transaction claimed by a higher rule was
    # reassigned anyway, so accepting a broad suggestion silently overwrote
    # categories the user had already curated. `add_rule(first=True)` puts these
    # rules at the top, so re-deriving gives them precedence *and* respects the
    # rest of the order.
    engine = rules_service.as_engine()
    count = 0
    try:
        for transaction in Transaction.query.all():
            resolved = engine.get_category(transaction.description)
            if transaction.category != resolved:
                transaction.category = resolved
                count += 1
        # Same second pass as `_recategorize`, for the same reason: the rules
        # just overwrote every row, including the transfers a previous run had
        # netted out. See `dough/services/transfers.py`.
        count += transfers.net_out_transfers(commit=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'applied_count': count, 'added': added})
