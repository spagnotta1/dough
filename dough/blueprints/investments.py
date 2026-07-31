"""The wealth dashboard, its two generated briefings, and manual holdings."""

import json

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request, stream_with_context)
from sqlalchemy import func
from werkzeug.exceptions import HTTPException

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.services import holdings as holdings_service
from dough.services.finance_context import wealth_context
from dough.services.networth import wealth_snapshot

import investments_intel
from models import FinancialAccount, InstitutionConnection, db

bp = Blueprint('investments', __name__)

@bp.route('/investments')
def index():
    benchmark = request.args.get('benchmark', 'sp500')
    if benchmark not in investments_intel.BENCHMARKS:
        benchmark = 'sp500'
    try:
        horizon = max(1, min(40, int(request.args.get('horizon', 10))))
    except (TypeError, ValueError):
        horizon = 10
    try:
        contribution = max(0.0, float(request.args.get('contribution', 0)))
    except (TypeError, ValueError):
        contribution = 0.0

    snap = wealth_snapshot(benchmark, horizon, contribution)
    nw = snap['nw']
    holdings = holdings_service.list_holdings()

    # The original donut mixed cash accounts in with the holdings; keep
    # that view intact, it answers a different question from the pure
    # portfolio allocation below it.
    portfolio_by_class = ({'Checking': nw['checking'], 'Savings': nw['savings']}
                          if (nw['checking'] or nw['savings']) else {})
    for h in holdings:
        portfolio_by_class[h.asset_class] = round(
            portfolio_by_class.get(h.asset_class, 0) + float(h.current_value), 2)
    portfolio_by_class = {k: v for k, v in portfolio_by_class.items() if v > 0}
    asset_classes = ['Stock', 'ETF', 'Mutual Fund', 'Bond', 'Crypto', 'Cash', 'Other']

    # --- synchronization context (finance_sync) ---
    connections = [c.to_dict() for c in InstitutionConnection.query
                   .order_by(InstitutionConnection.display_name).all()]
    synced_accounts = [a.to_dict() for a in
                       FinancialAccount.query.filter_by(is_active=True)
                       .order_by(FinancialAccount.account_type,
                                 FinancialAccount.name).all()]
    cash_synced = any(a['account_type'] in ('checking', 'savings')
                      for a in synced_accounts)
    last_sync = (db.session.query(func.max(InstitutionConnection.last_sync_at))
                 .scalar())
    investment_accounts = [a for a in synced_accounts
                           if a['account_type'] in ('brokerage', 'crypto')]

    return render_template(
        'investments.html',
        holdings=holdings, nw=nw,
        portfolio_by_class=portfolio_by_class,
        asset_classes=asset_classes,
        connections=connections,
        synced_accounts=synced_accounts,
        cash_synced=cash_synced,
        last_sync=last_sync.strftime('%Y-%m-%d %H:%M') if last_sync else None,
        wealth=snap,
        accounts=investments_intel.account_rollup(
            snap['positions'], investment_accounts, connections),
        benchmarks=investments_intel.BENCHMARKS,
        benchmark_key=benchmark,
        horizon=horizon,
        contribution=contribution,
    )

# ── Wealth copilot ──────────────────────────────────────────────────────
# Same split as the dashboard copilot: the page renders every hard number
# server-side, and the model is asked only for the parts that need
# judgement. Its context is the identical snapshot the page drew from, so
# it can never narrate figures the reader cannot see.

@bp.route('/api/investments/brief')
def wealth_brief():
    """A written read on the portfolio, plus concrete moves worth considering."""
    ai = current_ai()
    if not ai.is_available:
        return jsonify({'available': False})

    def produce():
        data, _ = ai.generate_json(
            messages=[{'role': 'user',
                       'content': json.dumps(wealth_context(), default=str)}],
            system=persona.WEALTH_STYLE + "\n\n" + persona.WEALTH_BRIEF_FORMAT,
            role='brief', max_tokens=900,
            metadata={'surface': 'wealth_brief'})
        data['available'] = True
        data.setdefault('opportunities', [])
        data.setdefault('questions', [])
        return data

    try:
        return jsonify(ai.cached('wealth_brief', produce))
    except AIError as e:
        current_app.logger.warning('wealth_brief unavailable: %s', e)
        return jsonify({'available': False})
    except Exception as e:
        current_app.logger.error('wealth_brief failed: %s', e)
        return jsonify({'available': False})

@bp.route('/api/investments/ask', methods=['POST'])
def wealth_ask():
    """Answer one portfolio question, streamed, with follow-up context.

    Unlike the dashboard copilot this one accepts a short prior turn list,
    because "should I rebalance?" is rarely the last thing someone wants
    to ask. History stays client-side and capped — anything that wants to
    become a real conversation has a path into /chat.
    """
    req = request.get_json(force=True) or {}
    question = (req.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    question = question[:500]

    history = []
    for turn in (req.get('history') or [])[-6:]:
        role = turn.get('role')
        content = (turn.get('content') or '').strip()[:2000]
        if role in ('user', 'assistant') and content:
            history.append({'role': role, 'content': content})
    # A trailing assistant turn would leave two assistant messages in a row
    # once the new question is appended below.
    while history and history[-1]['role'] == 'assistant' and len(history) % 2 == 0:
        history.pop()

    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': AIConfigurationError().user_message}), 503

    system_prompt = (
        persona.WEALTH_STYLE + "\n\n"
        f"{json.dumps(wealth_context(), indent=2, default=str)}\n\n"
        + persona.WEALTH_ASK_RULES
    )

    messages = history + [{'role': 'user', 'content': question}]

    def _generate():
        try:
            for chunk in ai.stream_text(
                    messages=messages, system=system_prompt, role='ask',
                    max_tokens=800, cache_system=True,
                    metadata={'surface': 'wealth_ask'}):
                yield f'data: {json.dumps({"delta": chunk})}\n\n'
            yield 'data: [DONE]\n\n'
        except AIError as e:
            current_app.logger.warning('wealth_ask failed: %s', e)
            yield f'data: {json.dumps({"error": e.user_message})}\n\n'
        except Exception as e:
            current_app.logger.error('wealth_ask failed: %s', e)
            yield f'data: {json.dumps({"error": "Something went wrong on my end. Ask me again?"})}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'},
    )

@bp.route('/api/holdings', methods=['POST'])
def add_holding():
    d = request.get_json(force=True)
    try:
        h = holdings_service.create_holding(
            ticker=d.get('ticker', ''),
            name=d.get('name', ''),
            shares=d.get('shares', 0),
            current_value=d.get('current_value', 0),
            asset_class=d.get('asset_class', 'Stock'),
            account_name=d.get('account_name', 'Brokerage'),
        )
        return jsonify(h.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@bp.route('/api/holdings/<int:hid>', methods=['PUT', 'DELETE'])
def holding(hid):
    # The synced-holding refusal is the service's now, so `/api/v1/holdings`
    # gives the identical answer rather than a second wording of the same rule.
    #
    # `except HTTPException: raise` for the reason spelled out in
    # dough/blueprints/transactions.py: the service resolves the id with
    # `get_owned`, which refuses a foreign row by raising NotFound, and a bare
    # `except Exception` below would turn that 404 into a 400 with the
    # authorization failure reduced to a message string.
    try:
        if request.method == 'DELETE':
            holdings_service.delete_holding(hid)
            return jsonify({'ok': True})
        h = holdings_service.update_holding(hid, request.get_json(force=True))
        return jsonify(h.to_dict())
    except holdings_service.SyncedHoldingError as e:
        return jsonify({'error': str(e)}), 409
    except HTTPException:
        raise
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
