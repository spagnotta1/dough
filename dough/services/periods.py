"""Feature 2 — what changed between two periods, as structured findings.

The engine answers one question — "what is different?" — for any two windows:
month against month, quarter against quarter, year against year, or two dates a
user picked. It returns **data, never prose**. Turning a finding into a sentence
is `dough/ai/`'s job, and the separation is the point: the model receives a list
of changes that were computed, so the only thing it can get wrong is the wording.
A model handed two raw snapshots and asked what changed would be doing the
subtraction itself, and a subtraction done in a language model is a number
nobody can check.

## Why a change needs two thresholds

A category that went from $4 to $8 doubled. Reporting that as "dining spending
up 100%" is technically true and completely useless, and an assistant that leads
with it looks broken. A category that went from $2,000 to $2,090 moved $90,
which is real money and a 4.5% wobble that means nothing.

So a finding must clear **both** a percentage and a dollar floor — the same
two-part test `dashboard_intel.MATERIAL_SWING_PCT` / `MATERIAL_SWING_USD` already
applies to the attention centre, and the constants are imported from there
rather than re-chosen, so the two surfaces cannot disagree about what "material"
means.

## Appeared and disappeared

A category with no baseline cannot have a percentage — `pct_change` returns
None, deliberately, and every consumer has to deal with it. Those are not
skipped: "you spent $340 on Travel and did not last month" is often the most
interesting line in the whole comparison. They are classified as `appeared` /
`disappeared` and carry `pct: None`, so a formatter that prints a percentage
cannot print a fabricated one.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from dashboard_intel import MATERIAL_SWING_PCT, MATERIAL_SWING_USD
from dough.services import analytics
from dough.services.analytics import (custom_window, pct_change,
                                      preceding_window, resolve_window)

#: A category must move at least this share of itself AND this many dollars.
#: Imported from `dashboard_intel` so the attention centre and the comparison
#: engine cannot drift apart on what counts as worth mentioning.
MIN_CHANGE_PCT = MATERIAL_SWING_PCT
MIN_CHANGE_USD = MATERIAL_SWING_USD

#: Income and net-flow moves are judged on a lower bar than a single category:
#: a 10% swing in what a household earns is a bigger event than a 25% swing in
#: what it spent on coffee.
MIN_INCOME_CHANGE_PCT = 5.0
MIN_INCOME_CHANGE_USD = 100.0

#: How many category findings a comparison returns. Past about eight, a
#: "what changed" answer stops being a summary and becomes the ledger again.
DEFAULT_FINDING_LIMIT = 8

#: Directions a finding can carry. `appeared` and `disappeared` exist because a
#: percentage cannot be computed against a zero baseline -- see the docstring.
DIRECTIONS = ('increase', 'decrease', 'appeared', 'disappeared')


def compare(current, previous=None, *, account=None, limit=DEFAULT_FINDING_LIMIT):
    """Everything that changed between two windows, as structured findings.

    `previous` defaults to the period immediately before `current`, of the same
    shape — the comparison a user means when they do not name one.

    The return value is a dictionary rather than a list of findings because the
    totals are part of the answer: a formatter needs "spending fell 12%" as well
    as the categories that drove it, and recomputing the headline from the
    findings would exclude every category that fell below the threshold.
    """
    previous = previous or preceding_window(current)

    now = analytics.period_summary(current, account=account)
    before = analytics.period_summary(previous, account=account)

    return {
        'current': now,
        'previous': before,
        'totals': _totals(now, before),
        'categories': _category_findings(now, before, limit=limit),
        'headline': _headline(now, before),
    }


def compare_kind(kind='month', anchor=None, *, account=None,
                 limit=DEFAULT_FINDING_LIMIT):
    """`compare` for a named period — 'month', 'quarter' or 'year'.

    The convenience that stops four callers each writing
    `compare(resolve_window(kind, anchor))` slightly differently.
    """
    window = resolve_window(kind, anchor)
    return compare(window, account=account, limit=limit)


def compare_ranges(start_a, end_a, start_b, end_b, *, account=None,
                   limit=DEFAULT_FINDING_LIMIT):
    """`compare` for two hand-picked ranges.

    Ordered so the *later* range is the current one regardless of the order the
    arguments arrive in. A user who picks the older range second means to
    compare forwards, and reporting their improvement as a decline because of
    argument order is a bug they cannot see the cause of.
    """
    first = custom_window(start_a, end_a)
    second = custom_window(start_b, end_b)
    if second.start < first.start:
        first, second = second, first
    return compare(second, first, account=account, limit=limit)


# ── The findings ────────────────────────────────────────────────────────────

def _totals(now, before):
    """The four headline movements: spending, income, net flow, savings rate.

    Always returned, all four, whether or not they cleared a threshold. These
    are the numbers a comparison is *about* — suppressing "income was flat"
    would leave a formatter unable to say the useful thing that nothing moved.
    """
    out = {}
    for key in ('spending', 'income', 'net'):
        current, previous = now[key], before[key]
        out[key] = {
            'current': current,
            'previous': previous,
            'delta': round(current - previous, 2),
            'pct': pct_change(current, previous),
            'material': _is_material(
                current - previous, pct_change(current, previous),
                min_pct=(MIN_INCOME_CHANGE_PCT if key != 'spending'
                         else MIN_CHANGE_PCT),
                min_usd=(MIN_INCOME_CHANGE_USD if key != 'spending'
                         else MIN_CHANGE_USD)),
        }

    # The savings rate is a percentage already, so its "change" is a difference
    # in points, not a percentage of a percentage. Reporting a move from 10% to
    # 20% as "up 100%" is arithmetically defensible and reliably misread.
    rate_now, rate_before = now['savings_rate'], before['savings_rate']
    out['savings_rate'] = {
        'current': rate_now,
        'previous': rate_before,
        'delta_points': (None if rate_now is None or rate_before is None
                         else round(rate_now - rate_before, 1)),
        'pct': None,
    }
    return out


def _category_findings(now, before, *, limit):
    """Per-category movements worth mentioning, biggest dollar swing first.

    Ranked by absolute dollars rather than by percentage. The percentage is what
    makes a change *interesting*; the dollars are what make it *matter*, and a
    list sorted by percentage puts a $6 swing above a $600 one.
    """
    categories = set(now['by_category']) | set(before['by_category'])
    findings = []

    for category in categories:
        current = now['by_category'].get(category, 0.0)
        previous = before['by_category'].get(category, 0.0)
        delta = round(current - previous, 2)
        if not delta:
            continue

        pct = pct_change(current, previous)
        if previous == 0:
            direction = 'appeared'
        elif current == 0:
            direction = 'disappeared'
        else:
            direction = 'increase' if delta > 0 else 'decrease'

        # An appearance or disappearance has no percentage to test, so it is
        # judged on dollars alone -- otherwise a brand-new $400 Travel category
        # would be dropped for failing a percentage test it cannot take.
        if direction in ('appeared', 'disappeared'):
            if abs(delta) < MIN_CHANGE_USD:
                continue
        elif not _is_material(delta, pct):
            continue

        findings.append({
            'category': category,
            'direction': direction,
            'current': current,
            'previous': previous,
            'delta': delta,
            'pct': pct,
        })

    findings.sort(key=lambda f: -abs(f['delta']))
    return findings[:limit]


def _headline(now, before):
    """The single biggest driver of the spending difference, or None.

    Returned separately from the findings list because "what changed" almost
    always has one answer, and a formatter that has to pick one out of eight
    will sometimes pick a different one than the totals imply. None when nothing
    cleared the bar -- a genuinely flat month should be reported as flat, not
    have its largest rounding error promoted to a headline.
    """
    findings = _category_findings(now, before, limit=1)
    if not findings:
        return None
    top = findings[0]
    spending_delta = round(now['spending'] - before['spending'], 2)
    return {
        'category': top['category'],
        'direction': top['direction'],
        'delta': top['delta'],
        'pct': top['pct'],
        # How much of the total move this one category explains. Lets a
        # formatter say "almost all of it" or "one of several" truthfully
        # instead of implying a single cause every time.
        'share_of_total_change': (
            round(abs(top['delta']) / abs(spending_delta) * 100.0, 1)
            if spending_delta else None),
    }


def _is_material(delta, pct, *, min_pct=MIN_CHANGE_PCT, min_usd=MIN_CHANGE_USD):
    """Both tests, or it is noise. See the module docstring."""
    if abs(delta) < min_usd:
        return False
    if pct is None:
        return True
    return abs(pct) >= min_pct


__all__ = ['compare', 'compare_kind', 'compare_ranges', 'DIRECTIONS',
           'MIN_CHANGE_PCT', 'MIN_CHANGE_USD', 'DEFAULT_FINDING_LIMIT']
