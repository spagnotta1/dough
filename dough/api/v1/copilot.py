"""`/api/v1/copilot` — the generated briefings and the one-shot questions.

Four surfaces, two shapes. The briefings (`/brief`, `/investments/brief`) are
cached JSON. The questions (`/ask`, `/investments/ask`) stream, for the reason
`chat.py` states: an answer takes seconds and a client showing nothing until it
lands feels broken.

## `available: false` is a field, not an error

The web routes answer `{'available': false}` with a 200 when the AI is not
configured, and this API keeps that rather than raising 503. The difference is
who is asking and what they can do about it. A briefing is optional furniture on
a dashboard: a client should render the rest of the page and omit the card. A
503 would make a perfectly healthy dashboard look degraded because an optional
feature is switched off, and a client would have to special-case that status to
tell "no API key" from "the server is broken".

The `/ask` endpoints do raise 503, and the asymmetry is deliberate: somebody who
typed a question and pressed send has made a request that cannot be satisfied,
and telling them nothing is worse than telling them the feature is unavailable.

## The model never sees a figure the client cannot

Both briefings are built from the same context functions the web routes use —
`copilot_context` and `wealth_context`, over `wealth_snapshot`. That is what
stops the model narrating a number no page displays, and it is the reason these
routes assemble no context of their own.
"""

from __future__ import annotations

from datetime import datetime
import json

from flask import Blueprint, Response, current_app, stream_with_context

from dough.api.envelope import ok
from dough.api.errors import ServiceUnavailable, ValidationError
from dough.api.pagination import date_arg
from dough.api.validation import body, optional_date, require_str
from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError
from dough.ai.service import current_ai
from dough.services.finance_context import copilot_context, wealth_context

bp = Blueprint('api_v1_copilot', __name__)

#: How much prior conversation the wealth question accepts. Matches the web
#: route: enough that "should I rebalance?" can be followed up, short enough
#: that this stays a passing question rather than becoming an untracked chat.
MAX_HISTORY_TURNS = 6
MAX_QUESTION_CHARS = 500


@bp.route('/copilot/brief', methods=['GET'])
def brief():
    """A written read on the month, plus concrete opportunities.

    Cached per window. A briefing about March–June must not be served to a
    caller who has since asked about this month, which is what the `variant`
    keys on — and the scope on every cache key is the household, which is what
    closes SEC-0003.
    """
    start = date_arg('start')
    end = date_arg('end')
    variant = f'{start}|{end}' if start and end else 'default'

    ai = current_ai()
    if not ai.is_available:
        return ok({'available': False})

    def produce():
        # Built inside the producer so a cache hit skips the snapshot too, not
        # just the model call.
        data, _ = ai.generate_json(
            messages=[{'role': 'user',
                       'content': json.dumps(
                           copilot_context(_as_datetime(start),
                                           _as_datetime(end)), default=str)}],
            system=persona.COPILOT_STYLE + '\n\n' + persona.COPILOT_BRIEF_FORMAT,
            role='brief', max_tokens=700,
            metadata={'surface': 'api_v1_copilot_brief'})
        data['available'] = True
        data.setdefault('opportunities', [])
        data.setdefault('questions', [])
        return data

    return ok(_cached_or_unavailable(ai, 'copilot_brief', produce,
                                     variant=variant))


@bp.route('/copilot/investments/brief', methods=['GET'])
def investments_brief():
    """A written read on the portfolio, plus moves worth considering."""
    ai = current_ai()
    if not ai.is_available:
        return ok({'available': False})

    def produce():
        data, _ = ai.generate_json(
            messages=[{'role': 'user',
                       'content': json.dumps(wealth_context(), default=str)}],
            system=persona.WEALTH_STYLE + '\n\n' + persona.WEALTH_BRIEF_FORMAT,
            role='brief', max_tokens=900,
            metadata={'surface': 'api_v1_wealth_brief'})
        data['available'] = True
        data.setdefault('opportunities', [])
        data.setdefault('questions', [])
        return data

    return ok(_cached_or_unavailable(ai, 'wealth_brief', produce))


def _cached_or_unavailable(ai, surface, produce, variant=None):
    """Run a cached producer, degrading to `available: false` on any failure.

    A briefing is optional furniture -- see the module docstring. The failure is
    logged with its reason so it is diagnosable; the client is told only that
    the card has nothing to show, which is all it can act on.
    """
    try:
        if variant is not None:
            return ai.cached(surface, produce, variant=variant)
        return ai.cached(surface, produce)
    except AIError as exc:
        current_app.logger.warning('%s unavailable: %s', surface, exc)
        return {'available': False}
    except Exception as exc:
        current_app.logger.error('%s failed: %s', surface, exc)
        return {'available': False}


def _as_datetime(value):
    """A `date` from the query string as the `datetime` the context wants."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


@bp.route('/copilot/ask', methods=['POST'])
def ask():
    """Answer one dashboard question, streamed.

    Stateless by design: the copilot is for a question in passing, and anything
    that wants to become a conversation belongs in `/chat`, which is where
    history is kept. Wire format is identical to the chat stream, so a client
    writes one SSE reader.
    """
    data = body()
    question = _question(data)
    ai = _available_ai()

    system_prompt = (
        persona.COPILOT_STYLE + '\n\n'
        + json.dumps(copilot_context(_as_datetime(optional_date(data, 'start')
                                                  or None),
                                     _as_datetime(optional_date(data, 'end')
                                                  or None)),
                     indent=2, default=str) + '\n\n'
        + persona.COPILOT_ASK_RULES)

    return _stream_answer(ai, [{'role': 'user', 'content': question}],
                          system_prompt, max_tokens=600,
                          surface='api_v1_copilot_ask')


@bp.route('/copilot/investments/ask', methods=['POST'])
def investments_ask():
    """Answer one portfolio question, streamed, with a short prior context.

    Unlike the dashboard copilot this one accepts recent turns, because "should
    I rebalance?" is rarely the last thing anybody wants to ask. History stays
    with the client and is capped here — anything wanting to be a real
    conversation has a path into `/chat`.
    """
    data = body()
    question = _question(data)
    ai = _available_ai()

    system_prompt = (
        persona.WEALTH_STYLE + '\n\n'
        + json.dumps(wealth_context(), indent=2, default=str) + '\n\n'
        + persona.WEALTH_ASK_RULES)

    messages = _history(data) + [{'role': 'user', 'content': question}]
    return _stream_answer(ai, messages, system_prompt, max_tokens=800,
                          surface='api_v1_wealth_ask')


def _question(data):
    question = require_str(data, 'question', max_length=MAX_QUESTION_CHARS,
                           allow_empty=False)
    return question


def _available_ai():
    ai = current_ai()
    if not ai.is_available:
        # 503 here, unlike the briefings. Somebody typed a question and pressed
        # send; there is no version of this response that is useful furniture.
        raise ServiceUnavailable(AIConfigurationError().user_message)
    return ai


def _history(data):
    """The caller's prior turns, validated, capped and left well-formed.

    The trailing-assistant trim is not cosmetic: appending the new question to a
    history ending in an assistant turn produces two assistant messages in a
    row, which several providers reject outright.
    """
    turns = data.get('history') or []
    if not isinstance(turns, list):
        raise ValidationError('history must be an array.',
                              details={'history': 'Expected an array of turns.'})

    cleaned = []
    for turn in turns[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get('role')
        content = (turn.get('content') or '')
        if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
            cleaned.append({'role': role, 'content': content.strip()[:2000]})

    while cleaned and cleaned[-1]['role'] == 'assistant' and len(cleaned) % 2 == 0:
        cleaned.pop()
    return cleaned


def _stream_answer(ai, messages, system_prompt, *, max_tokens, surface):
    """The shared SSE body for both `ask` endpoints.

    No tenant re-binding here, unlike the chat stream: these endpoints persist
    nothing, so no query runs after `teardown_request` has released the scope.
    The context was fully built in the request frame above.
    """
    def _generate():
        try:
            for chunk in ai.stream_text(messages=messages, system=system_prompt,
                                        role='ask', max_tokens=max_tokens,
                                        cache_system=True,
                                        metadata={'surface': surface}):
                yield f'data: {json.dumps({"delta": chunk})}\n\n'
            yield 'data: [DONE]\n\n'
        except AIError as exc:
            current_app.logger.warning('%s failed: %s', surface, exc)
            yield f'data: {json.dumps({"error": exc.user_message})}\n\n'
        except Exception as exc:
            current_app.logger.error('%s failed: %s', surface, exc)
            yield f'data: {json.dumps({"error": "The answer could not be completed."})}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'})
