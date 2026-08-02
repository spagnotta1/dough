"""Feature 3 — which categories and merchants are actually trending.

A comparison of two periods (`periods.py`) answers "what changed?". It cannot
answer "is this a trend?", and the difference matters: one expensive month is
not a direction of travel, and telling somebody their restaurant spending is
"rising" on the strength of a single birthday dinner is the kind of confident
wrongness that costs an assistant its credibility.

So a trend here needs a **slope across several months**, not a delta across two.

## How the direction is decided

Ordinary least squares on the monthly series, which is the cheapest honest
answer. The slope is in dollars per month; it is then expressed as a share of
the series mean so that "rising" means the same thing for a $2,000 rent line and
a $60 streaming line.

Two guards stop a slope becoming a claim:

- **`MIN_MONTHS_FOR_TREND`.** Three points is the minimum where a line means
  anything at all, and even that is thin — the returned `confidence` says so.
- **R², reported and never hidden.** A slope through scattered points has a
  direction and no meaning. Series below `WEAK_FIT_R2` are returned with
  `direction: 'volatile'` rather than being forced into rising or falling, and
  a formatter is expected to say "moves around a lot" rather than pick one.

`numpy` is available (the services README lists it) but is not used: the fit is
five lines of arithmetic over at most twenty-four points, and the dependency
would buy nothing except a slower import in the sync scheduler.

## Inflation-ish observations

"Grocery inflation" is a real thing a user wants named, and it is not a slope in
total spending — it is the same basket costing more, which shows up as *spend
per transaction* rising while the transaction count stays flat. `unit_cost_trend`
reports that separately, so "you are shopping more often" and "each shop costs
more" stop being one indistinguishable number.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from sqlalchemy import Float, cast, func

from dough.services import analytics
from dough.services.analytics import lookback_window, month_keys_between
from models import Transaction, db

#: Default lookback. Six months is long enough for a direction to be real and
#: short enough that a job change last spring is not still steering the answer.
DEFAULT_LOOKBACK_MONTHS = 6

#: Below this many months with data, no trend is reported at all.
MIN_MONTHS_FOR_TREND = 3

#: |slope per month| as a share of the mean, above which a series is moving.
#: Under 3% a month is drift, and labelling drift as a trend produces an
#: assistant that reports six rising categories every month.
TREND_THRESHOLD_PCT = 3.0

#: R² below this means the line does not describe the points. Reported as
#: 'volatile' rather than forced into a direction.
WEAK_FIT_R2 = 0.35

#: Standard deviation as a share of the mean, above which a badly-fitting series
#: is genuinely swinging rather than merely level. Both tests have to pass to
#: earn 'volatile': a steady $300 every month also fits no line, and calling
#: that volatile would be as wrong as calling a spike a trend.
VOLATILE_DISPERSION = 0.25

#: A category must average at least this much a month to be worth trending.
#: Without it, a $9 category that drifted to $14 outranks everything.
MIN_MONTHLY_AVERAGE = 25.0


def category_trends(months=DEFAULT_LOOKBACK_MONTHS, *, account=None,
                    limit=None, anchor=None):
    """Every category's direction of travel over the lookback window.

    Sorted by absolute monthly slope in dollars — the categories whose movement
    is worth the most money, first. `limit` trims that list; None returns all of
    them, which is what `ai_context` wants so the model can be asked about a
    category the ranking did not promote.
    """
    start, end = _window_bounds(months, anchor)
    axis = month_keys_between(start, end)
    series = analytics.monthly_category_series(start, end, account=account)

    trends = []
    for category in {c for month in series.values() for c in month}:
        # Reindexed onto the full axis: a month where a category did not appear
        # is a real zero for that category, and dropping it would fit a line
        # through only the months somebody spent, which always trends flat.
        points = [series.get(key, {}).get(category, 0.0) for key in axis]
        trend = _describe(points, axis)
        if trend is None or trend['average'] < MIN_MONTHLY_AVERAGE:
            continue
        trend['category'] = category
        trends.append(trend)

    trends.sort(key=lambda t: -abs(t['slope_per_month']))
    return trends[:limit] if limit else trends


def merchant_trends(months=DEFAULT_LOOKBACK_MONTHS, *, account=None, limit=10,
                    min_months_seen=MIN_MONTHS_FOR_TREND, anchor=None):
    """Recurring merchants whose monthly cost is moving.

    Only merchants seen in at least `min_months_seen` distinct months are
    considered — a merchant visited twice has no trend, only two prices, and
    `anomalies.py` is where a single surprising charge belongs.
    """
    start, end = _window_bounds(months, anchor)
    axis = month_keys_between(start, end)

    rows = (db.session.query(
        Transaction.description,
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(-cast(Transaction.amount, Float)).label('total'))
        .filter(Transaction.date >= start, Transaction.date <= end,
                Transaction.amount < 0,
                ~func.lower(func.trim(Transaction.category)).in_(
                    analytics.TRANSFER_CATEGORIES))
        .group_by(Transaction.description, 'month'))
    if account and account not in ('both', 'all'):
        rows = rows.filter(Transaction.account_name == account)

    by_merchant = {}
    for row in rows.all():
        by_merchant.setdefault(row.description, {})[row.month] = round(
            float(row.total or 0.0), 2)

    trends = []
    for description, monthly in by_merchant.items():
        if len(monthly) < min_months_seen:
            continue
        points = [monthly.get(key, 0.0) for key in axis]
        trend = _describe(points, axis)
        if trend is None:
            continue
        trend['description'] = description
        trend['months_seen'] = len(monthly)
        trends.append(trend)

    trends.sort(key=lambda t: -abs(t['slope_per_month']))
    return trends[:limit] if limit else trends


def unit_cost_trend(category, months=DEFAULT_LOOKBACK_MONTHS, *, account=None,
                    anchor=None):
    """Is each purchase costing more, or are there simply more purchases?

    The distinction "grocery inflation" actually names. Total spend rising tells
    you nothing about which of the two is happening, and the advice differs
    completely: one is a price you cannot control, the other is a habit you can.

    Returns None when the category has too few months to say anything.
    """
    start, end = _window_bounds(months, anchor)
    axis = month_keys_between(start, end)

    rows = (db.session.query(
        func.strftime('%Y-%m', Transaction.date).label('month'),
        func.sum(-cast(Transaction.amount, Float)).label('total'),
        func.count(Transaction.id).label('n'))
        .filter(Transaction.date >= start, Transaction.date <= end,
                Transaction.amount < 0,
                func.lower(Transaction.category) == (category or '').lower())
        .group_by('month'))
    if account and account not in ('both', 'all'):
        rows = rows.filter(Transaction.account_name == account)

    monthly = {r.month: (float(r.total or 0.0), int(r.n or 0))
               for r in rows.all()}
    if len(monthly) < MIN_MONTHS_FOR_TREND:
        return None

    # Only months the category actually occurred in. A zero-spend month has no
    # average basket size, and feeding it in as 0 would manufacture a fall.
    seen = [key for key in axis if monthly.get(key, (0.0, 0))[1] > 0]
    if len(seen) < MIN_MONTHS_FOR_TREND:
        return None

    per_txn = [monthly[key][0] / monthly[key][1] for key in seen]
    counts = [float(monthly[key][1]) for key in seen]

    return {
        'category': category,
        'months': seen,
        'average_purchase': _describe(per_txn, seen),
        'purchase_count': _describe(counts, seen),
        'reading': _unit_cost_reading(_describe(per_txn, seen),
                                      _describe(counts, seen)),
    }


def _unit_cost_reading(cost, count):
    """Which of the two stories the numbers support, or neither."""
    if cost is None or count is None:
        return 'unknown'
    cost_up = cost['direction'] == 'rising'
    count_up = count['direction'] == 'rising'
    if cost_up and not count_up:
        return 'prices_rising'
    if count_up and not cost_up:
        return 'buying_more_often'
    if cost_up and count_up:
        return 'both'
    return 'stable'


# ── The fit ─────────────────────────────────────────────────────────────────

def _describe(points, axis=None):
    """Robust direction of a monthly series, with its goodness of fit.

    Returns None below `MIN_MONTHS_FOR_TREND` observed points — not a flat
    trend, None. "I do not have enough history to say" is a different answer
    from "it is flat", and collapsing the two is how an assistant ends up making
    a confident statement about two data points.

    ## Why the slope is Theil–Sen and not least squares

    This was ordinary least squares first, and `tests/test_trends.py::
    test_one_expensive_month_does_not_make_a_rising_trend` failed against it.
    Five months at $300 and one birthday dinner at $1,200 fits a line rising
    $129 a month at R² = 0.42 — over the weak-fit bar, so it would have been
    reported as "your dining is rising" on the strength of one meal. That is
    precisely the confident wrongness this module exists to avoid, and no
    threshold tuning fixes it: OLS minimises *squared* error, so the single
    furthest point is by construction the one steering the line.

    Theil–Sen takes the median of the pairwise slopes, so a lone spike moves
    the estimate by at most one rank. On that same series it returns zero. It
    costs O(n²) — 276 pairs at the twenty-four-month ceiling, which is nothing.
    """
    values = _trim_leading_absence([float(v) for v in points])
    if values is None:
        return None
    values, offset = values
    if len(values) < MIN_MONTHS_FOR_TREND:
        return None

    n = len(values)
    mean_y = sum(values) / n
    slope = _theil_sen(values)

    # How well that line describes the points. A perfectly flat series has no
    # variance to explain; it is reported as a perfect fit because a flat line
    # does describe it exactly.
    total_ss = sum((y - mean_y) ** 2 for y in values)
    if total_ss:
        # Intercept through the medians, which is the estimator Theil–Sen pairs
        # with -- using the mean here would hand the outlier back its influence
        # through the back door.
        intercept = _median(values) - slope * _median(list(range(n)))
        residual_ss = sum((y - (slope * x + intercept)) ** 2
                          for x, y in enumerate(values))
        r_squared = max(0.0, 1.0 - residual_ss / total_ss)
        dispersion = (total_ss / n) ** 0.5 / mean_y if mean_y else 0.0
    else:
        r_squared, dispersion = 1.0, 0.0

    relative = (abs(slope) / mean_y * 100.0) if mean_y else 0.0

    # Volatility is tested before flatness, not after. A series that swings by
    # $800 a month around a level mean has a near-zero slope, and calling that
    # "flat" tells the reader the opposite of what is happening.
    if r_squared < WEAK_FIT_R2 and dispersion >= VOLATILE_DISPERSION:
        direction = 'volatile'
    elif relative < TREND_THRESHOLD_PCT:
        direction = 'flat'
    else:
        direction = 'rising' if slope > 0 else 'falling'

    return {
        'direction': direction,
        'slope_per_month': round(slope, 2),
        'slope_pct_per_month': round(relative if slope >= 0 else -relative, 1),
        'average': round(mean_y, 2),
        'r_squared': round(r_squared, 2),
        'months': n,
        'first': round(values[0], 2),
        'last': round(values[-1], 2),
        'total': round(sum(values), 2),
        'confidence': _confidence(n, r_squared),
        'series': ({key: round(value, 2)
                    for key, value in zip(axis[offset:], values)}
                   if axis else None),
    }


def _trim_leading_absence(values):
    """Drop the zeros before a category's first appearance.

    A zero *after* a category has been seen is a real and meaningful zero — it
    is how "you stopped eating out" shows up, and
    `test_a_missing_month_counts_as_zero_not_as_absent` depends on it surviving.
    A zero *before* the first transaction is not a zero at all; it is the
    category not existing yet, and keeping it fabricates history.

    The difference is not cosmetic. A category first seen two months ago
    arrives as `[0, 0, 0, 0, 400, 800]` — six points, enough to clear the
    minimum, and a fit through four invented zeros that reports a steep,
    high-confidence rise from two real observations.

    Returns `(values, offset)`, or None when nothing was ever observed.
    """
    first = next((i for i, value in enumerate(values) if value), None)
    if first is None:
        return None
    return values[first:], first


def _theil_sen(values):
    """Median of the pairwise slopes. See `_describe` for why not least squares."""
    slopes = [(values[j] - values[i]) / (j - i)
              for i in range(len(values)) for j in range(i + 1, len(values))]
    return _median(slopes) if slopes else 0.0


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _confidence(months, r_squared):
    """How much weight a reader should put on this direction.

    Named rather than left as two raw numbers because the caller that most needs
    it is a language model, and 'low' is far harder to misreport than
    'r_squared: 0.41 over 3 months'.
    """
    if months < 4 or r_squared < WEAK_FIT_R2:
        return 'low'
    if months < 6 or r_squared < 0.7:
        return 'moderate'
    return 'high'


def _window_bounds(months, anchor=None):
    """The lookback window as a (start, end) pair.

    A pair rather than the `Window` itself because every caller here feeds the
    two ends straight into a query and an axis, and unpacking at four call sites
    reads worse than unpacking once.
    """
    window = lookback_window(months, anchor)
    return window.start, window.end


__all__ = ['category_trends', 'merchant_trends', 'unit_cost_trend',
           'DEFAULT_LOOKBACK_MONTHS', 'MIN_MONTHS_FOR_TREND',
           'TREND_THRESHOLD_PCT', 'WEAK_FIT_R2', 'VOLATILE_DISPERSION',
           'MIN_MONTHLY_AVERAGE']
