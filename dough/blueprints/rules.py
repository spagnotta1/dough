"""Categorization rules, including the AI suggest/apply pair."""


import re

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.services import rules_service
from dough.services.categorization import get_category_rules

from models import Transaction, db

bp = Blueprint('rules', __name__)

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
    rule_stats = {category: Transaction.query.filter(
        Transaction.category == category).count() for category in rules}
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
    """
    engine = rules_service.as_engine()
    changed = 0
    for transaction in Transaction.query.all():
        category = engine.get_category(transaction.description)
        if transaction.category != category:
            transaction.category = category
            changed += 1
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
    """Send uncategorized descriptions to Claude and get rule suggestions."""
    body = request.get_json(force=True) or {}
    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured'}), 503

    # Unique uncategorized descriptions (cap at 200 to stay within token limits)
    rows = (Transaction.query
            .filter_by(category='Uncategorized')
            .with_entities(Transaction.description)
            .distinct()
            .limit(200)
            .all())
    if not rows:
        return jsonify({'suggestions': [], 'message': 'No uncategorized transactions found.'})

    descriptions     = [r[0] for r in rows]
    existing_cats    = list(get_category_rules().get_all_rules().keys())

    try:
        # The model comes from the picker, so it is user-controlled; the
        # catalog resolves an unknown id to the default rather than letting
        # it reach the provider. That replaces the old inline allow-set.
        data, _ = ai.generate_json(
            messages=[{'role': 'user',
                       'content': persona.rules_suggest_prompt(existing_cats,
                                                              descriptions)}],
            model=body.get('model'), role='suggest', max_tokens=2000,
            metadata={'surface': 'rules_ai_suggest'})
        raw_suggestions = data.get('suggestions', [])
    except AIError as e:
        current_app.logger.warning('rules_ai_suggest failed: %s', e)
        return jsonify({'error': e.user_message}), 503 if isinstance(
            e, AIConfigurationError) else 500
    except Exception as e:
        current_app.logger.error('rules_ai_suggest unexpected error: %s', e)
        return jsonify({'error': f'Unexpected error: {e}'}), 500

    # Enrich each suggestion with real match counts and example descriptions
    all_transactions = Transaction.query.all()
    enriched = []
    for s in raw_suggestions[:20]:
        cat    = (s.get('category') or '').strip()
        kw     = (s.get('keyword')  or '').strip()
        reason = (s.get('reason')   or '').strip()
        if not cat or not kw:
            continue

        is_regex = kw.startswith('/') and kw.endswith('/') and len(kw) > 2
        total_count = 0
        uncat_count = 0
        examples    = []
        for t in all_transactions:
            try:
                hit = (re.search(kw[1:-1], t.description, re.IGNORECASE)
                       if is_regex else kw.upper() in t.description.upper())
                if hit:
                    total_count += 1
                    if t.category == 'Uncategorized':
                        uncat_count += 1
                    if len(examples) < 3 and t.description not in examples:
                        examples.append(t.description)
            except re.error:
                pass

        enriched.append({
            'category':    cat,
            'keyword':     kw,
            'reason':      reason,
            'total_count': total_count,
            'uncat_count': uncat_count,
            'examples':    examples,
        })

    return jsonify({'suggestions': enriched})

@bp.route('/rules/ai-apply', methods=['POST'])
def ai_apply():
    """Accept one AI suggestion: add the rule and recategorize matching transactions."""
    body     = request.get_json(force=True) or {}
    category = (body.get('category') or '').strip()
    keyword  = (body.get('keyword')  or '').strip()
    if not category or not keyword:
        return jsonify({'error': 'Missing category or keyword'}), 400

    # Add at the TOP of the priority order so an accepted suggestion beats the
    # rules that were miscategorizing those transactions. Persisted through the
    # service: this used to call `add_rule_first` on the engine, which now edits
    # an in-memory copy and would have been discarded at the end of the request.
    if rules_service.add_rule(category, keyword, first=True) is None:
        return jsonify({'ok': True, 'applied_count': 0,
                        'message': 'That rule already exists.'})

    # Re-derive from the whole rule set rather than matching the keyword here.
    # The old version applied the new rule directly to every row it matched,
    # which ignored priority: a transaction claimed by a higher rule was
    # reassigned anyway, so accepting a broad suggestion silently overwrote
    # categories the user had already curated. `add_rule(first=True)` puts this
    # rule at the top, so re-deriving gives it precedence *and* respects the
    # rest of the order.
    engine = rules_service.as_engine()
    count = 0
    try:
        for transaction in Transaction.query.all():
            resolved = engine.get_category(transaction.description)
            if transaction.category != resolved:
                transaction.category = resolved
                count += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'applied_count': count})
