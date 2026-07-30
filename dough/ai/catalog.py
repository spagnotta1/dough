"""The model catalog: one list, four former copies.

Before this module the same three models were declared in four places, each
with its own labels and its own idea of the default:

| Where | What it declared |
|---|---|
| `app.py::rules_ai_suggest` | the allow-set, default `claude-sonnet-4-6` |
| `app.py` (7 other sites) | a hardcoded id per call site |
| `templates/chat.html` | `MODELS[]` with names + descriptions, default sonnet |
| `templates/rules.html` | picker buttons *and* `AI_MODEL_DESCS`, default haiku |

Nothing kept them in step. The two templates disagreed about the default and
described the same models differently, and a fifth id could be added to one
picker and silently rejected by the server's allow-set.

Now: this module is the source, `AIService` resolves through it, and the
templates receive `MODELS` from a context processor. `tests/test_ai_adapter.py`
asserts the templates contain no hardcoded model id, so the duplication cannot
grow back.

**Model versions are deliberately unchanged in this phase.** These three ids are
a generation behind the current 5-series and are all still valid. Upgrading them
is an operational change with its own risk -- on the 5-series, adaptive thinking
is on by default and `max_tokens` caps thinking *plus* output, so the 200-token
dashboard insight and the 700/900-token briefings would truncate. That needs its
own `max_tokens` audit and must not ride along with an architecture change.

Allowed:   stdlib only
Must not:  app, models, flask, anthropic, other dough packages
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Model:
    """One selectable model.

    `key` is the stable internal name. It exists so a future provider swap does
    not change what the client stores: `localStorage['check-active-model']`
    currently holds a raw Anthropic id, and a reader who picked "Deep" should
    still get the deep model after an upgrade renames it. `resolve()` accepts
    either the key or the provider id for exactly that reason.
    """

    key: str
    provider_id: str
    label: str
    description: str
    #: Short description for the compact picker in rules.html.
    short_description: str
    provider: str = 'anthropic'
    #: Rough speed/cost tier, for the UI to order and annotate by.
    tier: str = 'balanced'

    def as_dict(self):
        return {'key': self.key, 'id': self.provider_id, 'label': self.label,
                'description': self.description,
                'short_description': self.short_description,
                'provider': self.provider, 'tier': self.tier}


#: Ordered fastest to most thorough -- the order the pickers render in.
MODELS = (
    Model(key='quick',
          provider_id='claude-haiku-4-5-20251001',
          label='Quick',
          description=('Fastest and cheapest. Great for balances, lookups, '
                       'and quick back-and-forth.'),
          short_description='Fastest & most affordable',
          tier='fast'),
    Model(key='balanced',
          provider_id='claude-sonnet-4-6',
          label='Balanced',
          description='Strong reasoning at good speed. The best all-round choice.',
          short_description='Balanced speed and accuracy',
          tier='balanced'),
    Model(key='deep',
          provider_id='claude-opus-4-8',
          label='Deep',
          description=('Most thorough. Worth it for planning, trade-offs, '
                       'and detailed analysis.'),
          short_description='Deepest reasoning, highest cost',
          tier='deep'),
)

#: The model a request gets when it names none. Matches what chat.html used.
DEFAULT_MODEL = 'balanced'

# Named roles rather than ids at the call sites. A route asks for the model that
# suits its job; which id that is, is a decision recorded here.
#
# These preserve the existing per-endpoint choices exactly -- the briefings and
# the dashboard insight were on Haiku, the streaming answers on Sonnet -- but
# they make the *reason* visible, and they mean re-tiering every short briefing
# is one edit here instead of four hardcoded ids in app.py.
ROLES = {
    # A 2-3 sentence read, on the dashboard, wanted in about a second.
    'insight': 'quick',
    # Short JSON briefings. Structured output, small, latency-sensitive.
    'brief': 'quick',
    # Streamed conversational answers, where reasoning quality shows.
    'ask': 'balanced',
    # One-shot analysis with a large context (the chat non-streaming path).
    'analysis': 'balanced',
    # Rule suggestion over up to 200 descriptions; the user picks, this is the
    # fallback when they have not.
    'suggest': 'balanced',
}

_BY_KEY = {m.key: m for m in MODELS}
_BY_PROVIDER_ID = {m.provider_id: m for m in MODELS}


def all_models():
    """The catalog as plain dicts, for JSON and for templates."""
    return [m.as_dict() for m in MODELS]


def get(name) -> Optional[Model]:
    """Look up by internal key or provider id. None when unknown."""
    if not name:
        return None
    name = str(name).strip()
    return _BY_KEY.get(name) or _BY_PROVIDER_ID.get(name)


def resolve(name=None, *, role=None, default=None) -> Model:
    """Return a `Model`, never raising and never returning None.

    Resolution order: an explicit `name`, then the model for `role`, then
    `default`, then `DEFAULT_MODEL`.

    Unknown names fall through rather than raising. That is deliberate: the name
    usually comes from `localStorage` or a POST body, so it is attacker- and
    stale-cache-controlled, and the old code's `if model not in {...}` silent
    fallback is the behaviour to preserve. A typo must not turn into a 500 on a
    page someone is reading their balance on.
    """
    for candidate in (name, ROLES.get(role) if role else None, default,
                      DEFAULT_MODEL):
        found = get(candidate)
        if found is not None:
            return found
    # DEFAULT_MODEL is validated by a test, so this is unreachable in practice.
    return MODELS[0]


def provider_id(name=None, *, role=None) -> str:
    """The wire id for a name or role. Convenience over `resolve().provider_id`."""
    return resolve(name, role=role).provider_id


__all__ = ['Model', 'MODELS', 'DEFAULT_MODEL', 'ROLES', 'all_models', 'get',
           'resolve', 'provider_id']
