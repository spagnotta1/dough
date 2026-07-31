"""`/api/v1/chat` — conversations, messages, and the streamed reply.

## Why the streaming endpoint is the one exception to the envelope

`POST /chat/messages` answers `text/event-stream`, not a JSON envelope. That is
a deliberate, documented exception and the only one in v1.

The alternative was to buffer the model's reply and answer in the envelope like
everything else. It would be consistent and it would be wrong: a full answer
takes several seconds to generate, and a client that shows nothing until it
arrives feels broken in a way that no amount of correctness compensates for.
Streaming is the feature, not an implementation detail.

The compromise is that *failures before the stream opens* — no message, unknown
conversation, no API key — are ordinary envelope errors with ordinary status
codes, so a client's normal error path handles every case except a failure
mid-stream. That one arrives as an SSE `error` event, which is the only channel
left once the response has begun.

## Tenancy across the stream boundary

`stream_with_context` preserves the *app* context, not the request's tenant
scope: `teardown_request` has already run by the time the generator saves the
assistant's reply. The household is therefore captured in the request and
re-bound inside the generator. This is the same bug and the same fix as
`dough/blueprints/chat.py`, and it is repeated rather than shared because the
capture has to happen in the request frame — a helper would move it into the
generator's frame, where there is no longer a household to capture.
"""

from __future__ import annotations

from datetime import datetime
import json
import uuid

from flask import Blueprint, Response, current_app, stream_with_context

from dough.api.envelope import created, no_content, ok, pagination_meta
from dough.api.errors import NotFound, ServiceUnavailable, ValidationError
from dough.api.pagination import int_arg, page_request
from dough.api.validation import body, optional_bool, optional_str, require_str
from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.ai.types import TextDelta
from dough.services.finance_context import build_finance_context
from dough.tenancy import find_owned, require_household, tenant_scope

from models import ChatMessage, Conversation, db

bp = Blueprint('api_v1_chat', __name__)

#: Matches `dough/blueprints/chat.py`. Imported rather than redefined would be
#: better, but that module is a blueprint and importing a blueprint from another
#: blueprint is what `dough/blueprints/__init__.py` rule 1 forbids. The values
#: are pinned by `tests/test_api_v1.py` so the two cannot drift silently.
CHAT_HISTORY_LIMIT = 24
CHAT_MAX_TOKENS = 4096

SORTABLE = {'updated_at': Conversation.updated_at,
            'created_at': Conversation.created_at,
            'title': Conversation.title}


def _serialize_conversation(conversation):
    return {
        'id': conversation.id,
        'title': conversation.title,
        'created_at': conversation.created_at.isoformat() + 'Z',
        'updated_at': conversation.updated_at.isoformat() + 'Z',
    }


def _serialize_message(message):
    return {
        'id': message.id,
        'role': message.role,
        'content': message.content,
        # Marked as UTC explicitly. The column holds naive `utcnow()` values, so
        # a client parsing the bare ISO string would read them as local time and
        # show every message shifted by its own offset.
        'created_at': message.created_at.isoformat() + 'Z',
    }


@bp.route('/chat/conversations', methods=['GET'])
def list_conversations():
    page = page_request(sortable=SORTABLE, default_sort='updated_at')
    from dough.api.pagination import apply_ordering

    query = Conversation.query
    total = query.count()
    rows = (apply_ordering(query, page, SORTABLE)
            .limit(page.page_size).offset(page.offset).all())
    return ok([_serialize_conversation(c) for c in rows],
              pagination=pagination_meta(page.page, page.page_size, total))


@bp.route('/chat/conversations', methods=['POST'])
def create_conversation():
    conversation = Conversation(
        id=str(uuid.uuid4()),
        title=optional_str(body(), 'title', max_length=80) or 'New Chat')
    db.session.add(conversation)
    db.session.commit()
    return created(_serialize_conversation(conversation),
                   location=f'/api/v1/chat/conversations/{conversation.id}')


@bp.route('/chat/conversations/<conversation_id>', methods=['GET'])
def get_conversation(conversation_id):
    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')
    return ok(_serialize_conversation(conversation))


@bp.route('/chat/conversations/<conversation_id>', methods=['PATCH'])
def rename_conversation(conversation_id):
    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')
    conversation.title = require_str(body(), 'title', max_length=80,
                                     allow_empty=True) or 'New Chat'
    db.session.commit()
    return ok(_serialize_conversation(conversation))


@bp.route('/chat/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')
    ChatMessage.query.filter_by(session_id=conversation_id).delete()
    db.session.delete(conversation)
    db.session.commit()
    return no_content()


@bp.route('/chat/conversations/<conversation_id>/messages', methods=['GET'])
def list_messages(conversation_id):
    """Every message in a conversation, oldest first.

    Ordered by `id`, not `created_at`. Both messages of a turn are often written
    in the same commit and share a timestamp to the microsecond, so ordering by
    time puts the assistant's reply before the question roughly half the time.
    `id` is insertion-ordered and cannot tie.
    """
    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')

    limit = int_arg('limit', default=200, minimum=1, maximum=1000)
    messages = (ChatMessage.query.filter_by(session_id=conversation_id)
                .order_by(ChatMessage.id.asc()).limit(limit).all())
    return ok([_serialize_message(m) for m in messages])


@bp.route('/chat/conversations/<conversation_id>/messages', methods=['DELETE'])
def clear_messages(conversation_id):
    """Empty a conversation without deleting it.

    `?keep=N` truncates from the Nth message onwards instead, which is what the
    two rewind actions in the web UI need — regenerating a reply keeps
    everything up to the last user turn, and editing an earlier prompt keeps
    everything before it.
    """
    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')

    keep = int_arg('keep', default=0, minimum=0)
    messages = (ChatMessage.query.filter_by(session_id=conversation_id)
                .order_by(ChatMessage.id.asc()).all())
    for message in messages[keep:]:
        db.session.delete(message)
    if keep == 0:
        conversation.title = 'New Chat'
    conversation.updated_at = datetime.utcnow()
    db.session.commit()
    return ok({'conversation_id': conversation_id,
               'remaining': min(keep, len(messages))})


@bp.route('/chat/conversations/<conversation_id>/messages', methods=['POST'])
def send_message(conversation_id):
    """Ask a question and stream the reply as `text/event-stream`.

    Every refusal that can be decided *before* the stream opens is an ordinary
    envelope error — see the module docstring. Once the response has begun the
    only channel left is an SSE `error` event, which is what the generator
    emits.

    Wire format, so a client can be written against it:

        data: {"delta": "..."}     zero or more, in order
        data: {"error": "..."}     at most one, terminal
        data: [DONE]               on successful completion only
    """
    data = body()
    resend = optional_bool(data, 'resend') is True
    message_text = optional_str(data, 'message', max_length=8000) or ''
    if not message_text and not resend:
        raise ValidationError('A message is required.',
                              details={'message': 'This field is required.'})

    conversation = find_owned(Conversation, conversation_id)
    if conversation is None:
        raise NotFound('No such conversation.')

    ai = current_ai()
    if not ai.is_available:
        raise ServiceUnavailable(AIConfigurationError().user_message)

    # History is read before the new message is stored, so the model is not sent
    # the question twice.
    recent = (ChatMessage.query.filter_by(session_id=conversation_id)
              .order_by(ChatMessage.id.desc())
              .limit(CHAT_HISTORY_LIMIT).all())
    history = [{'role': m.role, 'content': m.content} for m in reversed(recent)]

    if resend:
        if not history or history[-1]['role'] != 'user':
            raise ValidationError('There is nothing to regenerate.',
                                  details={'resend': 'The last stored message '
                                                     'is not a question.'})
        messages = history
    else:
        # Persisted immediately so the question survives the client navigating
        # away or losing the connection mid-answer.
        now = datetime.utcnow()
        db.session.add(ChatMessage(session_id=conversation_id, role='user',
                                   content=message_text, created_at=now))
        if conversation.title == 'New Chat':
            conversation.title = message_text[:55] + (
                '…' if len(message_text) > 55 else '')
        conversation.updated_at = now
        db.session.commit()
        messages = history + [{'role': 'user', 'content': message_text}]

    system_prompt = (
        persona.DOUGH_PERSONA + '\n\n'
        + persona.CHAT_GUIDANCE
        + f'{json.dumps(build_finance_context(detail=True), indent=2, default=str)}\n\n'
        + persona.CHAT_RULES)

    model = optional_str(data, 'model', max_length=80) or None

    # Captured in the request frame -- see the module docstring. By the time the
    # generator runs, `teardown_request` has released the request's scope.
    streaming_household = require_household()

    def _generate():
        with tenant_scope(streaming_household):
            yield from _stream(ai, messages, system_prompt, model,
                               conversation_id)

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'})


def _stream(ai, messages, system_prompt, model, conversation_id):
    """Emit deltas, then persist whatever was generated.

    The `finally` runs on normal completion, on error, *and* on client
    disconnect (which arrives as `GeneratorExit`). That is why the save is there
    rather than after the loop: a person who closes the app halfway through an
    answer should still find it waiting when they come back.
    """
    full = ''
    completed = False
    try:
        for event in ai.stream(messages=messages, system=system_prompt,
                               model=model, role='ask',
                               max_tokens=CHAT_MAX_TOKENS, cache_system=True,
                               metadata={'surface': 'api_v1_chat'}):
            if isinstance(event, TextDelta):
                full += event.text
                yield f'data: {json.dumps({"delta": event.text})}\n\n'
        completed = True
    except AIError as exc:
        current_app.logger.warning('api chat stream failed: %s', exc)
        yield f'data: {json.dumps({"error": exc.user_message})}\n\n'
        return
    except Exception as exc:
        current_app.logger.error('api chat stream failed: %s', exc)
        failure = json.dumps({'error': 'The reply could not be completed.'})
        yield f'data: {failure}\n\n'
        return
    finally:
        if full:
            try:
                now = datetime.utcnow()
                db.session.add(ChatMessage(session_id=conversation_id,
                                           role='assistant', content=full,
                                           created_at=now))
                conversation = find_owned(Conversation, conversation_id)
                if conversation:
                    conversation.updated_at = now
                db.session.commit()
            except Exception as exc:
                current_app.logger.error('api chat save failed: %s', exc)
                db.session.rollback()

    if completed:
        yield 'data: [DONE]\n\n'
