"""Ask Dough: the conversational surface, plus the two generated summaries
that speak in the same voice (the dashboard insight and the copilot)."""

from datetime import datetime
import json
import uuid

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request, stream_with_context)

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.formatting import md_to_html
from dough.ai.service import current_ai, extract_json_object as ai_extract_json
from dough.ai.types import TextDelta
from dough.services.finance_context import (build_finance_context,
                                            copilot_context)
from dough.tenancy import find_owned, require_household, tenant_scope

from models import ChatMessage, Conversation, db

bp = Blueprint('chat', __name__)

# How much of a conversation is replayed, and the reply ceiling. These sat in
# app.py next to the routes that read them; they are chat policy, so they came
# here rather than to dough/ai/, which knows nothing about conversations.
CHAT_HISTORY_LIMIT = 24     # prior messages replayed to the model
CHAT_MAX_TOKENS    = 4096

@bp.route('/chat')
def index():
    return render_template('chat.html')

@bp.route('/api/chat', methods=['POST'])
def api_chat():
    req = request.get_json(force=True)
    user_message = (req.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400
    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured.'}), 503

    # Built once. This used to be assigned to an unused `context` and then
    # rebuilt inline for the message, running the whole snapshot -- dozens of
    # queries -- twice per request. Same output, half the work.
    context = build_finance_context()
    try:
        response = ai.generate(
            messages=[{
                'role': 'user',
                'content': f"Financial data:\n{json.dumps(context, indent=2)}\n\nQuestion: {user_message}"
            }],
            system=persona.CHAT_JSON_SYSTEM,
            role='analysis', max_tokens=1500,
            metadata={'surface': 'api_chat'})
    except AIError as e:
        current_app.logger.warning('api_chat failed: %s', e)
        return jsonify({'error': e.user_message}), 502

    # Parse JSON — completely isolated from HTML conversion. This surface
    # keeps its lenient fallback rather than using generate_json(): a reply
    # that is prose instead of JSON is still shown to the reader as prose,
    # which is better than an error for a free-text analysis view.
    raw = ai_extract_json(response.text)
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        result = {'analysis': raw, 'insights': [], 'recommended_actions': []}
    if not isinstance(result, dict):
        result = {'analysis': raw, 'insights': [], 'recommended_actions': []}

    # Unwrap double-encoded: Claude occasionally nests the full JSON inside analysis
    analysis_text = result.get('analysis', '')
    if isinstance(analysis_text, str) and analysis_text.strip().startswith('{'):
        try:
            inner = json.loads(analysis_text)
            if isinstance(inner.get('analysis'), str):
                result = inner
                analysis_text = inner['analysis']
        except Exception:
            pass

    # Convert markdown to HTML — fully isolated, never causes a JSON fallback
    try:
        html = md_to_html(analysis_text) if analysis_text else ''
    except Exception:
        html = '<pre style="white-space:pre-wrap;font-size:.85rem">' + \
               analysis_text.replace('&', '&amp;').replace('<', '&lt;') + '</pre>'

    return jsonify({
        'html': html,
        'insights': result.get('insights', []),
        'actions': result.get('recommended_actions', []),
    })

# ── Conversation management ──────────────────────────────────────────────

@bp.route('/api/conversations', methods=['GET'])
def api_conversations():
    convs = (Conversation.query
             .order_by(Conversation.updated_at.desc())
             .all())
    return jsonify({'conversations': [
        {'id': c.id, 'title': c.title, 'updated_at': c.updated_at.isoformat()}
        for c in convs
    ]})

@bp.route('/api/conversations', methods=['POST'])
def api_new_conversation():
    conv = Conversation(id=str(uuid.uuid4()), title='New Chat')
    db.session.add(conv)
    db.session.commit()
    return jsonify({'id': conv.id, 'title': conv.title})

@bp.route('/api/conversations/<conv_id>', methods=['PATCH'])
def api_rename_conversation(conv_id):
    """Rename a conversation. Empty titles fall back to 'New Chat'."""
    req   = request.get_json(force=True) or {}
    title = (req.get('title') or '').strip()[:120] or 'New Chat'
    conv  = find_owned(Conversation, conv_id)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    try:
        conv.title = title
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Could not rename conversation'}), 500
    return jsonify({'id': conv.id, 'title': conv.title})

@bp.route('/api/conversations/<conv_id>', methods=['DELETE'])
def api_delete_conversation(conv_id):
    try:
        ChatMessage.query.filter_by(session_id=conv_id).delete()
        Conversation.query.filter_by(id=conv_id).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({'ok': True})

# ── Chat messages ────────────────────────────────────────────────────────

@bp.route('/api/chat_history')
def api_chat_history():
    conv_id = request.args.get('conv', '').strip()
    if not conv_id:
        return jsonify({'messages': []})
    msgs = (ChatMessage.query
            .filter_by(session_id=conv_id)
            .order_by(ChatMessage.id.asc())   # id is insertion-ordered; created_at ties when saved in one commit
            .all())
    return jsonify({'messages': [
        {'role': m.role, 'content': m.content,
         'created_at': m.created_at.isoformat() + 'Z'}   # mark as UTC so JS Date parses correctly
        for m in msgs
    ]})

@bp.route('/api/chat_truncate', methods=['POST'])
def api_chat_truncate():
    """Drop every message from ``keep`` onwards.

    Backs the two rewind actions in the UI: regenerating a reply (keep all
    messages up to and including the last user turn) and editing an earlier
    prompt (keep everything before it). Returns the surviving count so the
    client can reconcile its local cache.
    """
    req     = request.get_json(force=True) or {}
    conv_id = (req.get('conv_id') or '').strip()
    keep    = req.get('keep')
    if not conv_id:
        return jsonify({'error': 'No conversation ID'}), 400
    try:
        keep = max(0, int(keep))
    except (TypeError, ValueError):
        return jsonify({'error': 'keep must be an integer'}), 400

    msgs = (ChatMessage.query
            .filter_by(session_id=conv_id)
            .order_by(ChatMessage.id.asc()).all())
    try:
        for m in msgs[keep:]:
            db.session.delete(m)
        db.session.commit()
    except Exception as e:
        current_app.logger.error('chat_truncate failed: %s', e)
        db.session.rollback()
        return jsonify({'error': 'Could not update conversation'}), 500
    return jsonify({'ok': True, 'remaining': min(keep, len(msgs))})

# The _ALLOWED_MODELS set that used to live here is gone: dough/ai/catalog.py
# is the allow-list now, and resolve() falls back to the default for an
# unknown id exactly as this did.

@bp.route('/api/chat_stream', methods=['POST'])
def api_chat_stream():
    req = request.get_json(force=True)
    user_message = (req.get('message') or '').strip()
    conv_id       = (req.get('conv_id') or '').strip()
    model         = req.get('model')
    # `resend` replays the conversation as it already stands — used by
    # "regenerate", where the trailing assistant reply has been truncated
    # away and the last stored message is the user's prompt.
    resend        = bool(req.get('resend'))
    if not user_message and not resend:
        return jsonify({'error': 'No message provided'}), 400
    if not conv_id:
        return jsonify({'error': 'No conversation ID'}), 400

    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': AIConfigurationError().user_message}), 503

    conv = find_owned(Conversation, conv_id)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404

    # ── Build Claude context from history BEFORE saving the current message ──
    recent = (ChatMessage.query
              .filter_by(session_id=conv_id)
              .order_by(ChatMessage.id.desc())
              .limit(CHAT_HISTORY_LIMIT).all())
    history = [{'role': m.role, 'content': m.content} for m in reversed(recent)]

    if resend:
        # Nothing new to persist; the stored history already ends with the
        # user turn we want answered.
        if not history or history[-1]['role'] != 'user':
            return jsonify({'error': 'Nothing to regenerate'}), 400
        messages = history
    else:
        # ── Persist user message immediately so it survives navigation / disconnect ──
        user_ts = datetime.utcnow()
        try:
            db.session.add(ChatMessage(session_id=conv_id, role='user',
                                       content=user_message, created_at=user_ts))
            if conv.title == 'New Chat':
                conv.title = user_message[:55] + ('…' if len(user_message) > 55 else '')
            conv.updated_at = user_ts
            db.session.commit()
        except Exception as e:
            current_app.logger.error('chat_stream pre-save user msg failed: %s', e)
            db.session.rollback()
        messages = history + [{'role': 'user', 'content': user_message}]

    fin_ctx = build_finance_context(detail=True)
    system_prompt = (
        persona.DOUGH_PERSONA + "\n\n"
        + persona.CHAT_GUIDANCE
        + f"{json.dumps(fin_ctx, indent=2, default=str)}\n\n"
        + persona.CHAT_RULES
    )

    # Captured here, in the request, and re-bound inside the generator
    # below. The generator body runs while the response is being streamed,
    # which is *after* teardown_request has already reset the request's
    # tenant scope — so by the time the `finally` clause saves the
    # assistant's reply there is no household bound any more, and the save
    # would raise TenantContextMissing into a response already half sent.
    # stream_with_context preserves the *app* context, not this one.
    streaming_household = require_household()

    def _generate():
        with tenant_scope(streaming_household):
            yield from _stream_body()

    def _stream_body():
        full_response = ''
        stream_done   = False
        try:
            # cache_system: the snapshot is byte-identical for every turn in
            # a session, so later messages read the prefix at the cache rate
            # rather than as fresh input -- measured at 24,632 tokens read
            # from cache on a repeat call. The adapter renders the provider's
            # cache_control block; this route no longer knows the wire format.
            for event in ai.stream(
                    messages=messages, system=system_prompt, model=model,
                    role='ask', max_tokens=CHAT_MAX_TOKENS, cache_system=True,
                    metadata={'surface': 'chat_stream'}):
                if isinstance(event, TextDelta):
                    full_response += event.text
                    yield f'data: {json.dumps({"delta": event.text})}\n\n'
            stream_done = True
        except AIError as e:
            current_app.logger.warning('chat_stream failed: %s', e)
            yield f'data: {json.dumps({"error": e.user_message})}\n\n'
            return
        except Exception as e:
            current_app.logger.error('chat_stream unexpected error: %s', e)
            yield f'data: {json.dumps({"error": "I lost my train of thought there. Ask me again?"})}\n\n'
            return
        finally:
            # Runs on normal completion, errors, AND client disconnect (GeneratorExit).
            # Saves whatever response accumulated so nothing is silently lost.
            if full_response:
                try:
                    asst_ts = datetime.utcnow()
                    db.session.add(ChatMessage(session_id=conv_id, role='assistant',
                                               content=full_response, created_at=asst_ts))
                    c = find_owned(Conversation, conv_id)
                    if c:
                        c.updated_at = asst_ts
                    db.session.commit()
                except Exception as e:
                    current_app.logger.error('chat_stream save assistant failed: %s', e)
                    db.session.rollback()

        if stream_done:
            yield 'data: [DONE]\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )

@bp.route('/api/chat_clear', methods=['POST'])
def api_chat_clear():
    """Clear messages for a conversation without deleting it."""
    req     = request.get_json(force=True) or {}
    conv_id = (req.get('conv_id') or '').strip()
    if conv_id:
        try:
            ChatMessage.query.filter_by(session_id=conv_id).delete()
            conv = find_owned(Conversation, conv_id)
            if conv:
                conv.title      = 'New Chat'
                conv.updated_at = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
    return jsonify({'ok': True})

@bp.route('/api/dashboard-insight')
def dashboard_insight():
    ai = current_ai()
    if not ai.is_available:
        return jsonify({'insight': ''})

    def produce():
        # Inside the producer so a cache hit skips the snapshot too, not
        # just the model call. The old code built it before checking.
        context = build_finance_context(months=1)
        resp = ai.generate(
            messages=[{'role': 'user', 'content': json.dumps(context)}],
            system=persona.INSIGHT_STYLE, role='insight', max_tokens=200,
            metadata={'surface': 'dashboard_insight'})
        return resp.text.strip() or None

    try:
        return jsonify({'insight': ai.cached('dashboard_insight', produce) or ''})
    except AIError as e:
        # This card is optional furniture; a failure renders nothing rather
        # than an error, exactly as the bare `except Exception` did before.
        current_app.logger.info('dashboard_insight unavailable: %s', e)
        return jsonify({'insight': ''})
    except Exception as e:
        current_app.logger.error('dashboard_insight failed: %s', e)
        return jsonify({'insight': ''})

@bp.route('/api/copilot/brief')
def copilot_brief():
    """A short written read on the month, plus concrete opportunities.

    The dashboard already renders the hard numbers server-side, so this
    endpoint exists only for the parts that need judgement: how the month
    is actually going, and what is worth doing about it.
    """
    def _parse(name):
        raw = request.args.get(name)
        try:
            return datetime.strptime(raw, '%Y-%m-%d') if raw else None
        except ValueError:
            return None

    start, end = _parse('start'), _parse('end')
    # Cache per window: a briefing about March–June must not be served to
    # a reader who has since switched the dashboard to this month. The
    # window is the CacheKey variant; the scope is the tenancy boundary
    # Phase 5 will fill in.
    variant = f"{start:%Y-%m-%d}|{end:%Y-%m-%d}" if start and end else 'default'

    ai = current_ai()
    if not ai.is_available:
        return jsonify({'available': False})

    def produce():
        data, _ = ai.generate_json(
            messages=[{'role': 'user',
                       'content': json.dumps(copilot_context(start, end), default=str)}],
            system=persona.COPILOT_STYLE + "\n\n" + persona.COPILOT_BRIEF_FORMAT,
            role='brief', max_tokens=700,
            metadata={'surface': 'copilot_brief'})
        data['available'] = True
        data.setdefault('opportunities', [])
        data.setdefault('questions', [])
        return data

    try:
        return jsonify(ai.cached('copilot_brief', produce, variant=variant))
    except AIError as e:
        current_app.logger.warning('copilot_brief unavailable: %s', e)
        return jsonify({'available': False})
    except Exception as e:
        current_app.logger.error('copilot_brief failed: %s', e)
        return jsonify({'available': False})

@bp.route('/api/copilot/ask', methods=['POST'])
def copilot_ask():
    """Answer one dashboard question, streamed, without touching chat history.

    Deliberately stateless: the copilot card is for a quick question in
    passing. Anything that wants to become a conversation has a "continue
    in chat" path into /chat, which is where history belongs.
    """
    req = request.get_json(force=True) or {}
    question = (req.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    if len(question) > 500:
        question = question[:500]

    def _parse(name):
        try:
            raw = req.get(name)
            return datetime.strptime(raw, '%Y-%m-%d') if raw else None
        except (ValueError, TypeError):
            return None

    ai = current_ai()
    if not ai.is_available:
        return jsonify({'error': AIConfigurationError().user_message}), 503

    system_prompt = (
        persona.COPILOT_STYLE + "\n\n"
        f"{json.dumps(copilot_context(_parse('start'), _parse('end')), indent=2, default=str)}\n\n"
        + persona.COPILOT_ASK_RULES
    )

    def _generate():
        try:
            for chunk in ai.stream_text(
                    messages=[{'role': 'user', 'content': question}],
                    system=system_prompt, role='ask', max_tokens=600,
                    cache_system=True,
                    metadata={'surface': 'copilot_ask'}):
                yield f'data: {json.dumps({"delta": chunk})}\n\n'
            yield 'data: [DONE]\n\n'
        except AIError as e:
            current_app.logger.warning('copilot_ask failed: %s', e)
            yield f'data: {json.dumps({"error": e.user_message})}\n\n'
        except Exception as e:
            current_app.logger.error('copilot_ask failed: %s', e)
            yield f'data: {json.dumps({"error": "Something went wrong on my end. Ask me again?"})}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'},
    )
