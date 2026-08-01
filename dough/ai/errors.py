"""The only exceptions the rest of Dough may catch from the AI layer.

Before this module, eight routes caught `anthropic.RateLimitError`,
`anthropic.AuthenticationError`, `anthropic.APIConnectionError` and
`anthropic.APIError` directly. That made the provider a dependency of every
route's error handling: swapping providers would mean rewriting eight
`except` ladders, and adding a second provider would mean catching both SDKs'
exception trees in the same block.

The rule is now one line long: **routes catch `AIError` and its subclasses,
never a provider's.** `tests/test_ai_adapter.py` asserts that `app.py` contains
no `except anthropic.*` at all.

Each class carries `user_message` — the sentence Dough says when this goes
wrong. It lives here rather than at the call site because the same failure
should read the same way whether it happened in the chat stream or the
dashboard briefing, and because these strings are Dough speaking: warm, no
blame, and never leaking a provider name or a stack trace to someone looking at
their bank balance.
"""


class AIError(Exception):
    """Base for every failure originating in the AI layer.

    `retryable` tells a caller whether trying again could plausibly work. It is
    advisory -- nothing retries automatically today, because a retry inside a
    streaming response would have to replay tokens the reader already saw.
    """

    user_message = "Something went wrong on my end. Try me again in a moment."
    retryable = False

    def __init__(self, message='', *, user_message=None, provider=None,
                 model=None, cause=None):
        super().__init__(message or self.__class__.__name__)
        #: technical detail, for the log
        self.message = message
        #: what Dough says, for the reader
        self.user_message = user_message or self.__class__.user_message
        self.provider = provider
        self.model = model
        #: the provider exception this was mapped from, kept for logging only
        self.cause = cause

    def __str__(self):
        parts = [self.message or self.__class__.__name__]
        if self.provider:
            parts.append(f'provider={self.provider}')
        if self.model:
            parts.append(f'model={self.model}')
        return ' '.join(parts)


class AIConfigurationError(AIError):
    """No provider is usable — typically a missing API key.

    Distinct from `AIAuthenticationError`: this is "never set up", which is a
    normal state for a fresh self-hosted install and must not be logged as an
    error. Routes answer it with 503 and, where the feature is optional, by
    simply not rendering the card.
    """

    user_message = ("I'm not set up to answer yet — this app needs an "
                    "Anthropic API key configured.")
    retryable = False


class AIAuthenticationError(AIError):
    """A key was present and the provider rejected it."""

    user_message = ("My API key was rejected — it needs checking in this "
                    "app's settings.")
    retryable = False


class AIRateLimited(AIError):
    """The provider is throttling us.

    `retry_after` is seconds, when the provider says so. Nothing consumes it
    yet; it is captured because throwing the value away at the boundary is how
    you end up unable to add backoff later without touching the adapter again.
    """

    user_message = ("I'm getting more questions than I can keep up with right "
                    "now. Give me a moment and ask again.")
    retryable = True

    def __init__(self, *args, retry_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class AITimeout(AIError):
    """The request did not complete in time."""

    user_message = "That took longer than I could wait. Ask me again?"
    retryable = True


class AIUnavailable(AIError):
    """The provider could not be reached, or returned a server-side failure."""

    user_message = ("I can't reach my brain at the moment. Check your "
                    "connection and I'll try again.")
    retryable = True


class AIResponseError(AIError):
    """The provider replied, but not with something usable.

    Covers an empty completion, a truncated one, and output that was supposed
    to be JSON and was not. This is the one failure mode that is our fault
    rather than the provider's -- usually a `max_tokens` too small for the
    format being demanded -- so it is worth its own class instead of being
    folded into `AIUnavailable`.
    """

    user_message = "I couldn't put that answer together. Ask me again?"
    retryable = True


class AIBudgetExceeded(AIError):
    """*This household* has spent its allowance. Ours, not the provider's.

    Deliberately a separate class from `AIRateLimited`, which they superficially
    resemble — both mean "not now, try later". They are opposite in the two ways
    that matter to whoever reads the log:

    - `AIRateLimited` is the provider throttling *us*, so it affects every
      household at once and is a capacity problem. This is one household
      reaching a ceiling this application set, so it affects exactly them and is
      the control working.
    - `AIRateLimited` firing a lot means asking the provider for more headroom.
      This firing a lot means either the limit is too low or somebody is
      abusing an account, and those need telling apart from a graph.

    Folding them together would make the cost control invisible in exactly the
    logs somebody scans when the bill is wrong.

    `retryable` is True because the window does reset — but not soon. See
    `retry_after`, which is seconds and is always populated here (the limiter
    knows precisely when the window turns over, which a provider rarely says).
    """

    user_message = ("We've hit this household's limit on questions for now. "
                    "The allowance refreshes shortly — try me again then.")
    retryable = True

    def __init__(self, *args, retry_after=None, policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after
        #: which policy refused — `ai` (hourly) or `ai_daily`
        self.policy = policy


__all__ = [
    'AIError', 'AIConfigurationError', 'AIAuthenticationError',
    'AIRateLimited', 'AITimeout', 'AIUnavailable', 'AIResponseError',
    'AIBudgetExceeded',
]
