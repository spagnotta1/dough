"""The AI provider abstraction: the boundary, the contract, and all 8 surfaces.

Four kinds of test here, in order:

1. **Structural** — the two rules that make the abstraction real: `anthropic` is
   imported in exactly one file, and no route catches a provider exception.
   AST-based, so a mention in a comment or docstring cannot pass or fail them.
2. **Contract** — `ChatRequest`/`ChatResponse` validation, the catalog, the
   cache, the exception hierarchy.
3. **Adapter** — `AnthropicAdapter`'s translation in both directions, driven by
   fakes rather than a network. Includes every exception mapping.
4. **Surfaces** — all eight AI endpoints exercised end to end through
   `EchoAdapter`, which before Phase 4 was impossible: the SDK was constructed
   inline in each route, so the route bodies could only be tested by having a
   real API key and making real calls.
"""

import ast
import json
import os
import time

import pytest

from dough.ai import (AIAuthenticationError, AIConfigurationError, AIError,
                      AIRateLimited, AIResponseError, AITimeout, AIUnavailable,
                      AIService, ChatRequest, ChatResponse, EchoAdapter,
                      LLMAdapter, MemoryCache, Message, NullCache, StreamEnd,
                      TextDelta, Usage)
from dough.ai import catalog, persona
from dough.ai.anthropic_adapter import AnthropicAdapter
from dough.ai.cache import GLOBAL_SCOPE, CacheKey
from dough.ai.service import extract_json_object, strip_code_fence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, 'templates')

#: The one file allowed to import the provider SDK.
ADAPTER_REL = os.path.join('dough', 'ai', 'anthropic_adapter.py')

SKIP_DIRS = {'.git', '__pycache__', 'migrations', 'backups', 'uploads',
             'static', 'templates', 'linter', 'docs', 'node_modules'}

# `tests/` is excluded from the boundary scans below, and that exclusion is the
# correct scope rather than a convenience: this very file imports `anthropic` to
# assert the exception mapping, and quotes the persona to assert where it lives.
# The rules being enforced are about *application* code -- the thing that would
# have to change to swap providers. A test that verifies the boundary is not a
# violation of it.
APP_SKIP_DIRS = SKIP_DIRS | {'tests'}


def _python_files(include_tests=False):
    skip = SKIP_DIRS if include_tests else APP_SKIP_DIRS
    for base, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in names:
            if name.endswith('.py'):
                yield os.path.join(base, name)


def _parse(path):
    with open(path, encoding='utf-8') as handle:
        return ast.parse(handle.read())


# ═══════════════════════════════════════════════════════════════════════════
# 1. Structural — the boundary exists and holds
# ═══════════════════════════════════════════════════════════════════════════

def test_anthropic_is_imported_in_exactly_one_file():
    """The provider SDK may only be imported by its adapter.

    This is the assertion the whole phase exists to make. Before Phase 4,
    `app.py` imported `anthropic` and constructed a client at eight call sites.
    """
    importers = []
    for path in _python_files():
        rel = os.path.relpath(path, REPO_ROOT)
        for node in ast.walk(_parse(path)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            if any(n.split('.')[0] == 'anthropic' for n in names):
                importers.append(rel)
    assert sorted(set(importers)) == [ADAPTER_REL], (
        f'anthropic must be imported only by {ADAPTER_REL}, found in {importers}')


def test_no_module_outside_the_adapter_catches_a_provider_exception():
    """`except anthropic.X` may appear only in the adapter.

    Catching a provider's exception type anywhere else re-couples that file to
    the provider, which is what five separate `except` ladders in `app.py` did.
    """
    offenders = []
    for path in _python_files():
        rel = os.path.relpath(path, REPO_ROOT)
        if rel == ADAPTER_REL:
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            for sub in ast.walk(node.type):
                if (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == 'anthropic'):
                    offenders.append(f'{rel}:{node.lineno}')
    assert offenders == [], f'provider exceptions caught outside the adapter: {offenders}'


def test_app_no_longer_holds_the_three_global_ai_caches():
    """_insight_cache / _brief_cache / _wealth_cache must be gone.

    Process-global and keyed only by time -- a cross-tenant leak the moment
    Phase 5 lands. Caching goes through dough/ai/cache.py, whose keys are scoped.
    """
    tree = _parse(os.path.join(REPO_ROOT, 'app.py'))
    assigned = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    leaked = assigned & {'_insight_cache', '_brief_cache', '_wealth_cache'}
    assert leaked == set(), f'still defined in app.py: {sorted(leaked)}'


def test_no_template_hardcodes_a_model_id():
    """Model ids come from the catalog via a context processor.

    chat.html and rules.html each had their own copy of the same three ids and
    disagreed about the default; rules.html had them twice.
    """
    offenders = []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith('.html'):
            continue
        with open(os.path.join(TEMPLATES, name), encoding='utf-8') as handle:
            body = handle.read()
        for model in catalog.MODELS:
            if model.provider_id in body:
                offenders.append(f'{name} hardcodes {model.provider_id}')
    assert offenders == [], offenders


def test_no_module_outside_persona_holds_the_persona_text():
    """A prompt lives in dough/ai/persona.py or nowhere."""
    marker = 'You are Dough, the financial companion'
    holders = []
    for path in _python_files():
        with open(path, encoding='utf-8') as handle:
            if marker in handle.read():
                holders.append(os.path.relpath(path, REPO_ROOT))
    assert holders == [os.path.join('dough', 'ai', 'persona.py')], holders


# ═══════════════════════════════════════════════════════════════════════════
# 2. Contract — the provider-neutral types
# ═══════════════════════════════════════════════════════════════════════════

def test_message_rejects_an_unknown_role():
    with pytest.raises(ValueError, match='role must be one of'):
        Message(role='system', content='x')


def test_chat_request_accepts_plain_dicts():
    """A route passes the shape it already has -- chat history rows, POST bodies."""
    req = ChatRequest(messages=[{'role': 'user', 'content': 'hi'}])
    assert req.messages == [Message('user', 'hi')]


@pytest.mark.parametrize('kwargs,match', [
    ({'messages': []}, 'at least one message'),
    ({'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 0}, 'must be positive'),
    ({'messages': [{'role': 'user', 'content': 'x'}], 'response_format': 'xml'},
     "must be 'text' or 'json'"),
])
def test_chat_request_validates(kwargs, match):
    with pytest.raises(ValueError, match=match):
        ChatRequest(**kwargs)


def test_replace_does_not_mutate_the_original():
    """A caller that builds one request and sends it twice gets the same thing."""
    original = ChatRequest(messages=[{'role': 'user', 'content': 'x'}],
                           model='a', metadata={'surface': 's'})
    copy = original.replace(model='b')
    copy.metadata['surface'] = 'mutated'
    assert original.model == 'a'
    assert original.metadata == {'surface': 's'}
    assert copy.model == 'b'


def test_usage_totals_and_response_truncation_flag():
    assert Usage(input_tokens=10, output_tokens=5).total == 15
    assert ChatResponse(text='x', finish_reason='length').truncated is True
    assert ChatResponse(text='x', finish_reason='stop').truncated is False


def test_response_as_dict_omits_the_raw_provider_object():
    """`raw` is for logging; serialising it would leak provider shape outward."""
    resp = ChatResponse(text='x', raw={'provider': 'internals'})
    assert 'raw' not in resp.as_dict()


# ── the exception hierarchy ────────────────────────────────────────────────

@pytest.mark.parametrize('cls', [AIConfigurationError, AIAuthenticationError,
                                 AIRateLimited, AITimeout, AIUnavailable,
                                 AIResponseError])
def test_every_error_is_an_aierror_with_a_user_message(cls):
    """One `except AIError` in a route must catch all of them.

    `user_message` is what Dough says; the technical detail stays in `message`
    for the log. The two must never be the same string -- that is the mistake
    the old code made, returning `str(anthropic_exception)` straight to the
    browser.

    Note this does NOT assert the provider is unnamed: AIConfigurationError
    deliberately says "Anthropic API key", because a reader who has not set one
    needs to know which key to set. The rule is that raw exception text and
    stack traces never reach the reader, not that the provider is a secret.
    """
    err = cls('technical detail from the SDK')
    assert isinstance(err, AIError)
    assert err.user_message
    assert err.user_message != err.message
    assert 'technical detail' not in err.user_message
    for leak in ('traceback', 'exception', 'errno', 'status_code', '  File "'):
        assert leak not in err.user_message.lower()
    # A sentence, not a code. These are read by someone looking at their money.
    assert err.user_message[0].isupper()
    assert err.user_message.rstrip()[-1] in '.?!'


def test_retryable_is_set_deliberately():
    """A missing key is not retryable; a rate limit is."""
    assert AIConfigurationError().retryable is False
    assert AIAuthenticationError().retryable is False
    assert AIRateLimited().retryable is True
    assert AIUnavailable().retryable is True


def test_error_str_includes_provider_and_model_for_the_log():
    err = AIUnavailable('boom', provider='anthropic', model='m-1')
    assert 'boom' in str(err) and 'anthropic' in str(err) and 'm-1' in str(err)


# ── the catalog ────────────────────────────────────────────────────────────

def test_default_model_is_in_the_catalog():
    assert catalog.get(catalog.DEFAULT_MODEL) is not None


def test_every_role_maps_to_a_real_model():
    for role, key in catalog.ROLES.items():
        assert catalog.get(key) is not None, f'role {role} -> unknown model {key}'


def test_resolve_never_raises_and_never_returns_none():
    """The name comes from localStorage or a POST body, so it is untrusted.

    A typo must fall back, not 500 a page someone is reading a balance on.
    """
    for name in (None, '', '   ', 'not-a-model', 'claude-9', 123, object()):
        assert catalog.resolve(name).provider_id


def test_resolve_accepts_both_a_key_and_a_provider_id():
    assert catalog.resolve('deep').provider_id == 'claude-opus-4-8'
    assert catalog.resolve('claude-opus-4-8').key == 'deep'


def test_resolve_precedence_is_name_then_role_then_default():
    assert catalog.resolve('quick', role='ask').key == 'quick'      # name wins
    assert catalog.resolve(None, role='insight').key == 'quick'     # then role
    assert catalog.resolve(None).key == catalog.DEFAULT_MODEL       # then default


def test_catalog_keys_and_ids_are_unique():
    keys = [m.key for m in catalog.MODELS]
    ids = [m.provider_id for m in catalog.MODELS]
    assert len(set(keys)) == len(keys)
    assert len(set(ids)) == len(ids)


def test_all_models_is_json_serialisable():
    """It is rendered into two templates with |tojson."""
    json.dumps(catalog.all_models())


# ── the cache ──────────────────────────────────────────────────────────────

def test_cache_key_requires_a_scope():
    """Scope is the tenancy boundary, so it cannot be forgotten."""
    with pytest.raises(ValueError, match='scope may not be empty'):
        CacheKey(scope='', surface='brief')
    with pytest.raises(ValueError, match='surface may not be empty'):
        CacheKey(scope='s', surface='')


def test_two_scopes_cannot_read_each_others_entries():
    """The Phase 5 property, asserted now so the wiring is proven before tenancy.

    Today both scopes are GLOBAL_SCOPE in production. This test is what
    guarantees that when Phase 5 starts passing a household id, entries separate
    rather than collide.
    """
    cache = MemoryCache()
    cache.set(CacheKey('household-1', 'brief'), {'narrative': 'A'}, ttl=60)
    cache.set(CacheKey('household-2', 'brief'), {'narrative': 'B'}, ttl=60)
    assert cache.get(CacheKey('household-1', 'brief')) == {'narrative': 'A'}
    assert cache.get(CacheKey('household-2', 'brief')) == {'narrative': 'B'}
    assert cache.get(CacheKey('household-3', 'brief')) is None


def test_cache_clear_can_target_one_scope():
    cache = MemoryCache()
    cache.set(CacheKey('a', 'x'), 1, ttl=60)
    cache.set(CacheKey('b', 'x'), 2, ttl=60)
    cache.clear(scope='a')
    assert cache.get(CacheKey('a', 'x')) is None
    assert cache.get(CacheKey('b', 'x')) == 2


def test_cache_expires_entries():
    clock = {'t': 1000.0}
    cache = MemoryCache(time_source=lambda: clock['t'])
    key = CacheKey('s', 'x')
    cache.set(key, 'v', ttl=10)
    clock['t'] = 1009.9
    assert cache.get(key) == 'v'
    clock['t'] = 1010.0
    assert cache.get(key) is None
    assert len(cache) == 0, 'expired entry should be dropped on read'


def test_cache_rejects_a_non_positive_ttl():
    cache = MemoryCache()
    cache.set(CacheKey('s', 'x'), 'v', ttl=0)
    assert cache.get(CacheKey('s', 'x')) is None


def test_null_cache_never_stores():
    cache = NullCache()
    cache.set(CacheKey('s', 'x'), 'v', ttl=999)
    assert cache.get(CacheKey('s', 'x')) is None


# ── JSON extraction ───────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('{"a":1}', '{"a":1}'),
    ('```json\n{"a":1}\n```', '{"a":1}'),
    ('```\n{"a":1}\n```', '{"a":1}'),
    ('```json {"a":1} ```', '{"a":1}'),          # single line, the old code's IndexError
    ('Here you go:\n{"a":1}\nhope that helps', '{"a":1}'),
    ('{"a": "text with } brace"}', '{"a": "text with } brace"}'),
])
def test_extract_json_object_handles_what_models_actually_emit(raw, expected):
    assert extract_json_object(raw) == expected


def test_strip_code_fence_leaves_an_interior_fence_alone():
    """The old per-line regex mangled a legitimate reply containing a fence."""
    text = 'Try this:\n```python\nx = 1\n```\nthat should work'
    assert strip_code_fence(text) == text


# ═══════════════════════════════════════════════════════════════════════════
# 3. Adapter — translation in both directions, no network
# ═══════════════════════════════════════════════════════════════════════════

class _FakeMessages:
    """Stands in for `client.messages`, capturing the payload it was sent."""

    def __init__(self, reply=None, raise_with=None, stream_chunks=None):
        self.reply = reply
        self.raise_with = raise_with
        self.stream_chunks = stream_chunks or []
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)
        if self.raise_with:
            raise self.raise_with
        return self.reply

    def stream(self, **payload):
        self.calls.append(payload)
        if self.raise_with:
            raise self.raise_with
        return _FakeStream(self.stream_chunks, self.reply)


class _FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


class _Block:
    def __init__(self, text, type='text'):
        self.text = text
        self.type = type


class _FakeUsage:
    def __init__(self, i=0, o=0, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


class _FakeMessage:
    def __init__(self, blocks, stop_reason='end_turn', model='m', usage=None):
        self.content = blocks
        self.stop_reason = stop_reason
        self.model = model
        self.usage = usage or _FakeUsage(10, 4)


def _adapter(messages):
    return AnthropicAdapter(api_key='test-key', client=_FakeClient(messages))


def test_adapter_is_an_llmadapter():
    assert issubclass(AnthropicAdapter, LLMAdapter)


def test_is_configured_does_no_io():
    """Called on the request path, so it must never touch the network."""
    assert AnthropicAdapter(api_key='').is_configured is False
    assert AnthropicAdapter(api_key='  ').is_configured is False
    assert AnthropicAdapter(api_key='k').is_configured is True


def test_generate_translates_the_request_and_the_response():
    fake = _FakeMessages(reply=_FakeMessage([_Block('the answer')],
                                            usage=_FakeUsage(100, 20, 900, 5)))
    resp = _adapter(fake).generate(ChatRequest(
        messages=[{'role': 'user', 'content': 'q'}],
        system='be brief', model='claude-x', max_tokens=64, temperature=0.3))

    sent = fake.calls[0]
    assert sent['model'] == 'claude-x'
    assert sent['max_tokens'] == 64
    assert sent['temperature'] == 0.3
    assert sent['system'] == 'be brief'      # a plain string when not cached
    assert sent['messages'] == [{'role': 'user', 'content': 'q'}]

    assert resp.text == 'the answer'
    assert resp.provider == 'anthropic'
    assert resp.finish_reason == 'stop'
    assert resp.usage.input_tokens == 100
    assert resp.usage.cache_read == 900
    assert resp.usage.cache_write == 5


def test_cache_system_renders_the_provider_cache_control_block():
    """The 8-9k-token snapshot is byte-identical per turn; caching it is ~10x."""
    fake = _FakeMessages(reply=_FakeMessage([_Block('x')]))
    _adapter(fake).generate(ChatRequest(
        messages=[{'role': 'user', 'content': 'q'}],
        system='big snapshot', model='m', cache_system=True))
    assert fake.calls[0]['system'] == [{
        'type': 'text', 'text': 'big snapshot',
        'cache_control': {'type': 'ephemeral'},
    }]


def test_no_system_key_is_sent_when_there_is_no_system_prompt():
    fake = _FakeMessages(reply=_FakeMessage([_Block('x')]))
    _adapter(fake).generate(ChatRequest(messages=[{'role': 'user', 'content': 'q'}],
                                        model='m'))
    assert 'system' not in fake.calls[0]


def test_max_tokens_stop_reason_becomes_length():
    fake = _FakeMessages(reply=_FakeMessage([_Block('cut off')],
                                            stop_reason='max_tokens'))
    resp = _adapter(fake).generate(
        ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))
    assert resp.finish_reason == 'length'
    assert resp.truncated is True


def test_multiple_text_blocks_are_joined_and_empty_content_does_not_crash():
    """The old `response.content[0].text` raised IndexError on an empty reply."""
    joined = _adapter(_FakeMessages(reply=_FakeMessage(
        [_Block('one '), _Block('two')]))).generate(
        ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))
    assert joined.text == 'one two'

    empty = _adapter(_FakeMessages(reply=_FakeMessage([]))).generate(
        ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))
    assert empty.text == ''


def test_non_text_blocks_are_ignored():
    resp = _adapter(_FakeMessages(reply=_FakeMessage(
        [_Block('keep'), _Block('drop', type='thinking')]))).generate(
        ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))
    assert resp.text == 'keep'


def test_stream_yields_deltas_then_one_streamend():
    fake = _FakeMessages(stream_chunks=['a', 'b', 'c'],
                         reply=_FakeMessage([_Block('abc')], usage=_FakeUsage(5, 3)))
    events = list(_adapter(fake).stream(
        ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m')))

    assert [e.text for e in events[:-1]] == ['a', 'b', 'c']
    assert isinstance(events[-1], StreamEnd)
    # The streamed path ends in the same ChatResponse the non-streamed one does,
    # which is what makes usage available for a stream at all.
    assert events[-1].response.text == 'abc'
    assert events[-1].response.usage.output_tokens == 3
    assert sum(isinstance(e, StreamEnd) for e in events) == 1


def test_unconfigured_adapter_raises_configuration_error():
    adapter = AnthropicAdapter(api_key='')
    with pytest.raises(AIConfigurationError):
        adapter.generate(ChatRequest(messages=[{'role': 'user', 'content': 'q'}]))
    with pytest.raises(AIConfigurationError):
        list(adapter.stream(ChatRequest(messages=[{'role': 'user', 'content': 'q'}])))


# ── exception mapping, the reason routes can catch one type ────────────────

def _provider_error(cls, status=None):
    """Build a provider exception without invoking its real __init__.

    The SDK's error classes require a request/response object; constructing one
    faithfully would be testing httpx, not the mapping.
    """
    import anthropic
    exc = cls.__new__(cls)
    Exception.__init__(exc, f'simulated {cls.__name__}')
    if status is not None:
        exc.status_code = status
    return exc


@pytest.mark.parametrize('provider_cls_name,expected', [
    ('AuthenticationError', AIAuthenticationError),
    ('PermissionDeniedError', AIAuthenticationError),
    ('RateLimitError', AIRateLimited),
    ('APITimeoutError', AITimeout),
    ('APIConnectionError', AIUnavailable),
    ('BadRequestError', AIResponseError),
    ('NotFoundError', AIResponseError),
    ('APIError', AIUnavailable),
])
def test_every_provider_exception_maps_to_ours(provider_cls_name, expected):
    import anthropic
    provider_cls = getattr(anthropic, provider_cls_name)
    fake = _FakeMessages(raise_with=_provider_error(provider_cls))
    with pytest.raises(expected) as caught:
        _adapter(fake).generate(
            ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m-1'))
    assert caught.value.provider == 'anthropic'
    assert caught.value.model == 'm-1'
    assert caught.value.cause is not None


def test_timeout_is_checked_before_connection_error():
    """APITimeoutError subclasses APIConnectionError in the SDK.

    Order matters: checking the base class first would report every timeout as a
    generic connectivity failure and lose the distinction the user sees.
    """
    import anthropic
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)
    fake = _FakeMessages(raise_with=_provider_error(anthropic.APITimeoutError))
    with pytest.raises(AITimeout):
        _adapter(fake).generate(
            ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))


@pytest.mark.parametrize('status,expected', [
    (429, AIRateLimited), (401, AIAuthenticationError),
    (403, AIAuthenticationError), (500, AIUnavailable), (503, AIUnavailable),
])
def test_unenumerated_status_codes_still_map(status, expected):
    """An SDK error class we have not enumerated must still degrade to ours."""
    import anthropic
    fake = _FakeMessages(raise_with=_provider_error(anthropic.APIStatusError, status))
    with pytest.raises(expected):
        _adapter(fake).generate(
            ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))


def test_a_stream_error_propagates_as_an_aierror():
    import anthropic
    fake = _FakeMessages(raise_with=_provider_error(anthropic.RateLimitError))
    with pytest.raises(AIRateLimited):
        list(_adapter(fake).stream(
            ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m')))


def test_a_non_provider_exception_is_not_swallowed():
    """A bug in our own code must not be reported as a provider outage."""
    fake = _FakeMessages(raise_with=KeyError('our bug'))
    with pytest.raises(KeyError):
        _adapter(fake).generate(
            ChatRequest(messages=[{'role': 'user', 'content': 'q'}], model='m'))


# ═══════════════════════════════════════════════════════════════════════════
# 4. AIService
# ═══════════════════════════════════════════════════════════════════════════

def _service(**kwargs):
    adapter = kwargs.pop('adapter', None) or EchoAdapter()
    kwargs.setdefault('cache', MemoryCache())
    return AIService(adapter, **kwargs)


def test_service_resolves_an_unknown_model_to_the_default():
    svc = _service()
    svc.generate(messages=[{'role': 'user', 'content': 'q'}], model='bogus')
    assert svc.adapter.requests[0].model == catalog.resolve().provider_id


def test_service_resolves_a_role():
    svc = _service()
    svc.generate(messages=[{'role': 'user', 'content': 'q'}], role='insight')
    assert svc.adapter.requests[0].model == catalog.provider_id(role='insight')


def test_service_refuses_both_a_request_and_keywords():
    svc = _service()
    req = ChatRequest(messages=[{'role': 'user', 'content': 'q'}])
    with pytest.raises(TypeError, match='not both'):
        svc.generate(req, max_tokens=5)


def test_service_raises_configuration_error_when_unavailable():
    svc = _service(adapter=EchoAdapter(configured=False))
    assert svc.is_available is False
    with pytest.raises(AIConfigurationError):
        svc.generate(messages=[{'role': 'user', 'content': 'q'}])


def test_generate_json_parses_a_fenced_reply():
    svc = _service(adapter=EchoAdapter(scripted=['```json\n{"narrative":"hi"}\n```']))
    data, resp = svc.generate_json(messages=[{'role': 'user', 'content': 'q'}])
    assert data == {'narrative': 'hi'}
    assert resp.provider == 'echo'


def test_generate_json_reports_truncation_rather_than_a_parse_error():
    """The actionable message: max_tokens was too small, not 'invalid JSON'."""
    class Truncating(EchoAdapter):
        def generate(self, request):
            resp = super().generate(request)
            return ChatResponse(text='{"narrative": "half a sen',
                                model=resp.model, provider=resp.provider,
                                finish_reason='length')

    svc = _service(adapter=Truncating())
    with pytest.raises(AIResponseError, match='max_tokens'):
        svc.generate_json(messages=[{'role': 'user', 'content': 'q'}], max_tokens=10)


def test_generate_json_rejects_a_non_object():
    svc = _service(adapter=EchoAdapter(scripted=['[1, 2, 3]']))
    with pytest.raises(AIResponseError, match='expected an object'):
        svc.generate_json(messages=[{'role': 'user', 'content': 'q'}])


def test_generate_json_rejects_unparseable_output():
    svc = _service(adapter=EchoAdapter(scripted=['I would rather write prose.']))
    with pytest.raises(AIResponseError, match='not valid JSON'):
        svc.generate_json(messages=[{'role': 'user', 'content': 'q'}])


def test_generate_json_rejects_an_empty_reply():
    svc = _service(adapter=EchoAdapter(scripted=['   ']))
    with pytest.raises(AIResponseError, match='empty'):
        svc.generate_json(messages=[{'role': 'user', 'content': 'q'}])


def test_cached_skips_the_producer_entirely_on_a_hit():
    """The producer builds the financial snapshot, not just the model call."""
    calls = []

    def produce():
        calls.append(1)
        return 'value'

    svc = _service()
    assert svc.cached('brief', produce) == 'value'
    assert svc.cached('brief', produce) == 'value'
    assert len(calls) == 1, 'producer must not run on a cache hit'


def test_cached_does_not_store_a_failure():
    """A rate limit must not pin an empty card in place for an hour."""
    svc = _service()

    def failing():
        raise AIRateLimited('slow down')

    with pytest.raises(AIRateLimited):
        svc.cached('brief', failing)
    assert svc.cached('brief', lambda: 'recovered') == 'recovered'


def test_cached_variants_do_not_collide():
    """A March-June briefing must not be served for a July dashboard."""
    svc = _service()
    svc.cached('brief', lambda: 'march', variant='2026-03|2026-06')
    assert svc.cached('brief', lambda: 'july', variant='default') == 'july'
    assert svc.cached('brief', lambda: 'x', variant='2026-03|2026-06') == 'march'


def test_service_scope_is_the_seam_phase_5_replaces():
    """AIService takes a scope_provider; today it returns GLOBAL_SCOPE."""
    assert _service().scope == GLOBAL_SCOPE
    scoped = AIService(EchoAdapter(), cache=MemoryCache(),
                       scope_provider=lambda: 'household-7')
    assert scoped.cache_key('brief').scope == 'household-7'


def test_two_services_with_different_scopes_do_not_share_entries():
    """The same shared cache, two tenants -- the Phase 5 isolation property."""
    shared = MemoryCache()
    a = AIService(EchoAdapter(), cache=shared, scope_provider=lambda: 'h1')
    b = AIService(EchoAdapter(), cache=shared, scope_provider=lambda: 'h2')
    a.cached('brief', lambda: 'A-data')
    assert b.cached('brief', lambda: 'B-data') == 'B-data'
    assert a.cached('brief', lambda: 'ignored') == 'A-data'


def test_stream_text_yields_only_text():
    svc = _service()
    chunks = list(svc.stream_text(messages=[{'role': 'user', 'content': 'a b c'}]))
    assert ''.join(chunks) == 'echo: a b c'
    assert all(isinstance(c, str) for c in chunks)


def test_service_from_config_builds_an_anthropic_adapter():
    svc = AIService.from_config({'ANTHROPIC_API_KEY': 'k',
                                 'AI_INSIGHT_CACHE_TTL': 60})
    assert isinstance(svc.adapter, AnthropicAdapter)
    assert svc.adapter.is_configured is True
    assert svc.cache_ttl == 60


def test_service_from_config_without_a_key_is_unavailable():
    svc = AIService.from_config({'ANTHROPIC_API_KEY': ''})
    assert svc.is_available is False


# ═══════════════════════════════════════════════════════════════════════════
# 5. The eight surfaces, end to end
#
# Before Phase 4 none of these route bodies could be tested: each constructed
# `anthropic.Anthropic(...)` inline, so reaching the code after that line
# required a real key and a real network call.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def ai_app(tmp_path):
    """An app whose AI layer answers deterministically, with no network."""
    import finance_sync.scheduler as scheduler_module
    from app import create_app
    from dough.tenancy import tenant_scope
    from models import db

    scheduler_module._scheduler = None
    adapter = EchoAdapter()
    application = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'ai.db'}",
        'SYNC_AUTO_ENABLED': False,
        'AI_ADAPTER': adapter,
    })
    application.echo = adapter
    # The ambient default household, matching conftest's `app` fixture: these
    # tests build conversations and transactions directly through the session
    # before exercising a route.
    with application.app_context():
        with tenant_scope(application.config['DEFAULT_HOUSEHOLD_ID']):
            yield application
        db.session.remove()
    scheduler_module._scheduler = None


@pytest.fixture()
def ai_client(ai_app):
    return ai_app.test_client()


def _script(app, *replies):
    app.echo.scripted = list(replies)


def _sse_payloads(response):
    """The JSON objects out of a text/event-stream body."""
    out = []
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith('data: ') and line != 'data: [DONE]':
            out.append(json.loads(line[6:]))
    return out


def test_dashboard_insight_returns_and_then_caches(ai_app, ai_client):
    _script(ai_app, 'You spent $412 on Dining.')
    first = ai_client.get('/api/dashboard-insight').get_json()
    assert first == {'insight': 'You spent $412 on Dining.'}
    # Second call must not reach the adapter at all.
    before = len(ai_app.echo.requests)
    assert ai_client.get('/api/dashboard-insight').get_json() == first
    assert len(ai_app.echo.requests) == before


def test_dashboard_insight_is_silent_when_the_provider_fails(ai_app, ai_client):
    """An optional card renders nothing rather than an error."""
    ai_app.echo.fail_with = AIRateLimited('slow down')
    assert ai_client.get('/api/dashboard-insight').get_json() == {'insight': ''}


def test_copilot_brief_returns_json_and_defaults_the_lists(ai_app, ai_client):
    _script(ai_app, json.dumps({'narrative': 'Steady month.'}))
    data = ai_client.get('/api/copilot/brief').get_json()
    assert data['narrative'] == 'Steady month.'
    assert data['available'] is True
    assert data['opportunities'] == [] and data['questions'] == []


def test_copilot_brief_caches_per_window(ai_app, ai_client):
    """A briefing about one window must never be served for another."""
    _script(ai_app,
            json.dumps({'narrative': 'March through June.'}),
            json.dumps({'narrative': 'This month.'}))
    a = ai_client.get('/api/copilot/brief?start=2026-03-01&end=2026-06-30').get_json()
    b = ai_client.get('/api/copilot/brief').get_json()
    assert a['narrative'] == 'March through June.'
    assert b['narrative'] == 'This month.'
    # And each window still serves its own from cache.
    again = ai_client.get('/api/copilot/brief?start=2026-03-01&end=2026-06-30').get_json()
    assert again['narrative'] == 'March through June.'


def test_copilot_brief_unavailable_on_bad_json(ai_app, ai_client):
    _script(ai_app, 'not json at all')
    assert ai_client.get('/api/copilot/brief').get_json() == {'available': False}


def test_copilot_ask_streams_deltas_then_done(ai_app, ai_client):
    _script(ai_app, 'Dining is up a little.')
    resp = ai_client.post('/api/copilot/ask', json={'question': 'How am I doing?'})
    assert resp.status_code == 200
    assert resp.mimetype == 'text/event-stream'
    body = resp.get_data(as_text=True)
    assert ''.join(p['delta'] for p in _sse_payloads(resp)) == 'Dining is up a little.'
    assert body.rstrip().endswith('data: [DONE]')


def test_copilot_ask_sends_a_cacheable_system_prompt(ai_app, ai_client):
    """The snapshot prefix must still be marked cacheable after the refactor."""
    ai_client.post('/api/copilot/ask', json={'question': 'q'})
    assert ai_app.echo.requests[0].cache_system is True


def test_copilot_ask_reports_a_provider_failure_in_the_stream(ai_app, ai_client):
    ai_app.echo.fail_with = AIRateLimited('slow down')
    resp = ai_client.post('/api/copilot/ask', json={'question': 'q'})
    payloads = _sse_payloads(resp)
    assert payloads and 'error' in payloads[-1]
    assert payloads[-1]['error'] == AIRateLimited().user_message


def test_wealth_brief_and_ask(ai_app, ai_client):
    _script(ai_app, json.dumps({'narrative': 'Concentrated in tech.'}),
            'Consider trimming.')
    brief = ai_client.get('/api/investments/brief').get_json()
    assert brief['narrative'] == 'Concentrated in tech.' and brief['available'] is True

    resp = ai_client.post('/api/investments/ask', json={'question': 'Rebalance?'})
    assert ''.join(p['delta'] for p in _sse_payloads(resp)) == 'Consider trimming.'


def test_wealth_ask_replays_capped_client_history(ai_app, ai_client):
    """History is client-supplied, so the cap and role filter must survive."""
    history = [{'role': 'user', 'content': f'q{i}'} for i in range(10)]
    ai_client.post('/api/investments/ask',
                   json={'question': 'and now?', 'history': history})
    sent = ai_app.echo.requests[0].messages
    assert len(sent) <= 7                       # 6 prior turns + the question
    assert sent[-1].content == 'and now?'
    assert all(m.role in ('user', 'assistant') for m in sent)


def test_chat_stream_persists_the_assistant_reply(ai_app, ai_client):
    from models import ChatMessage, Conversation, db

    conv = Conversation(id='conv-1', title='New Chat')
    db.session.add(conv)
    db.session.commit()

    _script(ai_app, 'Here is what I see.')
    resp = ai_client.post('/api/chat_stream',
                          json={'conv_id': 'conv-1', 'message': 'How am I doing?'})
    assert ''.join(p['delta'] for p in _sse_payloads(resp)) == 'Here is what I see.'

    stored = ChatMessage.query.filter_by(session_id='conv-1').order_by(
        ChatMessage.id).all()
    assert [(m.role, m.content) for m in stored] == [
        ('user', 'How am I doing?'), ('assistant', 'Here is what I see.')]
    # The title is taken from the first user message.
    assert db.session.get(Conversation, 'conv-1').title == 'How am I doing?'


def test_chat_stream_caches_the_snapshot_prefix(ai_app, ai_client):
    from models import Conversation, db

    db.session.add(Conversation(id='c2', title='New Chat'))
    db.session.commit()
    ai_client.post('/api/chat_stream', json={'conv_id': 'c2', 'message': 'hi'})
    request = ai_app.echo.requests[0]
    assert request.cache_system is True
    assert request.system.startswith('You are Dough')
    # The financial snapshot rides in the system prompt, which is what makes the
    # prefix worth caching.
    assert 'transaction_coverage' in request.system


def test_chat_stream_falls_back_to_the_default_model_for_an_unknown_id(ai_app, ai_client):
    from models import Conversation, db

    db.session.add(Conversation(id='c3', title='New Chat'))
    db.session.commit()
    ai_client.post('/api/chat_stream',
                   json={'conv_id': 'c3', 'message': 'hi', 'model': 'claude-evil'})
    assert ai_app.echo.requests[0].model == catalog.provider_id(role='ask')


def test_chat_stream_persists_a_partial_reply_when_the_stream_fails(ai_app, ai_client):
    """A half-answer must be saved, not silently lost."""
    from models import ChatMessage, Conversation, db

    db.session.add(Conversation(id='c4', title='New Chat'))
    db.session.commit()
    ai_app.echo.scripted = ['one two three four']
    ai_app.echo.fail_with = AIUnavailable('link dropped')
    ai_app.echo.fail_after = 2          # two words arrive, then it breaks

    resp = ai_client.post('/api/chat_stream',
                          json={'conv_id': 'c4', 'message': 'go'})
    payloads = _sse_payloads(resp)
    assert payloads[-1]['error'] == AIUnavailable().user_message

    saved = ChatMessage.query.filter_by(session_id='c4', role='assistant').all()
    assert len(saved) == 1 and saved[0].content == 'one two '


def test_api_chat_returns_html_insights_and_actions(ai_app, ai_client):
    _script(ai_app, json.dumps({
        'analysis': '## Summary\nYou are **fine**.',
        'insights': ['Dining is up'],
        'recommended_actions': ['Set a Dining budget'],
    }))
    data = ai_client.post('/api/chat', json={'message': 'How am I doing?'}).get_json()
    assert '<strong>fine</strong>' in data['html']
    assert data['insights'] == ['Dining is up']
    assert data['actions'] == ['Set a Dining budget']


def test_api_chat_falls_back_to_prose_when_the_reply_is_not_json(ai_app, ai_client):
    """This surface deliberately shows prose rather than erroring."""
    _script(ai_app, 'I would rather just talk about it.')
    data = ai_client.post('/api/chat', json={'message': 'hi'}).get_json()
    assert 'rather just talk' in data['html']
    assert data['insights'] == [] and data['actions'] == []


def test_api_chat_builds_the_finance_context_once(ai_app, ai_client):
    """It used to assign an unused `context` and then rebuild it inline."""
    calls = []
    import dough.services.finance_context as fc
    original = fc.build_finance_context

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    # Patched on the module that *calls* it. Before Phase 7 that was app.py;
    # the route now lives in the chat blueprint, and patching app.py would
    # silently do nothing -- the assertion below is what would have caught it.
    import dough.blueprints.chat as chat_module
    chat_module.build_finance_context = counting
    try:
        _script(ai_app, json.dumps({'analysis': 'ok'}))
        ai_client.post('/api/chat', json={'message': 'hi'})
    finally:
        chat_module.build_finance_context = original
    assert len(calls) == 1, f'built the snapshot {len(calls)} times, expected 1'


def test_rules_ai_suggest_needs_uncategorized_rows(ai_client):
    data = ai_client.post('/rules/ai-suggest', json={}).get_json()
    assert data['suggestions'] == []
    assert 'No uncategorized' in data['message']


def test_rules_ai_suggest_enriches_suggestions(ai_app, ai_client):
    from datetime import datetime

    from models import Transaction, db

    for i in range(3):
        db.session.add(Transaction(
            date=datetime(2026, 3, 1 + i), description=f'SQUARE COFFEE {i}',
            amount=-4.50, category='Uncategorized', account_name='Checking'))
    db.session.commit()

    _script(ai_app, json.dumps({'suggestions': [
        {'category': 'Dining', 'keyword': 'SQUARE COFFEE', 'reason': 'a cafe'}]}))
    data = ai_client.post('/rules/ai-suggest', json={'model': 'quick'}).get_json()
    assert len(data['suggestions']) == 1
    assert data['suggestions'][0]['category'] == 'Dining'
    assert ai_app.echo.requests[0].model == catalog.provider_id('quick')


def test_rules_ai_suggest_reports_a_provider_failure_as_doughs_message(ai_app, ai_client):
    from datetime import datetime

    from models import Transaction, db

    db.session.add(Transaction(date=datetime(2026, 3, 1), description='X',
                               amount=-1, category='Uncategorized',
                               account_name='Checking'))
    db.session.commit()
    ai_app.echo.fail_with = AIRateLimited('slow down')
    resp = ai_client.post('/rules/ai-suggest', json={})
    assert resp.status_code == 500
    assert resp.get_json()['error'] == AIRateLimited().user_message


@pytest.mark.parametrize('method,path,payload', [
    ('get', '/api/dashboard-insight', None),
    ('get', '/api/copilot/brief', None),
    ('get', '/api/investments/brief', None),
    ('post', '/api/copilot/ask', {'question': 'q'}),
    ('post', '/api/investments/ask', {'question': 'q'}),
    ('post', '/api/chat', {'message': 'q'}),
    ('post', '/rules/ai-suggest', {}),
])
def test_every_surface_degrades_without_a_provider(client, method, path, payload):
    """The conftest app has an unconfigured adapter -- the no-API-key state.

    No surface may 500. Each either reports unavailability or answers 503.
    """
    resp = getattr(client, method)(path, json=payload) if payload is not None \
        else getattr(client, method)(path)
    assert resp.status_code in (200, 503), resp.get_data(as_text=True)[:300]
    if resp.status_code == 200:
        body = resp.get_json()
        assert body in ({'insight': ''}, {'available': False}) or \
            body.get('suggestions') == [], body


def test_the_model_catalog_reaches_both_pickers(ai_client):
    """chat.html and rules.html render from the context processor."""
    for path in ('/chat', '/rules'):
        body = ai_client.get(path).get_data(as_text=True)
        for model in catalog.MODELS:
            assert model.provider_id in body, f'{path} is missing {model.provider_id}'
            assert model.label in body or model.short_description in body
