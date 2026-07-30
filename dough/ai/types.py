"""The provider-neutral contract: what goes into a model and what comes back.

Nothing in this module knows that Anthropic exists. That is the whole point —
`ChatRequest` is built by a route and consumed by whichever adapter is
installed, so adding a provider means writing a translator into its SDK, not
touching a route.

Two shapes deliberately kept out of these objects:

- **Provider-specific block structures.** Anthropic's `system` can be a string
  or a list of typed blocks with `cache_control`; OpenAI has no equivalent and
  puts the system prompt in the message list. So `ChatRequest` carries a plain
  `system` string plus a boolean `cache_system`, and each adapter renders that
  into its own wire format. A route never writes `{'type': 'text', ...}` again.
- **Tools, function calling, and structured-output schemas.** Those are real
  and coming, but they are a separate project (see Phase 4's scope note), and a
  field designed before the first use case is a field designed wrong.
  `ChatRequest.metadata` exists so an experiment can carry something extra
  without a signature change in the meantime.

`response_format` is the one forward-looking field included, because six of the
eight existing call sites already demand JSON in prose and then hand-strip
markdown fences off the reply. Naming that intent lets `AIService` do the
stripping in one place instead of six.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

# Roles the contract accepts. Deliberately not an enum: these cross a JSON
# boundary in both directions (the chat history table stores them as strings,
# and the client posts them back), so a plain string keeps the edges simple.
ROLES = ('user', 'assistant')


@dataclass(frozen=True)
class Message:
    """One conversational turn.

    Frozen because a request that has been handed to an adapter must not change
    underneath it -- the streaming path holds the message list across the whole
    response, and a mutation mid-stream would be invisible and unreproducible.
    """

    role: str
    content: str

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f'role must be one of {ROLES}, got {self.role!r}')

    def as_dict(self):
        return {'role': self.role, 'content': self.content}


@dataclass(frozen=True)
class Usage:
    """Token accounting, normalised across providers.

    `cache_read` and `cache_write` are zero for a provider without prompt
    caching rather than None, so a caller can always add them up.

    `cache_read` being large is how you confirm prompt caching is actually
    working -- before these fields existed there was no way to see it from the
    app at all, because the routes read `stream.text_stream` and discarded the
    usage object. Measured 2026-07-26: the chat system prompt is ~24.6k tokens
    and a repeat call reads all of it from cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self):
        return self.input_tokens + self.output_tokens

    def as_dict(self):
        return {'input_tokens': self.input_tokens,
                'output_tokens': self.output_tokens,
                'cache_read': self.cache_read,
                'cache_write': self.cache_write,
                'total': self.total}


@dataclass
class ChatRequest:
    """Everything needed to ask a model for one completion.

    Built by a route or a service, never by an adapter. `model` is a catalog
    key or a provider model id -- `AIService` resolves it through
    `dough.ai.catalog` before the adapter ever sees it, so an unknown id becomes
    the default rather than a provider error.
    """

    messages: List[Message]
    system: Optional[str] = None
    model: Optional[str] = None
    max_tokens: int = 1024
    temperature: Optional[float] = None
    #: Ask the provider to cache the system prompt. Ignored by providers that
    #: cannot; worth ~10x on the repeated snapshot for those that can.
    cache_system: bool = False
    #: 'text' or 'json'. 'json' does not enable any provider's native JSON mode
    #: -- the prompts already ask for JSON in prose. It tells AIService to strip
    #: markdown fences and parse, which six call sites were each doing by hand.
    response_format: str = 'text'
    #: Free-form, for tracing and future provider options. Never sent verbatim.
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.messages:
            raise ValueError('ChatRequest needs at least one message')
        # Accept plain dicts so a route can pass the shape it already has (the
        # chat history rows, the client-supplied turn list) without mapping.
        self.messages = [m if isinstance(m, Message) else Message(**m)
                         for m in self.messages]
        if self.max_tokens <= 0:
            raise ValueError(f'max_tokens must be positive, got {self.max_tokens}')
        if self.response_format not in ('text', 'json'):
            raise ValueError(
                f"response_format must be 'text' or 'json', got {self.response_format!r}")

    def message_dicts(self):
        return [m.as_dict() for m in self.messages]

    def replace(self, **changes):
        """A shallow copy with fields overridden.

        `AIService` uses this to apply the resolved model and the persona
        without mutating the caller's request -- a caller that builds one
        request and sends it twice must get the same thing both times.
        """
        base = {
            'messages': list(self.messages),
            'system': self.system,
            'model': self.model,
            'max_tokens': self.max_tokens,
            'temperature': self.temperature,
            'cache_system': self.cache_system,
            'response_format': self.response_format,
            'metadata': dict(self.metadata),
        }
        base.update(changes)
        return ChatRequest(**base)


@dataclass(frozen=True)
class ChatResponse:
    """One completion, plus what it cost and how it ended.

    `finish_reason` is normalised to 'stop' | 'length' | 'error' | 'unknown'.
    'length' matters more than it looks: it is the signal that `max_tokens` cut
    the model off mid-sentence, which on a JSON-shaped reply means the parse is
    guaranteed to fail. `AIService` turns that into `AIResponseError` with a
    message that says which it was, rather than a bare JSONDecodeError.
    """

    text: str
    model: str = ''
    provider: str = ''
    finish_reason: str = 'unknown'
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    #: The provider's own response object. For logging and debugging only --
    #: anything that reads this is coupling itself to a provider again.
    raw: Any = None

    @property
    def truncated(self):
        return self.finish_reason == 'length'

    def as_dict(self):
        """Serializable summary. Deliberately omits `raw`."""
        return {'text': self.text, 'model': self.model,
                'provider': self.provider, 'finish_reason': self.finish_reason,
                'usage': self.usage.as_dict(), 'latency_ms': self.latency_ms}


@dataclass(frozen=True)
class TextDelta:
    """One incremental chunk of a streamed reply."""

    text: str


@dataclass(frozen=True)
class StreamEnd:
    """The final event of a stream, carrying the assembled response.

    A stream yields `TextDelta`s and then exactly one `StreamEnd`. Making the
    terminator an object rather than a sentinel means the streaming and
    non-streaming paths converge on the same `ChatResponse` -- usage and
    finish_reason are available for a streamed reply too, which they were not
    when the routes read `stream.text_stream` directly.
    """

    response: ChatResponse


#: What an adapter's `stream()` yields.
StreamEvent = Iterator  # documentation alias; see LLMAdapter.stream


__all__ = ['Message', 'Usage', 'ChatRequest', 'ChatResponse', 'TextDelta',
           'StreamEnd', 'ROLES']
