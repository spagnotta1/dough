"""Categorization rules, including the AI suggest/apply pair."""


from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from dough.ai.service import current_ai
from dough.services import auto_categorize, rules_service

from models import Transaction

bp = Blueprint('rules', __name__)

#: The analysis itself lives in `dough/services/auto_categorize.py`. It was
#: inline here until UAT round 1, when the automatic post-sync pass needed to
#: run the identical thing without a request behind it. These names are kept as
#: aliases because tests and the model-picker reference them.
AI_BATCH_SIZE = auto_categorize.BATCH_SIZE
AI_MAX_BATCHES = auto_categorize.MAX_BATCHES

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

        elif action == 'clear_auto':
            # The undo for automatic categorization. Distinct from `clear_all`
            # on purpose: a household that dislikes what Dough decided for them
            # should not have to throw away the rules they wrote themselves to
            # say so. See `rules_service.clear_auto`.
            removed = rules_service.clear_auto()
            # Turning it off is part of the same action, not a second thing to
            # remember. Without it the next sync derives the same rules from
            # the same descriptions and puts them straight back, which makes
            # this button look broken rather than disagreed with.
            auto_categorize.set_enabled(False)
            if removed:
                changed = _recategorize()
                flash(f'Removed the {removed} rule'
                      f'{"" if removed == 1 else "s"} I wrote on my own — '
                      f'{changed} transaction{"" if changed == 1 else "s"} '
                      f'changed. Your own rules are untouched, and I have '
                      f'stopped categorizing on my own.', 'success')
            else:
                flash('I had not written any rules on my own. I have stopped '
                      'categorizing without being asked.', 'info')

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

        elif action == 'set_auto':
            enabled = request.form.get('enabled') == 'on'
            auto_categorize.set_enabled(enabled)
            flash('I will categorize new transactions as they arrive.'
                  if enabled else
                  'I will leave new transactions uncategorized until you ask.',
                  'success')

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

    # Which categories Dough wrote unprompted, so the page can label them and
    # offer to remove exactly those. [UAT round 1] A user has to be able to see
    # what was decided for them; a rule that appeared overnight looking exactly
    # like one they typed is the thing that would make this feature untrustworthy.
    rule_sources = rules_service.sources()

    return render_template('rules.html', rules=rules,
                           rule_stats=rule_stats,
                           rule_sources=rule_sources,
                           auto_rule_count=sum(1 for s in rule_sources.values()
                                               if s == 'ai'),
                           auto_enabled=auto_categorize.is_enabled(),
                           uncategorized_count=uncategorized_count,
                           autostart_ai=autostart_ai)


def _recategorize():
    """`auto_categorize.recategorize()`, with this page's error reporting.

    The re-derivation itself moved to the service so the automatic post-sync
    pass runs the identical thing; what stays here is the only part that is
    about being a web page — turning a failed commit into a flash rather than a
    500. See `dough/services/auto_categorize.py::recategorize` for why the pass
    is whole-ledger and what it costs.
    """
    try:
        return auto_categorize.recategorize()
    except Exception as exc:                           # pragma: no cover
        current_app.logger.error('recategorize failed: %s', exc)
        flash('I could not update your transactions.', 'error')
        return 0

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

    The analysis lives in `dough/services/auto_categorize.py`; this route is the
    HTTP shape of it. It moved there in UAT round 1 so the automatic post-sync
    pass could run the identical thing without a request behind it — the two
    must not drift, because a user who presses Analyze after an automatic pass
    is entitled to the same answer.

    A failure part-way through returns what earlier batches produced rather
    than nothing. Only a failure on the *first* batch is an error response.
    """
    body = request.get_json(force=True) or {}
    analysis = auto_categorize.analyze(model=body.get('model'))

    if analysis.error:
        return jsonify({'error': analysis.error}), (
            503 if analysis.error_is_configuration else 500)
    if not analysis.total_descriptions:
        return jsonify({'suggestions': [],
                        'message': 'No uncategorized transactions found.'})

    # What the analysis actually covered, so the page can say so. A user who
    # has been trained by the old behaviour to press Analyze repeatedly needs
    # to be told that this pass read everything — and told honestly when it
    # did not.
    return jsonify({
        'suggestions': analysis.suggestions,
        'analyzed_descriptions': analysis.analyzed_descriptions,
        'total_descriptions': analysis.total_descriptions,
        # Everything the analysis did not reach, whether because the batch cap
        # cut it off or because a batch failed part-way through.
        'skipped_descriptions': analysis.skipped_descriptions,
        'batches': analysis.batches,
        'partial': analysis.partial,
    })


@bp.route('/rules/ai-apply', methods=['POST'])
def ai_apply():
    """Accept AI suggestions: add the rules, then recategorize once.

    Takes `keywords` (a list) or `keyword` (one), and `categories` (a list of
    `{category, keywords}`) for "Accept all". All three land on the same path.

    Rules accepted here are `source='user'` even though a model proposed them,
    and that is the distinction the column is for: a person read this card and
    pressed the button. Only the unprompted post-sync pass writes `'ai'`.
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

    try:
        added, count = auto_categorize.apply(incoming, source='user')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not added:
        return jsonify({'ok': True, 'applied_count': 0, 'added': 0,
                        'message': 'Those rules already exist.'})
    return jsonify({'ok': True, 'applied_count': count, 'added': added})
