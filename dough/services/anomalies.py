"""Feature 6 — unusual financial activity, with the reason attached.

The ledger already carries an `anomaly_score` column, written by
`dough/services/transactions.py::compute_anomaly_scores` — an IsolationForest
over amount and category that marks a row `-1.0` and nothing else. It answers
"is this row odd?" and cannot answer "why?", which is the only part a user can
act on. "$847 at Delta is unusual" is actionable; a flagged row is a puzzle.

So this module does not replace that score. It adds the kinds of unusual that
have a *nameable cause*, each detected by its own test, each returning the
figures behind it:

| kind | the question it answers |
| --- | --- |
| `large_purchase` | is this charge big for this household, and for this category? |
| `duplicate` | did the same amount hit the same merchant twice in a few days? |
| `category_spike` | did one category cost far more this month than it usually does? |
| `missing_paycheck` | has recurring income stopped arriving? |
| `bill_increase` | is a regular bill now costing more? |
| `subscription_hike` | did a subscription quietly reprice? |

## The statistics, and why they are not standard deviations

Every threshold here is built on the **median and the median absolute
deviation**, not the mean and standard deviation. Household spending is not
normally distributed — it is a pile of small charges with a long right tail —
and one annual insurance payment inflates the standard deviation enough to hide
every genuine outlier beneath it for the rest of the year. The MAD is unmoved by
up to half the sample, so the rent payment stops being the reason rent is never
flagged.

`MODIFIED_Z_THRESHOLD` is 3.5, the conventional cutoff for the modified z-score
(Iglewicz & Hoaglin). It is stated as a constant rather than inlined because it
is a judgement call, and a reader should be able to argue with it.

Where a distribution genuinely has too few points to have a shape —
`MIN_SAMPLE_FOR_STATS` — no anomaly is reported at all. Three charges is not a
baseline, and "unusual compared to two other purchases" is a sentence that
should never reach a user.

## Nothing here writes

Detection is a read. The `anomaly_reviewed` flag on the ledger stays the
existing pane's business, and this module deliberately owns no write path, so a
briefing that runs it cannot mutate the row it is describing.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from sqlalchemy import func

from dough.services import analytics
from dough.services.analytics import as_date, lookback_window
from models import Transaction, db

#: Modified z-score above which a charge is an outlier. 3.5 is the conventional
#: Iglewicz & Hoaglin cutoff.
MODIFIED_Z_THRESHOLD = 3.5

#: The 0.6745 in the modified z-score: the MAD of a normal distribution times
#: this equals its standard deviation, which is what puts the score on a
#: familiar scale.
_MAD_TO_SIGMA = 0.6745

#: Below this many observations a distribution has no shape worth testing.
MIN_SAMPLE_FOR_STATS = 8

#: A charge must also clear this many dollars to be called large. Without it, a
#: household whose Dining charges are all $4 gets a $12 sandwich reported as a
#: statistical outlier, which is true and absurd.
MIN_LARGE_PURCHASE_USD = 100.0

#: When the MAD is zero the distribution is a point mass and no z-score exists.
#: A charge is then unusual if it is this many times the usual one.
#:
#: The case is not exotic, which is why it gets a constant rather than a `continue`:
#: a merchant charging the same amount every visit — twelve $20 shops, eight
#: identical rent payments — makes more than half the deviations exactly zero,
#: and the median of those is zero. `MIN_LARGE_PURCHASE_USD` still applies, so
#: this cannot fire on small money.
POINT_MASS_MULTIPLE = 3.0

#: Two identical charges to one merchant this many days apart look like a
#: double-post. Beyond it they look like somebody who buys coffee.
DUPLICATE_WINDOW_DAYS = 3

#: How far back the baselines are computed over.
BASELINE_MONTHS = 12

#: A recurring inflow is "missing" once this multiple of its usual gap passes.
#: 1.5 rather than 1.0: a paycheck landing on a Monday instead of a Friday is
#: not a missing paycheck, and an alert that fires every time payroll shifts by
#: a weekend is an alert people learn to ignore.
MISSING_INCOME_GAP_MULTIPLIER = 1.5

#: A recurring bill must rise by this share AND this many dollars.
BILL_INCREASE_PCT = 10.0
BILL_INCREASE_USD = 5.0

#: How much a charge may vary and still count as a fixed price. Used to tell a
#: subscription from a merchant that simply gets visited monthly -- see
#: `bill_increases`. Expressed as MAD over the median, so it is scale-free.
FIXED_PRICE_TOLERANCE = 0.15

SEVERITIES = ('critical', 'warning', 'info')


def detect(months=BASELINE_MONTHS, *, anchor=None, limit=25):
    """Every kind of anomaly, ranked, over the last `months`.

    The single entry point the copilot and the insight engine both call. Each
    detector is independent and failure-isolated by having no shared state, so
    a household with no income history still gets its duplicate charges.
    """
    window = lookback_window(months, anchor)
    rows = _rows(window)

    found = []
    found += large_purchases(rows)
    found += duplicates(rows)
    found += category_spikes(rows, window)
    found += missing_income(rows, window)
    found += bill_increases(rows)

    # Severity first, then size. A $2,000 duplicate outranks a $40 one, and both
    # outrank an informational category spike.
    order = {name: rank for rank, name in enumerate(SEVERITIES)}
    found.sort(key=lambda a: (order.get(a['severity'], 9), -abs(a.get('amount') or 0)))
    return found[:limit]


def _rows(window):
    """The window's transactions, once, as plain dicts.

    Read once and passed to every detector rather than each running its own
    query. Five detectors over several years of history is five full scans, and
    the whole point of `analytics` is that this package does not do that.
    """
    records = (analytics.base_query(window.start, window.end,
                                    include_transfers=False)
               .order_by(Transaction.date.asc(), Transaction.id.asc()).all())
    return [{'id': t.id, 'date': t.date, 'description': t.description,
             'amount': float(t.amount), 'category': t.category,
             'account': t.account_name} for t in records]


# ── Detectors ───────────────────────────────────────────────────────────────

def large_purchases(rows):
    """Charges far above what this household normally pays in that category.

    Compared within the category rather than against all spending. A $400
    grocery shop and a $400 flight are the same number and only one of them is
    surprising, and a single global threshold cannot tell them apart.
    """
    by_category = {}
    for row in rows:
        if row['amount'] < 0:
            by_category.setdefault(row['category'], []).append(row)

    found = []
    for category, charges in by_category.items():
        amounts = [abs(r['amount']) for r in charges]
        if len(amounts) < MIN_SAMPLE_FOR_STATS:
            continue
        centre = _median(amounts)
        spread = _mad(amounts)
        if not centre:
            continue

        for charge in charges:
            amount = abs(charge['amount'])
            if amount < MIN_LARGE_PURCHASE_USD:
                continue

            if spread:
                score = _modified_z(amount, centre, spread)
                unusual = score >= MODIFIED_Z_THRESHOLD
                basis = 'modified_z'
            else:
                # A point mass: every charge the same, so there is no spread to
                # score against and `_modified_z` would divide by zero. Any
                # large multiple of the usual amount is the anomaly. See
                # POINT_MASS_MULTIPLE -- this is the common case, not the edge.
                score = None
                unusual = amount >= centre * POINT_MASS_MULTIPLE
                basis = 'point_mass'

            if not unusual:
                continue
            found.append(_finding(
                'large_purchase', 'warning', charge,
                summary=f'{charge["description"]} is well above your usual {category} charge',
                amount=round(amount, 2),
                detail={'category': category,
                        'typical': round(centre, 2),
                        'times_typical': round(amount / centre, 1),
                        'score': round(score, 1) if score is not None else None,
                        'basis': basis,
                        'sample_size': len(amounts)}))
    return found


def duplicates(rows):
    """The same amount, to the same merchant, within a few days.

    A real double-post and a genuine repeat purchase are indistinguishable from
    the ledger alone, so this is reported as `info` and worded as a question.
    Calling it a duplicate outright would have the user chasing their bank over
    a coffee they bought twice.
    """
    seen = {}
    found = []
    for row in rows:
        if row['amount'] >= 0:
            continue
        key = (row['description'], round(abs(row['amount']), 2))
        previous = seen.get(key)
        if previous is not None:
            gap = (row['date'] - previous['date']).days
            if 0 <= gap <= DUPLICATE_WINDOW_DAYS:
                found.append(_finding(
                    'duplicate', 'info', row,
                    summary=f'Two charges of the same amount at {row["description"]}',
                    amount=round(abs(row['amount']), 2),
                    detail={'first_date': previous['date'].isoformat(),
                            'second_date': row['date'].isoformat(),
                            'days_apart': gap,
                            'first_id': previous['id']}))
        seen[key] = row
    return found


def category_spikes(rows, window):
    """A category costing far more this month than its own recent months.

    The baseline is the category's other months in the window, so a category
    that has always been expensive is not flagged for continuing to be.
    """
    monthly = {}
    for row in rows:
        if row['amount'] < 0:
            key = f'{row["date"]:%Y-%m}'
            bucket = monthly.setdefault(row['category'], {})
            bucket[key] = bucket.get(key, 0.0) + abs(row['amount'])

    latest = f'{window.end:%Y-%m}'
    found = []
    for category, months in monthly.items():
        current = months.get(latest)
        history = [total for key, total in months.items() if key != latest]
        if current is None or len(history) < MIN_SAMPLE_FOR_STATS // 2:
            continue
        centre = _median(history)
        spread = _mad(history)
        if not centre:
            continue
        score = _modified_z(current, centre, spread) if spread else None
        over = current - centre
        # Either a clean statistical outlier, or -- when the history is too
        # uniform to have a spread -- simply a large multiple of it.
        spiked = (score is not None and score >= MODIFIED_Z_THRESHOLD) or \
                 (score is None and current >= centre * 2)
        if not spiked or over < MIN_LARGE_PURCHASE_USD:
            continue
        found.append({
            'kind': 'category_spike', 'severity': 'warning',
            'summary': f'{category} cost more this month than it usually does',
            'amount': round(over, 2),
            'date': window.end.isoformat(), 'transaction_id': None,
            'description': category, 'category': category,
            'detail': {'this_month': round(current, 2),
                       'typical_month': round(centre, 2),
                       'over_by': round(over, 2),
                       'pct_over': analytics.pct_change(current, centre),
                       'months_compared': len(history),
                       'score': round(score, 1) if score is not None else None},
        })
    return found


def missing_income(rows, window):
    """A regular inflow that has not arrived when it should have.

    Only inflows seen at least three times are considered — twice is a
    coincidence, and reporting a missing paycheck to somebody who was paid a
    one-off bonus last quarter is worse than saying nothing.
    """
    inflows = {}
    for row in rows:
        if row['amount'] > 0:
            inflows.setdefault(row['description'], []).append(row)

    found = []
    for description, payments in inflows.items():
        if len(payments) < 3:
            continue
        dates = sorted(p['date'] for p in payments)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        usual_gap = _median(gaps)
        if usual_gap < 1:
            continue
        overdue = (window.end - dates[-1]).days
        if overdue <= usual_gap * MISSING_INCOME_GAP_MULTIPLIER:
            continue
        typical = _median([p['amount'] for p in payments])
        found.append({
            'kind': 'missing_paycheck', 'severity': 'critical',
            'summary': f'{description} has not arrived as usual',
            'amount': round(typical, 2),
            'date': dates[-1].isoformat(), 'transaction_id': None,
            'description': description, 'category': payments[-1]['category'],
            'detail': {'last_seen': dates[-1].isoformat(),
                       'days_since': overdue,
                       'usual_gap_days': round(usual_gap),
                       'typical_amount': round(typical, 2),
                       'times_seen': len(payments)},
        })
    return found


def bill_increases(rows):
    """A recurring charge that now costs more than it used to.

    The merchant's own earlier charges are the baseline, so this catches a rent
    rise and a streaming service repricing with the same test — and reports the
    annual cost of the change, which is the figure that makes somebody act.
    """
    by_merchant = {}
    for row in rows:
        if row['amount'] < 0:
            by_merchant.setdefault(row['description'], []).append(row)

    found = []
    for description, charges in by_merchant.items():
        if len(charges) < 4:
            continue
        ordered = sorted(charges, key=lambda r: r['date'])
        current = abs(ordered[-1]['amount'])
        baseline = _median([abs(r['amount']) for r in ordered[:-1]])
        if not baseline:
            continue
        delta = current - baseline
        pct = (delta / baseline) * 100.0
        if pct < BILL_INCREASE_PCT or delta < BILL_INCREASE_USD:
            continue
        # A subscription is a monthly cadence AND a repeated *identical* amount.
        # Cadence alone is not enough: somebody who eats at the same restaurant
        # once a month, spending more each time, matches the cadence perfectly
        # and is not subscribed to anything. Requiring the prior charges to be
        # near-identical is what separates "Netflix went from 15.99 to 22.99"
        # from "your restaurant bills have been climbing" — two findings that
        # deserve different words.
        gaps = [(b['date'] - a['date']).days
                for a, b in zip(ordered, ordered[1:])]
        priors = [abs(r['amount']) for r in ordered[:-1]]
        spread = _mad(priors) / baseline if baseline else 1.0
        monthly = 25 <= _median(gaps) <= 35 and spread <= FIXED_PRICE_TOLERANCE
        found.append(_finding(
            'subscription_hike' if monthly else 'bill_increase', 'warning',
            ordered[-1],
            summary=f'{description} costs more than it used to',
            amount=round(delta, 2),
            detail={'was': round(baseline, 2), 'now': round(current, 2),
                    'delta': round(delta, 2), 'pct': round(pct, 1),
                    'annual_impact': round(delta * (12 if monthly else 1), 2),
                    'charges_compared': len(ordered)}))
    return found


# ── Shared shapes and statistics ────────────────────────────────────────────

def _finding(kind, severity, row, *, summary, amount, detail):
    """One anomaly, in the shape every consumer reads.

    `transaction_id` is carried so a UI can link straight to the row and the
    copilot can be asked about it. That is what makes a finding checkable rather
    than an assertion the user has to take on faith.
    """
    return {'kind': kind, 'severity': severity, 'summary': summary,
            'amount': amount, 'date': as_date(row['date']).isoformat(),
            'transaction_id': row['id'], 'description': row['description'],
            'category': row['category'], 'detail': detail}


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values):
    """Median absolute deviation — the spread a single huge charge cannot move."""
    if not values:
        return 0.0
    centre = _median(values)
    return _median([abs(v - centre) for v in values])


def _modified_z(value, centre, spread):
    """(value - median) / MAD, on a standard-deviation-like scale.

    See the module docstring for why this and not a standard z-score.
    """
    if not spread:
        return 0.0
    return _MAD_TO_SIGMA * (value - centre) / spread


def open_flagged(limit=25):
    """The existing IsolationForest flags the ledger already carries.

    Kept here so a caller assembling "unusual activity" has one import rather
    than two, and so the statistical findings above and the model's flags can be
    presented together. This is a read of `Transaction.anomaly_score`; it does
    not recompute it.
    """
    rows = (Transaction.query
            .filter(Transaction.anomaly_score == -1.0,
                    Transaction.anomaly_reviewed.is_(False))
            .order_by(Transaction.date.desc())
            .limit(limit).all())
    return [{'kind': 'flagged', 'severity': 'info',
             'summary': f'{t.description} was flagged as unusual',
             'amount': round(abs(float(t.amount)), 2),
             'date': t.date.isoformat(), 'transaction_id': t.id,
             'description': t.description, 'category': t.category,
             'detail': {'source': 'isolation_forest'}}
            for t in rows]


def summary(months=BASELINE_MONTHS, *, anchor=None, findings=None):
    """Counts by kind and severity, for a card that has no room for the list.

    `findings` lets a caller that has already run `detect()` count the result it
    is holding rather than running the whole detector a second time — which is
    exactly what a page showing both the list and the counts would otherwise do.
    """
    found = detect(months, anchor=anchor) if findings is None else findings
    by_kind, by_severity = {}, {}
    for item in found:
        by_kind[item['kind']] = by_kind.get(item['kind'], 0) + 1
        by_severity[item['severity']] = by_severity.get(item['severity'], 0) + 1
    return {'total': len(found), 'by_kind': by_kind, 'by_severity': by_severity,
            'open_flagged': _open_flagged_count()}


def _open_flagged_count():
    return int(db.session.query(func.count(Transaction.id))
               .filter(Transaction.anomaly_score == -1.0,
                       Transaction.anomaly_reviewed.is_(False))
               .scalar() or 0)


__all__ = ['detect', 'summary', 'open_flagged', 'large_purchases', 'duplicates',
           'category_spikes', 'missing_income', 'bill_increases',
           'MODIFIED_Z_THRESHOLD', 'MIN_SAMPLE_FOR_STATS', 'SEVERITIES',
           'DUPLICATE_WINDOW_DAYS', 'BASELINE_MONTHS']
