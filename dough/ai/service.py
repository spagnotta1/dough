"""`AIService` — the only thing a route talks to.

    Route  →  AIService  →  LLMAdapter  →  provider SDK

Everything that is the same for every surface lives here, so it cannot end up
implemented five slightly different ways across eight routes:

- **Model resolution.** A name from a POST body or `localStorage` goes through
  `catalog.resolve()`, so an unknown id becomes the default instead of a
  provider error.
- **Caching**, through the `Cache` interface, with scoped keys. See
  `dough/ai/cache.py` for why the scope field exists before tenancy does.
- **JSON extraction.** Six of the eight surfaces ask for JSON in prose and then
  hand-strip markdown fences. That logic was duplicated four times with three
  different implementations, one of which (`raw.split('\\n', 1)[1]`) raises
  IndexError on a single-line fenced reply. It is one function now.
- **Truncation detection.** `finish_reason == 'length'` on a JSON-shaped reply
  guarantees the parse will fail. Reporting "the answer was cut off because
  max_tokens was too small" instead of a bare `JSONDecodeError` is the
  difference between a diagnosable bug and a mystery.
- **Logging.** One place that knows a model call happened, what it cost, and
  how long it took.

## What is deliberately NOT here

No retries. A retry inside a streaming response would replay tokens the reader
already saw, and a retry on the non-streaming path would double the latency of a
card that exists to appear in about a second. `AIError.retryable` records which
failures *could* be retried so the decision can be made later, at a layer that
knows whether the user is still waiting.

No tool calling, function calling, MCP, embeddings, or a prompt templating
engine. Each is its own project; this phase replaces a provider dependency with
an adapter and nothing more.

Allowed:   dough.ai.*, dough.services.audit, flask (current_app, for the
           per-app instance), stdlib
Must not:  app, models, anthropic, render_template/url_for/jsonify

`dough.services.audit` was added to that list in Phase 8, deliberately and with
a cost. It is the one dependency here that points at the domain rather than away
from it. The alternative was an `audit.record` call in each of the eight
surfaces that ask for a completion -- the same duplication the third bullet
above exists to remove, on the one code path where forgetting a call means an
AI request that nothing recorded. `_log` is already "the one place that knows a
model call happened"; this makes that true of the audit trail too.
"""

import json
import logging
import re
import time

# `import dough.ai.catalog as catalog`, not `from dough.ai import catalog`: the
# latter names the package, whose __init__ re-exports this module, so the
# dependency graph gains a service -> package -> service cycle. It works at
# runtime (the partially-initialised package is already in sys.modules) but it
# is not what this module depends on. It depends on catalog.
import dough.ai.catalog as catalog
from dough.ai.cache import GLOBAL_SCOPE, Cache, CacheKey, MemoryCache
from dough.ai.errors import (AIBudgetExceeded, AIConfigurationError, AIError,
                             AIResponseError)
from dough.ai.types import ChatRequest, StreamEnd, TextDelta

logger = logging.getLogger(__name__)

#: Key under which the per-app service is stored in `app.extensions`.
EXTENSION_KEY = 'dough_ai'

#: Surfaces that spend against `ai_categorize` instead of `ai`/`ai_daily`.
#:
#: Work nobody clicked, whose size is set by how much data arrived rather than
#: by how much a person asked for. See the `ai_categorize` policy in
#: `dough/services/ratelimit.py` for why that is a different budget rather than
#: no budget.
#:
#: Membership is safe to key on because every `surface` in this application is
#: a literal at its call site — there is no path by which a request body names
#: its own surface, and so none by which one talks its way out of the
#: interactive ceiling.
UNATTENDED_SURFACES = frozenset({'auto_categorize'})

# A leading ```json / ``` fence and its closing partner. Anchored and applied to
# the whole string rather than per-line: the old MULTILINE variants would strip
# a fence out of the *middle* of a reply that legitimately contained one, which
# on a chat answer about code is a real reply being silently mangled.
_FENCE_OPEN = re.compile(r'^\s*```[a-zA-Z0-9_-]*\s*\n')
_FENCE_CLOSE = re.compile(r'\n\s*```\s*$')
_FENCE_INLINE = re.compile(r'^\s*```[a-zA-Z0-9_-]*\s*(.*?)\s*```\s*$', re.DOTALL)


def strip_code_fence(text):
    """Remove one wrapping markdown fence, if the whole string is fenced.

    Leaves a fence that is merely *present* alone -- only a fence that opens at
    the start and closes at the end is removed.
    """
    if not text:
        return ''
    text = text.strip()
    if not text.startswith('```'):
        return text
    inline = _FENCE_INLINE.match(text)
    if inline:
        return inline.group(1).strip()
    text = _FENCE_OPEN.sub('', text)
    text = _FENCE_CLOSE.sub('', text)
    return text.strip()


def extract_json_object(text):
    """The outermost JSON object in `text`, as a string.

    Falls back to the fence-stripped text when no braces are found, so the
    caller's parse error names the real problem rather than an empty string.
    """
    cleaned = strip_code_fence(text)
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end > start:
        return cleaned[start:end + 1]
    return cleaned


class AIService:
    """Persona, caching, model resolution and JSON handling around an adapter."""

    def __init__(self, adapter, *, cache=None, cache_ttl=3600,
                 scope_provider=None):
        self.adapter = adapter
        self.cache: Cache = cache if cache is not None else MemoryCache()
        self.cache_ttl = cache_ttl
        # Phase 5 filled this in, which is the whole of what SEC-0003 needed:
        # app.py passes `dough.services.cache.household_scope`, every CacheKey
        # is namespaced by household, and the generated paragraph about one
        # family's spending stops being reachable from another's dashboard.
        #
        # The GLOBAL_SCOPE default remains for a service constructed without an
        # app -- the adapter tests do this -- and only for that. It is not a
        # fallback anything in the running application takes.
        self._scope_provider = scope_provider or (lambda: GLOBAL_SCOPE)

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(cls, config, *, adapter=None, cache=None,
                    scope_provider=None):
        """Build a service from a Flask config mapping.

        Takes the mapping rather than the app so it stays testable without one,
        and so it is honest about depending on exactly three keys.
        """
        if adapter is None:
            from dough.ai.anthropic_adapter import AnthropicAdapter
            adapter = AnthropicAdapter(
                api_key=config.get('ANTHROPIC_API_KEY', ''),
                timeout=config.get('AI_REQUEST_TIMEOUT'),
            )
        return cls(adapter, cache=cache,
                   cache_ttl=config.get('AI_INSIGHT_CACHE_TTL', 3600),
                   scope_provider=scope_provider)

    @classmethod
    def init_app(cls, app, *, adapter=None, cache=None, scope_provider=None):
        """Attach one service to a Flask app and return it.

        On `app.extensions` rather than a module global so two apps in one
        process -- which the test suite creates constantly -- cannot share an
        API key or a cache.

        An app-attached service is always household-scoped: `create_app` passes
        `household_scope`, and there is no configuration that turns that off.
        """
        service = cls.from_config(app.config, adapter=adapter, cache=cache,
                                  scope_provider=scope_provider)
        app.extensions[EXTENSION_KEY] = service
        return service

    @property
    def scope(self):
        return self._scope_provider()

    @property
    def is_available(self):
        """Whether a request could succeed. No network, safe on the request path."""
        return bool(self.adapter) and self.adapter.is_configured

    # -- request assembly -----------------------------------------------------

    def build_request(self, messages, *, system=None, model=None, role=None,
                      max_tokens=1024, temperature=None, cache_system=False,
                      response_format='text', metadata=None):
        """A `ChatRequest` with the model resolved through the catalog."""
        resolved = catalog.resolve(model, role=role)
        return ChatRequest(
            messages=messages,
            system=system,
            model=resolved.provider_id,
            max_tokens=max_tokens,
            temperature=temperature,
            cache_system=cache_system,
            response_format=response_format,
            metadata=dict(metadata or {}),
        )

    # -- the two delivery modes -----------------------------------------------

    def generate(self, request=None, **kwargs):
        """One completion. Accepts a `ChatRequest` or `build_request` keywords."""
        request = self._coerce(request, kwargs)
        self._require_available(request)
        self._require_budget(request)
        started = time.monotonic()
        try:
            response = self.adapter.generate(request)
        except AIError:
            # Already ours; the adapter logs nothing so log it once here.
            logger.warning('AI generate failed model=%s surface=%s',
                           request.model, request.metadata.get('surface'),
                           exc_info=True)
            raise
        self._log(response, request, time.monotonic() - started)
        return response

    def stream(self, request=None, **kwargs):
        """Yields `TextDelta` then one `StreamEnd`, same contract as the adapter.

        A thin pass-through on purpose: interposing buffering here would defeat
        the point, and the routes need each delta as it arrives.
        """
        request = self._coerce(request, kwargs)
        self._require_available(request)
        self._require_budget(request)
        started = time.monotonic()
        for event in self.adapter.stream(request):
            if isinstance(event, StreamEnd):
                self._log(event.response, request, time.monotonic() - started)
            yield event

    def stream_text(self, request=None, **kwargs):
        """Only the text chunks, for a caller that does not want the end event."""
        for event in self.stream(request, **kwargs):
            if isinstance(event, TextDelta):
                yield event.text

    # -- JSON surfaces --------------------------------------------------------

    def generate_json(self, request=None, **kwargs):
        """A completion parsed as JSON.

        Raises `AIResponseError` for a truncated or unparseable reply, so a
        caller has one exception type to handle instead of `JSONDecodeError`
        plus `IndexError` plus `KeyError` -- which is what the two briefing
        routes were each catching separately.
        """
        request = self._coerce(request, kwargs)
        if request.response_format != 'json':
            request = request.replace(response_format='json')
        response = self.generate(request)

        if response.truncated:
            raise AIResponseError(
                f'reply hit max_tokens={request.max_tokens} before the JSON closed',
                user_message=("I ran out of room putting that together. "
                              "Ask me again?"),
                provider=response.provider, model=response.model)
        if not response.text.strip():
            raise AIResponseError('reply was empty', provider=response.provider,
                                  model=response.model)

        payload = extract_json_object(response.text)
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as exc:
            logger.warning('AI JSON parse failed model=%s surface=%s: %s',
                           response.model, request.metadata.get('surface'), exc)
            raise AIResponseError(
                f'reply was not valid JSON: {exc}',
                user_message=('I could not put that answer together. '
                              'Try again, or pick a different model.'),
                provider=response.provider, model=response.model,
                cause=exc) from exc
        if not isinstance(data, dict):
            raise AIResponseError(
                f'reply parsed to {type(data).__name__}, expected an object',
                provider=response.provider, model=response.model)
        return data, response

    # -- caching --------------------------------------------------------------

    def cache_key(self, surface, variant=''):
        """A key in the current scope. Phase 5 changes the scope, not the call."""
        return CacheKey(scope=self.scope, surface=surface, variant=variant)

    def cached(self, surface, producer, *, variant='', ttl=None):
        """Return a cached value for `surface`, calling `producer` on a miss.

        `producer` is a zero-argument callable so the expensive part -- building
        the financial snapshot as well as the model call -- is skipped on a hit.
        A raised `AIError` is not cached: a rate limit must not pin an empty
        card in place for an hour.
        """
        key = self.cache_key(surface, variant)
        hit = self.cache.get(key)
        if hit is not None:
            logger.debug('AI cache hit %s', key)
            return hit
        value = producer()
        if value is not None:
            self.cache.set(key, value,
                           self.cache_ttl if ttl is None else ttl)
        return value

    def invalidate(self, surface, variant=''):
        self.cache.invalidate(self.cache_key(surface, variant))

    def clear_cache(self, *, all_scopes=False):
        self.cache.clear(None if all_scopes else self.scope)

    # -- internals ------------------------------------------------------------

    def _coerce(self, request, kwargs):
        if request is not None and kwargs:
            raise TypeError('pass either a ChatRequest or keywords, not both')
        if request is not None:
            if not isinstance(request, ChatRequest):
                raise TypeError(f'expected ChatRequest, got {type(request).__name__}')
            # A request built by hand may carry a catalog key rather than a
            # provider id; resolve it so the adapter never has to.
            resolved = catalog.resolve(request.model)
            if request.model != resolved.provider_id:
                request = request.replace(model=resolved.provider_id)
            return request
        return self.build_request(**kwargs)

    def _require_available(self, request):
        if not self.is_available:
            raise AIConfigurationError(
                'no AI provider is configured',
                provider=getattr(self.adapter, 'name', 'none'),
                model=request.model)

    def _require_budget(self, request):
        """Spend one unit of this household's AI allowance, or refuse.
        [Phase 10.6 — wires SEC-0018's `ai` and `ai_daily` policies]

        ## Why here and not at the twenty-odd call sites

        `generate` and `stream` are the only two ways a completion is ever
        requested — `generate_json` and `stream_text` both funnel through them —
        so this is the whole surface, and it is two functions rather than the
        eight routes and growing that reach them. The argument is the one
        `dough/api/guard.py` makes about scope enforcement: a route added later
        inherits the limit instead of having to remember it. A cost control that
        each new AI feature must opt into is a cost control that the next AI
        feature will not have.

        It also means the budget is spent where the *provider call* happens, so
        a cache hit costs nothing. `cached()` calls its producer only on a miss,
        which is the correct accounting: the ceiling is on spending money, and a
        cached answer did not.

        ## What it keys on when there is no household

        `'unscoped'` — a shared bucket, not an exemption. Nothing in the request
        path reaches here without a household, so this covers background work
        and future callers; giving them no limit at all would leave a hole whose
        size is however many such callers get written later.

        ## What happens with no application

        Nothing. The service is constructible without Flask (the adapter tests
        do exactly that) and a limiter lives on an app, so with no app context
        there is nothing to spend against and the call proceeds. That is not a
        bypass a request can reach: every route runs in one.
        """
        from flask import has_app_context

        if not has_app_context():
            return

        from dough.services.ratelimit import current_limiter
        from dough.tenancy import current_household

        try:
            limiter = current_limiter()
        except RuntimeError:
            # No limiter installed. An application built without `Limiter.init_app`
            # is a wiring bug rather than a policy decision, but failing the AI
            # call is the wrong way to report it -- `current_limiter` already
            # raises a clear message on the surfaces that must not be silent.
            return

        identity = current_household() or 'unscoped'

        # The unattended pass is charged to its own policy. Not an exemption --
        # it is still refused when it runs away -- but not the budget that
        # exists to ration what a person clicked, because nobody clicked this
        # and its size is the bank's decision rather than theirs.
        if request.metadata.get('surface') in UNATTENDED_SURFACES:
            self._spend('ai_categorize', identity, limiter, request)
            return

        # Both names written as literals, one call each, rather than a loop over
        # a tuple. `tests/test_ratelimit.py` finds call sites by reading the AST
        # for a *constant* first argument, precisely so that "which policies does
        # this application actually enforce" is answerable by reading rather than
        # by running it -- and a loop variable makes both the test and a person
        # grepping for `'ai_daily'` miss the one place it is used.
        #
        # Hourly first: a caller who has exhausted both should be told about the
        # window that reopens sooner.
        self._spend('ai', identity, limiter, request)
        self._spend('ai_daily', identity, limiter, request)

    def _spend(self, policy_name, identity, limiter, request):
        """One policy. Raises `AIBudgetExceeded` if this call is over it."""
        decision = limiter.check(policy_name, identity)
        if decision.allowed:
            return

        logger.warning(
            'AI budget %s exhausted household=%s surface=%s retry_after=%ss',
            policy_name, identity, request.metadata.get('surface', '-'),
            decision.retry_after)
        # Function-local, as `_log` imports them: this module must not name
        # `models` at module scope (see the header).
        from dough.services import audit
        from models import EVENT_RATE_LIMITED
        audit.record(EVENT_RATE_LIMITED,
                     metadata={'policy': policy_name,
                               'retry_after': decision.retry_after,
                               'surface': request.metadata.get('surface')})
        raise AIBudgetExceeded(
            f'{policy_name} budget exhausted',
            retry_after=decision.retry_after,
            policy=policy_name,
            provider=getattr(self.adapter, 'name', 'none'),
            model=request.model)

    def _log(self, response, request, elapsed):
        usage = response.usage
        surface = request.metadata.get('surface', '-')
        logger.info(
            'AI %s surface=%s model=%s finish=%s in=%d out=%d cache_r=%d '
            'cache_w=%d %dms',
            response.provider, surface,
            response.model, response.finish_reason, usage.input_tokens,
            usage.output_tokens, usage.cache_read, usage.cache_write,
            response.latency_ms or int(elapsed * 1000))

        # Which surface, which model, what it cost. Never the prompt and never
        # the completion: this is a table nothing deletes from, and the whole
        # point of the surfaces is that the prompt contains the household's
        # transactions. `audit.redact()` would strip them anyway -- 'prompt' and
        # 'completion' are both in its deny-list -- but not putting them in is
        # the guarantee, and the redactor is the backstop.
        from dough.services import audit
        from models import EVENT_AI_REQUESTED
        audit.record(EVENT_AI_REQUESTED, entity_type='ai_surface',
                     metadata={'surface': surface, 'provider': response.provider,
                               'model': response.model,
                               'finish_reason': response.finish_reason,
                               'input_tokens': usage.input_tokens,
                               'output_tokens': usage.output_tokens,
                               'latency_ms': (response.latency_ms
                                              or int(elapsed * 1000))})


def current_ai():
    """The service attached to the current Flask app.

    A function rather than a proxy object so the failure when it is missing is a
    clear message at the call site instead of an AttributeError somewhere deeper.
    """
    from flask import current_app

    service = current_app.extensions.get(EXTENSION_KEY)
    if service is None:
        raise RuntimeError(
            'No AIService on this app. create_app() calls '
            'AIService.init_app(app); if you built the app another way, call it.')
    return service


__all__ = ['AIService', 'current_ai', 'EXTENSION_KEY', 'strip_code_fence',
           'extract_json_object']
