"""Dough's AI layer.

    Route
      ↓
    AIService          service.py    — caching, model resolution, JSON, logging
      ↓
    LLMAdapter (ABC)   base.py       — the provider-neutral interface
      ↓
    AnthropicAdapter   anthropic_adapter.py

Supporting modules:

| Module | Holds |
|---|---|
| `types.py` | `ChatRequest`, `ChatResponse`, `Message`, `Usage`, `TextDelta`, `StreamEnd` |
| `errors.py` | `AIError` and its six subclasses — the only exceptions routes catch |
| `catalog.py` | `MODELS`, `DEFAULT_MODEL`, `ROLES`, `resolve()` |
| `persona.py` | `DOUGH_PERSONA` and every prompt string in the app |
| `cache.py` | `Cache` interface, `MemoryCache`, scoped `CacheKey` |

Two rules hold this together, both enforced by `tests/test_ai_adapter.py`:

1. **`anthropic` is imported by `anthropic_adapter.py` and nowhere else.**
2. **Routes catch `AIError`, never a provider's exception classes.**

Adding a provider means writing one adapter and choosing it in
`AIService.from_config`. No route changes.
"""

from dough.ai.base import EchoAdapter, LLMAdapter
from dough.ai.cache import GLOBAL_SCOPE, Cache, CacheKey, MemoryCache, NullCache
from dough.ai.errors import (AIAuthenticationError, AIConfigurationError,
                             AIError, AIRateLimited, AIResponseError,
                             AITimeout, AIUnavailable)
from dough.ai.service import EXTENSION_KEY, AIService, current_ai
from dough.ai.types import (ChatRequest, ChatResponse, Message, StreamEnd,
                            TextDelta, Usage)

__all__ = [
    # interface + implementations
    'LLMAdapter', 'EchoAdapter', 'AIService', 'current_ai', 'EXTENSION_KEY',
    # contract
    'ChatRequest', 'ChatResponse', 'Message', 'Usage', 'TextDelta', 'StreamEnd',
    # errors
    'AIError', 'AIConfigurationError', 'AIAuthenticationError', 'AIRateLimited',
    'AITimeout', 'AIUnavailable', 'AIResponseError',
    # cache
    'Cache', 'CacheKey', 'MemoryCache', 'NullCache', 'GLOBAL_SCOPE',
]
