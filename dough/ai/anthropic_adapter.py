"""The Anthropic implementation of `LLMAdapter`.

**This is the only module in the application permitted to import `anthropic`.**
`tests/test_ai_adapter.py` enforces that by AST-walking every other first-party
file. Before Phase 4 the SDK was constructed at eight separate call sites in
`app.py` and its exception classes were caught in five different `except`
ladders, which is what made the provider a dependency of the routes rather than
of one file.

## Two things the old code did that are preserved exactly

**A fresh client per request.** `anthropic.Anthropic(api_key=...)` was built
inside every call. That is wasteful -- it discards the underlying HTTP
connection pool -- but the fix is not free: the client is built from config that
a test may override per-app, so a process-wide singleton would leak one app's key
into another's. The client is cached per adapter *instance* instead, and
`AIService` holds one instance per Flask app on `app.extensions`. Same lifetime
as before from the app's point of view, one connection pool instead of eight.

**Prompt caching on the system block.** Three streaming surfaces sent
`system=[{'type': 'text', 'text': ..., 'cache_control': {'type': 'ephemeral'}}]`
because the financial snapshot is byte-identical for every turn in a session.
It is now `ChatRequest.cache_system=True`, and this adapter renders the block; a
provider without prompt caching ignores the flag.

Measured against the live database on 2026-07-26, the chat system prompt is
**24,632 tokens** (the pre-existing comment in `app.py` said 8-9k, which was out
of date). Two identical calls reported `cache_write=24632, cache_read=0` then
`cache_write=0, cache_read=24632` -- a full hit, billed at the cache rate rather
than as fresh input. That saving is the reason the flag exists and is worth
protecting in any future refactor.

Note the *latency* benefit is unproven here: in a single pair of samples the
cached call took 1077ms against 578ms uncached, which is well inside the noise of
n=1. Do not repeat the old comment's claim that it "starts streaming noticeably
sooner" without measuring it properly.

Allowed:   anthropic, dough.ai.{base,errors,types}, stdlib
Must not:  app, models, flask
"""

import time

import anthropic

from dough.ai.base import LLMAdapter
from dough.ai.errors import (AIAuthenticationError, AIConfigurationError,
                             AIRateLimited, AIResponseError, AITimeout,
                             AIUnavailable)
from dough.ai.types import ChatResponse, StreamEnd, TextDelta, Usage

#: Anthropic's stop_reason vocabulary mapped onto the neutral one.
_FINISH_REASONS = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'max_tokens': 'length',
    'tool_use': 'stop',
    'refusal': 'stop',
    'pause_turn': 'stop',
}


class AnthropicAdapter(LLMAdapter):
    """Chat completions via the Anthropic Messages API."""

    name = 'anthropic'

    def __init__(self, api_key='', *, timeout=None, client=None):
        self._api_key = (api_key or '').strip()
        self._timeout = timeout
        # Injectable for tests that want to assert on the wire payload without
        # a network. Production always passes None and gets a lazy real client.
        self._client = client

    @property
    def is_configured(self):
        return bool(self._api_key) or self._client is not None

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise AIConfigurationError(
                    'ANTHROPIC_API_KEY is not set', provider=self.name)
            kwargs = {'api_key': self._api_key}
            if self._timeout is not None:
                kwargs['timeout'] = self._timeout
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # -- request translation --------------------------------------------------

    def _payload(self, request):
        """Render a neutral `ChatRequest` into Messages API keyword arguments."""
        payload = {
            'model': request.model,
            'max_tokens': request.max_tokens,
            'messages': request.message_dicts(),
        }
        if request.system:
            if request.cache_system:
                # A list of typed blocks is the only form that accepts
                # cache_control. See the module docstring for why this matters.
                payload['system'] = [{
                    'type': 'text',
                    'text': request.system,
                    'cache_control': {'type': 'ephemeral'},
                }]
            else:
                payload['system'] = request.system
        if request.temperature is not None:
            payload['temperature'] = request.temperature
        return payload

    @staticmethod
    def _usage(raw_usage):
        if raw_usage is None:
            return Usage()
        return Usage(
            input_tokens=getattr(raw_usage, 'input_tokens', 0) or 0,
            output_tokens=getattr(raw_usage, 'output_tokens', 0) or 0,
            # Absent on older SDK versions, hence the getattr with a default
            # rather than an attribute access -- a missing cache counter must
            # not turn a working reply into an AttributeError.
            cache_read=getattr(raw_usage, 'cache_read_input_tokens', 0) or 0,
            cache_write=getattr(raw_usage, 'cache_creation_input_tokens', 0) or 0,
        )

    @staticmethod
    def _text_of(message):
        """Concatenate the text blocks of a message, ignoring any other type.

        The old code read `response.content[0].text`, which raises IndexError on
        an empty completion and silently drops anything after the first block.
        Joining every text block is both safer and correct for a reply the model
        split across blocks.
        """
        blocks = getattr(message, 'content', None) or []
        return ''.join(getattr(b, 'text', '') for b in blocks
                       if getattr(b, 'type', 'text') == 'text')

    # -- error translation ----------------------------------------------------

    def _translate(self, exc, model):
        """Map an Anthropic exception onto this application's hierarchy.

        Ordered most specific first. `APIStatusError` subclasses are checked
        before `APIError`, and the 5xx/429 status fallback catches anything the
        SDK adds later that we have not enumerated -- an unknown provider error
        must degrade to `AIUnavailable`, not escape as itself.
        """
        common = {'provider': self.name, 'model': model, 'cause': exc}

        if isinstance(exc, anthropic.AuthenticationError):
            return AIAuthenticationError(str(exc), **common)
        if isinstance(exc, anthropic.PermissionDeniedError):
            return AIAuthenticationError(
                str(exc), user_message=('My API key does not have access to '
                                        'that model. It needs checking in this '
                                        "app's settings."), **common)
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = None
            response = getattr(exc, 'response', None)
            if response is not None:
                try:
                    raw = response.headers.get('retry-after')
                    retry_after = float(raw) if raw else None
                except (AttributeError, TypeError, ValueError):
                    retry_after = None
                return AIRateLimited(str(exc), retry_after=retry_after, **common)
            return AIRateLimited(str(exc), **common)
        if isinstance(exc, anthropic.APITimeoutError):
            return AITimeout(str(exc), **common)
        if isinstance(exc, anthropic.APIConnectionError):
            return AIUnavailable(str(exc), **common)
        if isinstance(exc, anthropic.BadRequestError):
            # Our fault, not the provider's: a malformed payload, or a
            # max_tokens above the model's ceiling.
            return AIResponseError(str(exc), **common)
        if isinstance(exc, anthropic.NotFoundError):
            return AIResponseError(
                str(exc), user_message=('I was asked for a model I cannot '
                                        'find. This needs looking at in the '
                                        "app's settings."), **common)

        status = getattr(exc, 'status_code', None)
        if status == 429:
            return AIRateLimited(str(exc), **common)
        if status in (401, 403):
            return AIAuthenticationError(str(exc), **common)
        if isinstance(status, int) and status >= 500:
            return AIUnavailable(str(exc), **common)
        if isinstance(exc, anthropic.APIError):
            return AIUnavailable(str(exc), **common)
        return None  # not ours; let the caller re-raise

    def _raise_translated(self, exc, model):
        translated = self._translate(exc, model)
        if translated is not None:
            raise translated from exc
        raise exc

    # -- the interface --------------------------------------------------------

    def generate(self, request):
        if not self.is_configured:
            raise AIConfigurationError('ANTHROPIC_API_KEY is not set',
                                       provider=self.name)
        started = time.monotonic()
        try:
            message = self._get_client().messages.create(**self._payload(request))
        except AIConfigurationError:
            raise
        except Exception as exc:
            self._raise_translated(exc, request.model)

        text = self._text_of(message)
        elapsed = int((time.monotonic() - started) * 1000)
        stop = getattr(message, 'stop_reason', None)
        return ChatResponse(
            text=text,
            model=getattr(message, 'model', request.model) or '',
            provider=self.name,
            finish_reason=_FINISH_REASONS.get(stop, 'unknown' if stop is None else stop),
            usage=self._usage(getattr(message, 'usage', None)),
            latency_ms=elapsed,
            raw=message,
        )

    def stream(self, request):
        if not self.is_configured:
            raise AIConfigurationError('ANTHROPIC_API_KEY is not set',
                                       provider=self.name)
        started = time.monotonic()
        collected = ''
        try:
            with self._get_client().messages.stream(**self._payload(request)) as stream:
                for chunk in stream.text_stream:
                    collected += chunk
                    yield TextDelta(chunk)
                final = stream.get_final_message()
        except AIConfigurationError:
            raise
        except GeneratorExit:
            # The client disconnected. Not an error, and must not be translated
            # into one -- the caller's `finally` still needs to persist what
            # arrived, which is exactly what the chat stream relies on.
            raise
        except Exception as exc:
            self._raise_translated(exc, request.model)

        elapsed = int((time.monotonic() - started) * 1000)
        stop = getattr(final, 'stop_reason', None)
        yield StreamEnd(ChatResponse(
            # `collected` rather than re-reading the final message: it is what
            # the reader actually saw, and the two could differ if the SDK
            # normalised anything.
            text=collected,
            model=getattr(final, 'model', request.model) or '',
            provider=self.name,
            finish_reason=_FINISH_REASONS.get(stop, 'unknown' if stop is None else stop),
            usage=self._usage(getattr(final, 'usage', None)),
            latency_ms=elapsed,
            raw=final,
        ))


__all__ = ['AnthropicAdapter']
