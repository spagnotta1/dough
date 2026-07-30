"""Categorization rules, including the AI suggest/apply pair."""


import re

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.services.categorization import get_category_rules

from models import Transaction, db

bp = Blueprint('rules', __name__)

@bp.route('/rules', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            category = request.form.get('category')
            keyword = request.form.get('keyword')
            get_category_rules().add_rule(category, keyword)
            for transaction in Transaction.query.filter(Transaction.description.ilike(f'%{keyword}%')).all():
                transaction.category = category
            try:
                db.session.commit()
                flash('Rule saved — I went back and recategorised your existing '
                      'transactions to match.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Error updating transactions: {str(e)}', 'error')
        elif action == 'remove':
            category = request.form.get('category')
            keyword = request.form.get('keyword')
            get_category_rules().remove_rule(category, keyword)
            for transaction in Transaction.query.filter(
                Transaction.description.ilike(f'%{keyword}%'),
                Transaction.category == category
            ).all():
                transaction.category = 'Uncategorized'
            try:
                db.session.commit()
                flash('Rule removed — I recategorised the transactions it was '
                      'affecting.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Error updating transactions: {str(e)}', 'error')
        return redirect(url_for('rules.index'))
    rule_stats = {cat: Transaction.query.filter(Transaction.category == cat).count()
                  for cat in get_category_rules().get_all_rules()}
    uncategorized_count = Transaction.query.filter_by(category='Uncategorized').count()
    return render_template('rules.html', rules=get_category_rules().get_all_rules(),
                           rule_stats=rule_stats,
                           uncategorized_count=uncategorized_count)

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
    new_order = (request.json or {}).get('order', [])
    rules_engine = get_category_rules()
    all_rules = rules_engine.get_all_rules()
    reordered = {cat: all_rules[cat] for cat in new_order if cat in all_rules}
    for cat in all_rules:
        if cat not in reordered:
            reordered[cat] = all_rules[cat]
    rules_engine.rules = reordered
    rules_engine._save_rules(reordered)
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

    # Add rule at the TOP of the priority list so it beats all existing rules.
    get_category_rules().add_rule_first(category, keyword)

    is_regex = keyword.startswith('/') and keyword.endswith('/') and len(keyword) > 2
    count    = 0
    try:
        if is_regex:
            pattern = keyword[1:-1]
            # Recategorize ALL matching transactions regardless of current category —
            # the AI rule takes priority over whatever was assigned before.
            for t in Transaction.query.all():
                try:
                    if re.search(pattern, t.description, re.IGNORECASE):
                        t.category = category
                        count += 1
                except re.error:
                    pass
        else:
            txns = Transaction.query.filter(
                Transaction.description.ilike(f'%{keyword}%')
            ).all()
            for t in txns:
                t.category = category
            count = len(txns)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'applied_count': count})
