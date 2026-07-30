"""`LLMAdapter` — the interface every provider implements.

    Route
      ↓
    AIService.generate() / .stream()
      ↓
    LLMAdapter            (this module)
      ↓
    AnthropicAdapter      (or OpenAI, Gemini, Azure, Ollama, ...)

An adapter's whole job is translation: `ChatRequest` in, provider SDK call,
`ChatResponse` out, and every provider exception mapped onto `AIError`. It
performs no caching, no persona injection, no model-name resolution and no
retries — those are `AIService`'s, so they cannot end up implemented three
slightly different ways across three providers.

## Why `stream()` exists alongside `generate()`

The user-facing brief asked for `generate(request) -> ChatResponse` as the
single contract, and that is what the non-streaming path is. Streaming cannot
collapse into it: three of Dough's eight surfaces are Server-Sent Events, and
the reader has to see tokens before the response object could possibly exist.

So the contract is: **one request type, one response type, two delivery
modes.** `stream()` takes the same `ChatRequest` and yields `TextDelta` objects
followed by exactly one `StreamEnd` carrying a fully-populated `ChatResponse`.
The two paths therefore end in the same place, which they did not before —
`stream.text_stream` was read directly by the routes and the usage numbers were
simply thrown away.

## What an implementer must guarantee

1. **Never raise a provider exception.** Everything becomes an `AIError`
   subclass. This is enforced for the Anthropic adapter by a test that walks
   `app.py`'s AST for `except anthropic.*`.
2. **`is_configured` must not perform I/O.** It is called on the request path
   to decide whether to render a card at all, and a network probe there would
   put a provider round-trip in front of a page load.
3. **A stream that fails after the first delta still raises.** Callers append
   to a buffer as deltas arrive and persist whatever accumulated, so a
   half-answer must be distinguishable from a complete one.
4. **`name` is stable.** It appears in `ChatResponse.provider`, in log lines and
   in cache keys.

Allowed:   dough.ai.types, dough.ai.errors, stdlib
Must not:  app, models, flask, any provider SDK (that is the subclass's job)
"""

from abc import ABC, abstractmethod
from typing import Iterator, Union

from dough.ai.errors import AIConfigurationError
from dough.ai.types import (ChatRequest, ChatResponse, StreamEnd, TextDelta,
                            Usage)


class LLMAdapter(ABC):
    """A provider of chat completions."""

    #: Stable identifier, e.g. 'anthropic'. Appears in responses and logs.
    name = 'unknown'

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this adapter could serve a request right now.

        Must be cheap and must not touch the network -- see guarantee 2 above.
        For most providers this is "an API key is present and non-empty".
        """

    @abstractmethod
    def generate(self, request: ChatRequest) -> ChatResponse:
        """One completion, start to finish.

        Raises `AIConfigurationError` when not configured, and the appropriate
        `AIError` subclass for any provider failure.
        """

    @abstractmethod
    def stream(self, request: ChatRequest) -> Iterator[Union[TextDelta, StreamEnd]]:
        """The same completion, delivered incrementally.

        Yields zero or more `TextDelta`, then exactly one `StreamEnd`. Raises
        the same `AIError` subclasses as `generate`, including part-way through.
        """

    def __repr__(self):
        return f'<{self.__class__.__name__} name={self.name!r} configured={self.is_configured}>'


class EchoAdapter(LLMAdapter):
    """A deterministic adapter that never leaves the process.

    Ships in the package rather than in the test suite because it is the
    reference implementation of the contract: it is what a new provider adapter
    should be read against, and it is what lets `AIService` and the eight routes
    be tested end to end with no API key and no network. `tests/conftest.py`
    installs it, which is why the suite exercises the real route bodies rather
    than skipping them.

    The reply is the last user message, prefixed. `scripted` overrides that with
    a queue of canned replies, which is how the JSON-shaped surfaces get tested.
    """

    name = 'echo'

    def __init__(self, scripted=None, *, configured=True, fail_with=None,
                 fail_after=None):
        #: Replies to hand out in order; the echo behaviour resumes when empty.
        self.scripted = list(scripted or [])
        self._configured = configured
        #: An AIError instance to raise instead of replying.
        self.fail_with = fail_with
        #: Raise `fail_with` mid-stream, after this many deltas.
        self.fail_after = fail_after
        #: Every request received, for assertions.
        self.requests = []

    @property
    def is_configured(self):
        return self._configured

    def _text_for(self, request):
        self.requests.append(request)
        if self.fail_with is not None and self.fail_after is None:
            raise self.fail_with
        if self.scripted:
            return self.scripted.pop(0)
        last_user = next((m.content for m in reversed(request.messages)
                          if m.role == 'user'), '')
        return f'echo: {last_user}'

    def _response(self, request, text, latency_ms=0):
        return ChatResponse(
            text=text,
            model=request.model or '',
            provider=self.name,
            finish_reason='stop',
            # Rough but non-zero, so a caller asserting on usage sees a real
            # shape rather than all zeroes.
            usage=Usage(input_tokens=sum(len(m.content) for m in request.messages) // 4,
                        output_tokens=len(text) // 4),
            latency_ms=latency_ms,
            raw=None,
        )

    def generate(self, request):
        if not self._configured:
            raise AIConfigurationError('EchoAdapter deliberately unconfigured',
                                       provider=self.name)
        return self._response(request, self._text_for(request))

    def stream(self, request):
        if not self._configured:
            raise AIConfigurationError('EchoAdapter deliberately unconfigured',
                                       provider=self.name)
        text = self._text_for(request)
        # Word-at-a-time, so a test can observe more than one delta.
        chunks = [w + ' ' for w in text.split(' ')]
        if chunks:
            chunks[-1] = chunks[-1].rstrip()
        sent = ''
        for index, chunk in enumerate(chunks):
            if self.fail_after is not None and index >= self.fail_after:
                raise self.fail_with or RuntimeError('EchoAdapter mid-stream failure')
            sent += chunk
            yield TextDelta(chunk)
        yield StreamEnd(self._response(request, sent))


__all__ = ['LLMAdapter', 'EchoAdapter']
