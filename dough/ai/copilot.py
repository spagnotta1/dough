"""`FinancialCopilot` — the one thing a surface talks to about money.

    Route / API / scheduler
        ↓
    FinancialCopilot          this module — coordination, caching, prioritisation
        ↓
    dough/services/*          analytics, periods, trends, health, anomalies,
        ↓                     budgets, insights, finsearch
    ai_context.build()        the summarised snapshot
        ↓
    AIService → adapter → Anthropic

## Why this exists, and why it is not a pass-through

`AIService` is the seam between the app and *a model*. It knows nothing about
money, and it should not. The analytics services know about money and nothing
about models. Between them sat a gap that every surface filled for itself, and
three problems grew in it:

1. **Duplicated computation.** The Insights hub called `anomalies.detect()`
   three times for one page — once for the list, once for the counts, once
   inside `proactive.insights()`. That was found by benchmarking rather than by
   a bug report, which is the point: nothing fails, the page is just three times
   more expensive than it looks. A chat turn assembling the same context would
   have done it a fourth time.
2. **No shared cache.** `AIService.cached` caches a *generated answer* per
   surface. Nothing cached the analytics underneath, so two surfaces on one page
   recomputed the same six months of history independently.
3. **Nowhere to decide.** "Which insights matter enough to send?" and "which
   sections does this surface need?" are one decision each, and they were being
   made — differently — at each call site.

This module is where those three live. It earns its place by doing work, not by
forwarding calls: `analytics()` below is the coordinated pass, and every public
method is built on it.

## The layering rule this respects

`dough/services/` may not import an LLM client — that is the services README's
rule, and it is why the orchestrator is *here* rather than in
`dough/services/copilot.py`. `dough/ai/` is the only package permitted to reach
both directions; `service.py` already imports `dough.services.audit` and
`dough.services.ratelimit` for exactly this reason.

Imports of `dough.services.*` are function-local throughout, matching
`service.py`. At module scope they would make `dough.ai` import the whole
analytics layer — and therefore `models` — at package import time, which is what
keeps `import dough.ai` cheap for the adapter tests that build no application.

## Caching

Two layers, deliberately distinct:

- **The analytics cache** (`analytics()`) is household-scoped and holds
  *computed figures*. Short TTL, because a freshly imported transaction should
  show up on the next page load rather than in an hour.
- **The generation cache** is `AIService.cached`, unchanged, and holds *written
  prose*. It already exists and is already household-scoped.

Both are keyed by household. `TenantScopedTTLCache` resolves the household on
every operation and raises when there is none, so a cached briefing about one
family's spending cannot be served to another — SEC-0003, and the reason no
cache in this application is keyed by time alone.
"""

from __future__ import annotations

import json
import logging

from dough.ai import persona
from dough.ai.errors import AIConfigurationError, AIError

logger = logging.getLogger(__name__)

#: How long a coordinated analytics pass stays warm. Two minutes: long enough
#: that the four surfaces on one page share one computation, short enough that
#: an import or a sync is visible on the next navigation rather than after an
#: hour of wondering why.
ANALYTICS_TTL = 120

#: Trend/baseline lookback, matching `proactive.LOOKBACK_MONTHS` and
#: `ai_context.TREND_MONTHS`. Named here too so a surface can see the number it
#: is getting without reading three modules.
LOOKBACK_MONTHS = 6

#: Which context sections each surface needs. This is the "nowhere to decide"
#: problem from the docstring, decided once. A budget coach that also ships the
#: portfolio is paying tokens to make its own answer slower.
SURFACE_SECTIONS = {
    'brief':      ('period', 'comparison', 'categories', 'budgets',
                   'insights', 'anomalies'),
    'review':     ('coverage', 'period', 'comparison', 'categories',
                   'merchants', 'trends', 'budgets', 'networth', 'insights',
                   'anomalies', 'health'),
    'budgets':    ('period', 'budgets', 'comparison', 'trends'),
    'ask':        ('coverage', 'period', 'comparison', 'categories',
                   'merchants', 'trends', 'budgets', 'recurring', 'networth',
                   'insights', 'anomalies', 'health'),
    'insights':   ('period', 'comparison', 'insights', 'anomalies', 'health',
                   'trends'),
}


class FinancialCopilot:
    """Coordinated analytics and generation for one household.

    Constructed per request rather than per application, because the household
    is per request and every figure it holds belongs to one. The *caches* it
    uses are per application and household-scoped — see the module docstring —
    so nothing is recomputed across requests that does not need to be.
    """

    def __init__(self, ai, *, cache=None, anchor=None, months=LOOKBACK_MONTHS):
        self.ai = ai
        self.anchor = anchor
        self.months = months
        self._cache = cache if cache is not None else _default_cache()
        # Per-instance memo, on top of the cross-request cache. One request that
        # asks for the analytics four times should not pay four cache lookups
        # and four deserialisations either.
        self._run = None

    # -- availability ---------------------------------------------------------

    @property
    def is_available(self):
        """Whether *generation* could succeed. Analytics never need a model."""
        return bool(self.ai) and self.ai.is_available

    def _require_ai(self):
        """Refuse when there is no model. For *questions* only.

        The asymmetry `dough/api/v1/copilot.py` established and this preserves:
        a briefing is optional furniture on a page, so it degrades to
        `available: False` and the card is omitted. A question somebody typed
        and sent cannot be satisfied at all, and telling them nothing is worse
        than telling them the feature is off.
        """
        if not self.is_available:
            raise AIConfigurationError('no AI provider is configured')

    # -- the coordinated pass -------------------------------------------------

    def analytics(self, *, window=None, refresh=False):
        """Every analytic for this household and window, computed once.

        This is the method the module exists for. Each expensive service is
        called **exactly once** and its result is threaded into everything
        downstream that needs it:

        - `anomalies.detect()` — the costliest call in the layer — feeds the
          unusual-activity list, the counts, the proactive insights and the
          context's `unusual_activity` section.
        - `periods.compare()` — four aggregate queries — feeds the headline
          movements, the insights and the context's `vs_previous_period`.

        `tests/test_copilot.py::test_one_pass_calls_each_expensive_service_once`
        counts the calls, so a future surface added to this method cannot
        quietly reintroduce the duplication this exists to remove.
        """
        from dough.services import (analytics, anomalies, health, periods,
                                    proactive, trends)

        if self._run is not None and window is None and not refresh:
            return self._run

        key = _variant(window, self.months)
        if not refresh:
            hit = self._cache.get('analytics', key)
            if hit is not None:
                if window is None:
                    self._run = hit
                return hit

        window = window or analytics.resolve_window('month', self.anchor)

        # The two expensive ones, once each.
        findings = anomalies.detect(self.months, anchor=self.anchor, limit=10)
        comparison = periods.compare(window)

        run = {
            'window': window.as_dict(),
            'summary': comparison['current'],
            'comparison': comparison,
            'findings': findings,
            'anomaly_summary': anomalies.summary(findings=findings),
            'trends': trends.category_trends(self.months, limit=8,
                                             anchor=self.anchor),
            'health': health.score(self.months, anchor=self.anchor),
            'insights': proactive.insights(anchor=self.anchor,
                                           months=self.months,
                                           findings=findings,
                                           comparison=comparison),
        }

        self._cache.set('analytics', run, key, ttl=ANALYTICS_TTL)
        if window is None:
            self._run = run
        return run

    def context(self, surface='ask', *, window=None, sections=None):
        """The snapshot for one surface, built on the coordinated pass.

        `surface` picks the section set from `SURFACE_SECTIONS`; `sections`
        overrides it outright for a caller that knows better. The precomputed
        findings and comparison are handed to `ai_context.build`, so assembling
        a context costs the sections it actually needs and not a second
        detection run.
        """
        from dough.services import ai_context

        run = self.analytics(window=window)
        chosen = sections or SURFACE_SECTIONS.get(surface) or None
        return ai_context.build(
            chosen, anchor=self.anchor, window=_window_from(run, window),
            months=self.months,
            findings=run['findings'], comparison=run['comparison'])

    def invalidate(self):
        """Drop this household's cached analytics.

        Called after anything that changes the ledger — an import, a sync, a
        category edit. Without it a two-minute TTL means a user who has just
        uploaded a statement watches an unchanged dashboard and reasonably
        concludes the upload failed.
        """
        self._run = None
        for key in self._cache_keys():
            self._cache.invalidate('analytics', key)

    def _cache_keys(self):
        """Variants `invalidate` has to clear.

        The cache interface has no prefix scan, so the keys are enumerated
        rather than guessed. Only the default window is cached across requests
        today; a named window is a one-off and expires on its own.
        """
        return [_variant(None, self.months)]

    # -- generated surfaces ---------------------------------------------------

    def brief(self, *, window=None):
        """The dashboard briefing: how the period is going, and what to do.

        Cached as prose through `AIService.cached`, whose producer builds the
        context — so a cache hit skips the analytics as well as the model call,
        which is the accounting `AIService` documents and the reason the
        producer is a closure rather than a value.
        """
        if not self.is_available:
            return {'available': False}

        def produce():
            data, _ = self.ai.generate_json(
                messages=[{'role': 'user',
                           'content': json.dumps(self.context('brief',
                                                              window=window),
                                                 default=str)}],
                system=persona.COPILOT_STYLE + '\n\n' + persona.COPILOT_BRIEF_FORMAT,
                role='brief', max_tokens=700,
                metadata={'surface': 'copilot_brief'})
            data['available'] = True
            data.setdefault('opportunities', [])
            data.setdefault('questions', [])
            return data

        return self._cached_or_unavailable('copilot_brief', produce,
                                           _variant(window, self.months))

    def monthly_review(self, *, window=None):
        """Feature 1 — the written monthly review.

        Distinct from `brief()` in scope rather than in kind: the briefing is a
        card on a dashboard and this is the whole month, so it gets the wider
        section set and more room to write. Both read the same coordinated pass,
        which is what stops them contradicting each other on the same page.
        """
        if not self.is_available:
            return {'available': False}

        def produce():
            data, _ = self.ai.generate_json(
                messages=[{'role': 'user',
                           'content': json.dumps(self.context('review',
                                                              window=window),
                                                 default=str)}],
                system=persona.COPILOT_STYLE + '\n\n' + persona.MONTHLY_REVIEW_FORMAT,
                role='brief', max_tokens=1200,
                metadata={'surface': 'copilot_monthly_review'})
            data['available'] = True
            for key in ('highlights', 'watch', 'questions'):
                data.setdefault(key, [])
            return data

        return self._cached_or_unavailable('copilot_review', produce,
                                           _variant(window, self.months))

    def budget_coaching(self):
        """Feature 5 — which budgets are healthy, which are at risk, and why.

        The projection is arithmetic, not a model output: spend so far divided
        by the fraction of the month elapsed. It is computed here and *given* to
        the model, because "you are on track to finish at $612" is exactly the
        kind of number a language model will produce plausibly and wrongly.
        """
        if not self.is_available:
            return {'available': False}
        context = self.context('budgets')
        projected = _project_budgets(context.get('budgets'))

        def produce():
            payload = dict(context, budget_projection=projected)
            data, _ = self.ai.generate_json(
                messages=[{'role': 'user',
                           'content': json.dumps(payload, default=str)}],
                system=persona.COPILOT_STYLE + '\n\n' + persona.BUDGET_COACH_FORMAT,
                role='brief', max_tokens=800,
                metadata={'surface': 'copilot_budget_coach'})
            data['available'] = True
            data.setdefault('budgets', [])
            data['projection'] = projected
            return data

        return self._cached_or_unavailable('copilot_budget_coach', produce)

    def affordability(self, *, one_off=0.0, monthly=0.0, label=None,
                      months=None):
        """Feature 9 — can they afford it, and what does the answer depend on?

        The verdict is decided by `dough/services/affordability.py` and handed
        to the model already made. That separation is the whole design: a
        language model asked "can I afford a $40,000 car" will produce a
        confident, plausible, unverifiable answer, and the one thing it must not
        do here is decide. It explains a band that arithmetic chose.

        Not cached. A scenario is a one-off question with caller-supplied
        numbers, so the cache key would be as large as the input and the hit
        rate would be zero.
        """
        from dough.services import affordability

        scenario = affordability.assess(
            one_off=one_off, monthly=monthly, label=label,
            months=months or self.months, anchor=self.anchor)

        if not self.is_available:
            # The structured assessment is still the useful half, and it needed
            # no model to produce. Returning it without prose beats returning
            # nothing at all.
            return dict(scenario, available=False)

        try:
            data, _ = self.ai.generate_json(
                messages=[{'role': 'user',
                           'content': json.dumps(scenario, default=str)}],
                system=(persona.COPILOT_STYLE + '\n\n' + persona.COPILOT_GROUNDING
                        + '\n\n' + persona.AFFORDABILITY_FORMAT),
                role='ask', max_tokens=800,
                metadata={'surface': 'copilot_affordability'})
        except AIError as exc:
            logger.warning('affordability narration failed: %s', exc)
            return dict(scenario, available=False, reason=exc.user_message)

        # The computed verdict wins over anything the model wrote. Merged in
        # this order deliberately: if a reply ever contained a `verdict` key it
        # would be the model's opinion, and this is not a question it gets a
        # vote on.
        data.update(scenario)
        data['available'] = True
        return data

    def investments(self):
        """Feature 8 — a plain-English read on the portfolio.

        Built on `wealth_context()`, which derives from `wealth_snapshot()` —
        the same single derivation the Investments page renders and the existing
        `/api/v1/copilot/investments/brief` sends. Reusing it is what keeps the
        copilot from narrating a figure the page does not show.

        What this adds over that endpoint is the grounding contract and a shape
        that separates allocation, diversification and performance, so a reader
        can find the part they wanted.
        """
        if not self.is_available:
            return {'available': False}

        def produce():
            from dough.services.finance_context import wealth_context

            data, _ = self.ai.generate_json(
                messages=[{'role': 'user',
                           'content': json.dumps(wealth_context(), default=str)}],
                system=(persona.WEALTH_STYLE + '\n\n' + persona.COPILOT_GROUNDING
                        + '\n\n' + persona.INVESTMENT_REVIEW_FORMAT),
                role='brief', max_tokens=1000,
                metadata={'surface': 'copilot_investment_review'})
            data['available'] = True
            for key in ('observations', 'questions'):
                data.setdefault(key, [])
            return data

        return self._cached_or_unavailable('copilot_investment_review', produce)

    # -- questions ------------------------------------------------------------

    def answer(self, question, *, history=None, max_tokens=700):
        """Answer a question, streamed, **retrieving before generating**.

        The retrieval step is the point. `finsearch.search()` parses the
        question, runs the query it implies, and returns the figures — so when
        somebody asks "how much did I spend on restaurants last quarter?" the
        model is handed the total rather than a pile of transactions to add up.
        A total a model computed in prose is indistinguishable from a real one
        and is wrong often enough to matter.

        When the parse matches nothing the retrieval block is omitted rather
        than sent empty, and the model works from the general snapshot. An empty
        result presented as an answer is how "you spent $0 on that" gets said to
        somebody who spent plenty.

        Yields text chunks. Streaming for the reason `chat.py` gives: an answer
        takes seconds and a client showing nothing until it lands feels broken.
        """
        self._require_ai()

        system = self.system_prompt(question)
        messages = list(history or []) + [{'role': 'user', 'content': question}]
        return self.ai.stream_text(
            messages=messages, system=system, role='ask',
            max_tokens=max_tokens, cache_system=False,
            metadata={'surface': 'copilot_ask'})

    def system_prompt(self, question=None):
        """The full system prompt for a question, retrieval included.

        Public because it is the thing worth asserting on in a test — building
        it is where a surface either does or does not ground its answer, and a
        test that had to start a stream to check would be testing the adapter.
        """
        from dough.services import finsearch

        blocks = [persona.COPILOT_STYLE, persona.COPILOT_GROUNDING]

        retrieved = None
        if question:
            try:
                retrieved = finsearch.search(question)
            except Exception as exc:            # pragma: no cover - defensive
                # Retrieval is an enhancement. A parser bug must degrade to the
                # general snapshot, not fail the question.
                logger.warning('copilot retrieval failed: %s', exc)
                retrieved = None

        if retrieved and retrieved.get('matched'):
            blocks.append(
                'Figures retrieved for this specific question, computed from '
                'the ledger. Prefer these over anything you would derive '
                'yourself:\n' + json.dumps(retrieved, default=str, indent=2))

        blocks.append('General financial snapshot:\n'
                      + json.dumps(self.context('ask'), default=str, indent=2))
        blocks.append(persona.COPILOT_ASK_RULES)
        return '\n\n'.join(blocks)

    # -- insights -------------------------------------------------------------

    def insights(self, limit=5):
        """The proactive observations, from the coordinated pass. No model."""
        return self.analytics()['insights'][:limit]

    def health(self):
        return self.analytics()['health']

    def unusual_activity(self, limit=8):
        return self.analytics()['findings'][:limit]

    # -- internals ------------------------------------------------------------

    def _cached_or_unavailable(self, surface, produce, variant=''):
        """Run a cached producer, degrading to `available: false` on failure.

        The same contract `dough/api/v1/copilot.py` established: a briefing is
        optional furniture, so a failure logs its reason and tells the client
        only that the card has nothing to show. Centralised here so a new
        generated surface inherits it instead of reimplementing it.
        """
        try:
            return self.ai.cached(surface, produce, variant=variant)
        except AIError as exc:
            logger.warning('%s unavailable: %s', surface, exc)
            return {'available': False, 'reason': exc.user_message}
        except Exception as exc:
            logger.error('%s failed: %s', surface, exc)
            return {'available': False}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _default_cache():
    from dough.services.cache import TenantScopedTTLCache

    return TenantScopedTTLCache(ttl=ANALYTICS_TTL)


def _variant(window, months):
    """A cache variant that distinguishes windows and lookbacks.

    Two surfaces asking about different periods must not share an entry — the
    bug `dough/api/v1/copilot.py` guards with the same idea, where a briefing
    about March–June must not be served to somebody now asking about this month.
    """
    if window is None:
        return f'default|{months}'
    return f'{window.start}|{window.end}|{months}'


def _window_from(run, window):
    """The `Window` the run was computed for.

    Rebuilt from the run's own dictionary rather than passed alongside it, so
    the context is always built for the period the analytics actually covered
    even when the caller supplied nothing.
    """
    if window is not None:
        return window

    from dough.services.analytics import custom_window

    stored = run['window']
    return custom_window(stored['start'], stored['end'])


def _project_budgets(budgets):
    """Where each budget lands at month end, at the current pace.

    Arithmetic, not a model output — see `budget_coaching`. A budget 60% spent
    on the 15th of a 31-day month is pacing to about 124%.

    The projection itself is `dough.services.budgets.project`. It used to be
    computed here, which put the most useful number on the Budgets page inside
    the one package that talks to a model and behind a method — `budget_
    coaching()` — that no route and no template ever called. The page shows the
    projection now, and it must be the same projection: a card reading "on pace
    for $612" beside a briefing that says $580 is the same class of bug as two
    answers to "am I over budget".

    Returns `available: False` rather than an empty list when there are no
    budgets, matching the section.
    """
    from dough.services.budgets import project

    if not budgets or not budgets.get('available'):
        return {'available': False, 'reason': 'no budgets set'}

    elapsed_pct = max(budgets.get('month_progress_pct') or 0, 1)
    rows = []
    for budget in budgets.get('budgets', []):
        limit = float(budget.get('limit') or 0.0)
        spent = float(budget.get('spent') or 0.0)
        projected = project(spent, elapsed_pct)
        rows.append({
            'category': budget['category'],
            'limit': limit,
            'spent': spent,
            'projected_month_end': projected,
            'projected_pct_of_limit': (round(projected / limit * 100, 1)
                                       if limit > 0 else None),
            'on_track': projected <= limit if limit > 0 else None,
            # How much a projection made this early is worth. Naming it stops
            # the model presenting day-three arithmetic as a forecast.
            'confidence': ('low' if elapsed_pct < 25
                           else 'moderate' if elapsed_pct < 60 else 'high'),
        })
    rows.sort(key=lambda r: -(r['projected_pct_of_limit'] or 0))
    return {'available': True, 'month_elapsed_pct': round(elapsed_pct),
            'budgets': rows}


def current_copilot(**kwargs):
    """A copilot for the current request, wired to this app's `AIService`.

    A function rather than an app extension because the copilot is per request
    (it carries a household's figures) while `AIService` is per application.
    Mirrors `current_ai()`, which is what every route already calls.
    """
    from dough.ai.service import current_ai

    return FinancialCopilot(current_ai(), **kwargs)


__all__ = ['FinancialCopilot', 'current_copilot', 'SURFACE_SECTIONS',
           'ANALYTICS_TTL', 'LOOKBACK_MONTHS']
