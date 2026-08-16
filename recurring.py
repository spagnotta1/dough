"""Recurring-payment detection.

Splits recurring outflows into two kinds:

* **Bills & payments** — debt service and obligatory monthly payments
  (student loans, auto loans, credit card bills, insurance, rent, utilities).
  Identified by category: the user's own categorization rules are treated as
  the primary evidence, so no payment cadence is required — amounts and
  timing may vary (a twice-monthly card payment, an irregular bursar
  payment). Two or more recent payments to the same payee qualify.

* **Subscriptions** — genuine subscription services. Anything the user has
  categorized as ``Subscriptions`` qualifies on a monthly cadence. For every
  other category (where a monthly-ish restaurant habit would otherwise slip
  through) the bar is much higher: the charge must repeat for the *exact same
  amount* at a tight monthly interval at least three times — the signature of
  an automated billing system, not a human ordering "the usual".

Which tier a transaction lands in is decided by the *words* in the household's
own category name, not by an exact match against a fixed list — categories are
free text, and 'Education' or 'Insurance' name the same obligation as
'Student Loan' or 'Insurance Payment'.

Payees get renamed by their import source over time, so groups are merged
across wordings and each group is labelled with the wording on its most recent
charge — the name the next charge will arrive under.

Groups whose most recent hit is older than ``RECENCY_DAYS`` (measured against
the newest transaction in the data, not the wall clock) are treated as
cancelled/paid off and excluded. Groups the user has manually dismissed
(their normalized description matches a ``dismissed_keys`` entry) are also
excluded — human-in-the-loop wins over any heuristic.
"""

import re
from datetime import timedelta

# Categories are free text the household names itself — there is no fixed list
# to match against. Someone who files their student-loan servicers under
# 'Education' and their auto policy under 'Insurance' means exactly what the
# person who typed 'Student Loan' and 'Insurance Payment' meant, and an
# exact-string set silently disagreed: their loan payments fell past the bill
# tier into the strict tier and surfaced as *subscriptions*. So classify on the
# words in the category name instead.
#
# Whole words, not substrings: 'rent' as a substring also matches 'Parenting'.
BILL_CATEGORY_WORDS = {
    'loan', 'loans', 'mortgage', 'mortgages', 'rent', 'lease',
    'insurance', 'premium', 'premiums', 'utility', 'utilities',
    'bill', 'bills', 'debt', 'education', 'tuition',
    'childcare', 'daycare', 'hoa',
}
# Phrases whose individual words are too generic to list on their own.
BILL_CATEGORY_PHRASES = ('credit card', 'car payment', 'student debt')

SUBSCRIPTION_CATEGORY_WORDS = {'subscription', 'subscriptions',
                               'membership', 'memberships', 'streaming'}

# Money movement, not spending — never a bill or subscription.
EXCLUDED_CATEGORY_WORDS = {'transfer', 'transfers', 'income',
                           'investment', 'investments'}

RECENCY_DAYS = 183  # ~6 months

_AVG_MONTH_DAYS = 30.44


def normalize_description(description):
    """Strip digits and collapse whitespace so 'INVOICE 1234' == 'INVOICE 5678'."""
    return re.sub(r'\s+', ' ', re.sub(r'\d+', '', description or '')).strip().lower()


def _category_words(category):
    return set(re.findall(r'[a-z]+', (category or '').lower()))


def is_bill_category(category):
    """Whether a household's category name describes an obligatory payment."""
    name = (category or '').lower()
    return (any(phrase in name for phrase in BILL_CATEGORY_PHRASES)
            or bool(BILL_CATEGORY_WORDS & _category_words(category)))


def is_subscription_category(category):
    return bool(SUBSCRIPTION_CATEGORY_WORDS & _category_words(category))


def is_excluded_category(category):
    return bool(EXCLUDED_CATEGORY_WORDS & _category_words(category))


def _related(a, b):
    """Two normalized descriptions refer to the same payee if one contains
    the other (import sources vary the wording, not the payee name)."""
    return a in b or b in a


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def _merge_related(groups):
    """Merge groups whose normalized descriptions substring-match.

    Import sources describe the same payee differently over time (a CSV
    export says 'Withdrawal from FIRSTMARK PAYMENTS', the live sync just
    'FIRSTMARK'), which would otherwise show one loan as two rows.
    Returns (keys, txns) pairs.
    """
    clusters = []  # each: [set_of_keys, txns]
    for key, txns in sorted(groups.items(), key=lambda kv: len(kv[0])):
        for cluster in clusters:
            if any(_related(key, other) for other in cluster[0]):
                cluster[0].add(key)
                cluster[1].extend(txns)
                break
        else:
            clusters.append([{key}, list(txns)])
    return clusters


def _latest(txns):
    """The transaction whose wording should name the group.

    A merged group spans more than one wording of the same payee, and the
    current one is whatever the newest charge is called — a row must not stay
    labelled with an import format the bank stopped sending. Ties on the same
    date go to the wording used most often that day rather than to whichever
    order the merge happened to append in.
    """
    newest = max(t['date'] for t in txns)
    same_day = [t for t in txns if t['date'] == newest]
    counts = {}
    for t in same_day:
        counts[t['description']] = counts.get(t['description'], 0) + 1
    winner = max(counts, key=lambda d: (counts[d], d))
    return next(t for t in same_day if t['description'] == winner)


def _summarize(keys, txns, monthly_amount):
    txns = sorted(txns, key=lambda t: t['date'])
    gaps = [(txns[i + 1]['date'] - txns[i]['date']).days for i in range(len(txns) - 1)]
    gap = _median(gaps)
    latest = _latest(txns)
    return {
        'description': latest['description'],
        'desc_keys': sorted(keys),
        'category': latest['category'],
        'account_name': latest.get('account_name'),
        'monthly_amount': round(monthly_amount, 2),
        'occurrences': len(txns),
        'median_gap_days': round(gap, 1),
        'first_seen': txns[0]['date'].strftime('%Y-%m-%d'),
        'last_seen': latest['date'].strftime('%Y-%m-%d'),
        'next_expected': (latest['date'] + timedelta(days=round(gap))).strftime('%Y-%m-%d'),
    }


def detect_recurring(txns, dismissed_keys=()):
    """Detect recurring outflows in a list of transaction dicts.

    Each dict needs ``date`` (date or datetime), ``description``, ``amount``
    (float, negative = expense), ``category``, and optionally
    ``account_name``. ``dismissed_keys`` are normalized descriptions the user
    has manually marked as not recurring; any matching group is suppressed.
    Returns ``{'bills': [...], 'subscriptions': [...]}`` sorted by monthly
    amount descending.
    """
    dismissed_keys = [k for k in dismissed_keys if k]

    expenses = [t for t in txns
                if t['amount'] < 0 and not is_excluded_category(t['category'])]
    if not expenses:
        return {'bills': [], 'subscriptions': []}

    max_date = max(t['date'] for t in txns)
    cutoff = max_date - timedelta(days=RECENCY_DAYS)

    def is_dismissed(keys):
        return any(_related(key, d) for key in keys for d in dismissed_keys)

    bill_groups, sub_groups, other_groups = {}, {}, {}
    for t in expenses:
        key = normalize_description(t['description'])
        if not key:
            continue
        if is_bill_category(t['category']):
            bill_groups.setdefault(key, []).append(t)
        elif is_subscription_category(t['category']):
            sub_groups.setdefault(key, []).append(t)
        else:
            # Strict tier: only identical charge amounts can group together,
            # so bucket by amount first and merge renamed payees *within* a
            # bucket. Keying groups on (description, amount) directly — as this
            # did before — skipped the merge entirely, so a payee whose import
            # wording changed ('Withdrawal from FIRSTMARK PAYMENTS' in 2025,
            # 'FIRSTMARK' in 2026) listed twice: one stale row frozen at the
            # old name and one short row under the new one.
            other_groups.setdefault(round(t['amount'], 2), {}) \
                        .setdefault(key, []).append(t)

    bills, subscriptions = [], []

    for keys, grp in _merge_related(bill_groups):
        if len(grp) < 2 or is_dismissed(keys):
            continue
        grp.sort(key=lambda t: t['date'])
        if grp[-1]['date'] < cutoff:
            continue
        # The user's category rules already say this is a bill — no cadence
        # requirement, since debt payments can be irregular (extra principal,
        # twice-monthly card payments, semester bursar bills).
        # Show the typical actual payment as it appears in transactions;
        # the median resists one-off extra payments.
        summary = _summarize(keys, grp, _median([t['amount'] for t in grp]))
        # Annual cost follows the real payment frequency, not a flat ×12.
        summary['annual_cost'] = round(
            abs(summary['monthly_amount']) * 365.25 / max(summary['median_gap_days'], 1))
        bills.append(summary)

    for keys, grp in _merge_related(sub_groups):
        if len(grp) < 2 or is_dismissed(keys):
            continue
        grp.sort(key=lambda t: t['date'])
        if grp[-1]['date'] < cutoff:
            continue
        gaps = [(grp[i + 1]['date'] - grp[i]['date']).days for i in range(len(grp) - 1)]
        if not 20 <= _median(gaps) <= 40:
            continue
        # The current price is what renews, so charge = most recent amount.
        subscriptions.append(_summarize(keys, grp, grp[-1]['amount']))

    for amount, groups in other_groups.items():
        for keys, grp in _merge_related(groups):
            if len(grp) < 3 or is_dismissed(keys):
                continue
            grp.sort(key=lambda t: t['date'])
            if grp[-1]['date'] < cutoff:
                continue
            gaps = [(grp[i + 1]['date'] - grp[i]['date']).days for i in range(len(grp) - 1)]
            if not 25 <= _median(gaps) <= 35:
                continue
            if not all(18 <= g <= 45 for g in gaps):
                continue
            subscriptions.append(_summarize(keys, grp, amount))

    bills.sort(key=lambda g: g['monthly_amount'])
    subscriptions.sort(key=lambda g: g['monthly_amount'])
    return {'bills': bills, 'subscriptions': subscriptions}
