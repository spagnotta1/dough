"""Feature 7 — retrieval for natural-language financial questions.

"How much did I spend on restaurants last quarter?" has an exact answer that
SQL can produce. The failure mode this module exists to prevent is the model
producing it instead — reading a list of transactions and adding them up in
prose, which looks identical to a real answer and is wrong often enough to
matter, silently, with no way for the reader to tell the difference.

So the flow is: **parse → query → answer over the result**. The model's job is
to choose the query and phrase the outcome, never to compute the total.

## What this module does and does not parse

It resolves the *structured* parts of a question that have a right answer:
a date range ("last quarter", "this year", "last month"), a category or merchant
match, an intent ("how much", "which merchant", "biggest"). It is a small, dull,
deterministic parser and it is meant to stay that way.

It does **not** attempt to understand the question in general. When it cannot
resolve something it says so — `matched: False` — rather than guessing, and the
caller falls back to asking the model to pick a query explicitly. A parser that
quietly resolves "restaurants" to the Rent category is worse than one that
admits defeat, because the answer it produces is confident and wrong.

## Why the category match is fuzzy but bounded

Categories are user-defined strings: one household's `Dining`, another's
`Restaurants`, a third's `Food & Drink`. A question says "restaurants". The
match therefore runs over the household's *actual* category names — never a
hardcoded taxonomy — and returns every category it matched so the answer can
say which ones were combined. `CATEGORY_SYNONYMS` seeds the common cases; the
substring pass catches the rest.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import func

from dough.services import analytics
from dough.services.analytics import (Window, custom_window, label_for,
                                      lookback_window, month_bounds,
                                      quarter_bounds, resolve_window,
                                      year_bounds)
from models import Transaction, db

#: Words a person uses for a category, mapped to the words a ledger uses. Only
#: a seed: the matcher also does a substring pass over the household's real
#: category names, which is what handles the ones nobody predicted.
CATEGORY_SYNONYMS = {
    'restaurant': ('dining', 'restaurants', 'food', 'eating out', 'takeout'),
    'restaurants': ('dining', 'restaurants', 'food'),
    'eating out': ('dining', 'restaurants'),
    'food': ('groceries', 'dining', 'food'),
    'groceries': ('groceries', 'supermarket'),
    'gas': ('gas', 'fuel', 'petrol'),
    'petrol': ('gas', 'fuel'),
    'transport': ('transport', 'transportation', 'transit', 'rideshare', 'gas'),
    'travel': ('travel', 'airfare', 'flights', 'hotel'),
    'subscriptions': ('streaming', 'subscriptions', 'software'),
    'streaming': ('streaming', 'subscriptions'),
    'utilities': ('utilities', 'electric', 'water', 'internet'),
    'rent': ('rent', 'mortgage', 'housing'),
    'housing': ('rent', 'mortgage', 'housing'),
    'health': ('healthcare', 'medical', 'health', 'pharmacy'),
    'shopping': ('shopping', 'retail', 'amazon'),
    'entertainment': ('entertainment', 'movies', 'games'),
    'insurance': ('insurance',),
}

#: How many rows a line-item answer returns before it stops being an answer.
DEFAULT_ROW_LIMIT = 25

#: The intents the parser recognises. Anything else is `unknown`, which is a
#: real answer -- see the module docstring.
INTENTS = ('total_spend', 'total_income', 'largest', 'top_merchant',
           'list_transactions', 'savings', 'count', 'unknown')

_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july',
           'august', 'september', 'october', 'november', 'december')


def search(question, *, today=None, limit=DEFAULT_ROW_LIMIT):
    """Answer a question from the ledger, or say why it could not be answered.

    Returns the parse *and* the figures, always. A caller that disagrees with
    the parse can re-run with explicit arguments, and a formatter can say "over
    March–June, across Dining and Restaurants" because both are in the result
    rather than only in the parser's head.
    """
    today = analytics.as_date(today or date.today())
    parsed = parse(question, today=today)
    window = parsed['window']

    categories = parsed['categories']
    merchant = parsed['merchant']

    query = analytics.base_query(window.start, window.end,
                                 include_transfers=parsed['include_transfers'])
    if categories:
        query = query.filter(func.lower(Transaction.category).in_(
            [c.lower() for c in categories]))
    if merchant:
        query = query.filter(Transaction.description.ilike(f'%{merchant}%'))

    spend, income, count = _totals(query)

    result = {
        'question': question,
        'matched': parsed['intent'] != 'unknown' or bool(categories or merchant),
        'intent': parsed['intent'],
        'window': window.as_dict(),
        'categories_matched': categories,
        'merchant_matched': merchant,
        'unmatched_terms': parsed['unmatched'],
        'total_spent': spend,
        'total_income': income,
        'transaction_count': count,
    }

    # Only the rows the intent actually needs. A "how much" question that also
    # ships 25 line items is 25 chances for the model to re-add them and report
    # a different number than the total directly above.
    if parsed['intent'] in ('largest', 'list_transactions', 'unknown'):
        result['transactions'] = _rows(query, limit=limit,
                                       biggest_first=parsed['intent'] == 'largest')
    if parsed['intent'] in ('top_merchant', 'unknown'):
        result['merchants'] = _merchants(query, limit=10)
    if parsed['intent'] == 'savings':
        result['net'] = round(income - spend, 2)
        result['savings_rate'] = (round((income - spend) / income * 100.0, 1)
                                  if income > 0 else None)
    return result


def parse(question, *, today=None):
    """The structured reading of a question. Deterministic, and small on purpose."""
    today = analytics.as_date(today or date.today())
    text = (question or '').lower().strip()

    window, window_phrase = _window_from(text, today)
    categories, category_phrase = _categories_from(text)
    merchant = _merchant_from(text, categories)

    return {
        'intent': _intent_from(text),
        'window': window,
        'window_phrase': window_phrase,
        'categories': categories,
        'category_phrase': category_phrase,
        'merchant': merchant,
        'include_transfers': 'transfer' in text,
        'unmatched': _unmatched(text, window_phrase, category_phrase, merchant),
    }


# ── Intent ──────────────────────────────────────────────────────────────────

def _intent_from(text):
    if re.search(r'\bsave[d]?\b|\bsavings? rate\b|\bput away\b', text):
        return 'savings'
    if re.search(r'\bwhich merchant\b|\bwho\b.*\bcharged\b|\bmost at\b'
                 r'|\bmerchant\b', text):
        return 'top_merchant'
    if re.search(r'\bbiggest\b|\blargest\b|\bmost expensive\b|\btop\b', text):
        return 'largest'
    if re.search(r'\bhow many\b|\bhow often\b|\bnumber of\b|\bcount\b', text):
        return 'count'
    if re.search(r'\bhow much\b.*\b(made|earn|earned|income|paid me)\b'
                 r'|\bincome\b', text):
        return 'total_income'
    if re.search(r'\bhow much\b|\btotal\b|\bspend\b|\bspent\b', text):
        return 'total_spend'
    if re.search(r'\bshow\b|\blist\b|\bwhat did i buy\b|\bpurchases?\b'
                 r'|\btransactions?\b', text):
        return 'list_transactions'
    return 'unknown'


# ── Window ──────────────────────────────────────────────────────────────────

def _window_from(text, today):
    """The date range a question names, defaulting to the last twelve months.

    The default is wide rather than narrow, and deliberately: a question with no
    period usually means "ever" or "recently", and answering "$0" because the
    parser assumed *this month* is the most annoying possible failure. A wide
    window over-reports at worst, and the window is returned so the answer can
    say which one it used.
    """
    if re.search(r'\blast month\b|\bprevious month\b', text):
        return resolve_window('month',
                              month_bounds(today)[0] - _one_day()), 'last month'
    if re.search(r'\bthis month\b|\bcurrent month\b', text):
        return resolve_window('month', today), 'this month'
    if re.search(r'\blast quarter\b', text):
        return resolve_window('quarter',
                              quarter_bounds(today)[0] - _one_day()), 'last quarter'
    if re.search(r'\bthis quarter\b', text):
        return resolve_window('quarter', today), 'this quarter'
    if re.search(r'\blast year\b', text):
        return resolve_window('year',
                              year_bounds(today)[0] - _one_day()), 'last year'
    if re.search(r'\bthis year\b|\byear to date\b|\bytd\b', text):
        start = year_bounds(today)[0]
        return Window(start, today, label_for(start, today), 'custom'), 'this year'

    rolling = re.search(r'\b(?:last|past)\s+(\d{1,2})\s+months?\b', text)
    if rolling:
        months = max(1, min(int(rolling.group(1)), 120))
        return lookback_window(months, today), rolling.group(0)

    days = re.search(r'\b(?:last|past)\s+(\d{1,3})\s+days?\b', text)
    if days:
        span = max(1, min(int(days.group(1)), 3650))
        start = today - _one_day() * (span - 1)
        return custom_window(start, today), days.group(0)

    named = re.search(r'\b(' + '|'.join(_MONTHS) + r')\b(?:\s+(\d{4}))?', text)
    if named:
        month = _MONTHS.index(named.group(1)) + 1
        year = int(named.group(2)) if named.group(2) else today.year
        # A month later in the year than today, with no year given, is last
        # year's -- "what did I spend in December" asked in March.
        if not named.group(2) and month > today.month:
            year -= 1
        return resolve_window('month', date(year, month, 1)), named.group(0)

    year_only = re.search(r'\b(20\d{2})\b', text)
    if year_only:
        return resolve_window('year', date(int(year_only.group(1)), 1, 1)), \
            year_only.group(1)

    return lookback_window(12, today), None


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


# ── Category and merchant ───────────────────────────────────────────────────

def _categories_from(text):
    """Which of the household's real categories a question refers to.

    Matched against the categories that actually exist rather than a fixed
    taxonomy — see the module docstring. Returns every match so the answer can
    name what it combined.
    """
    existing = _known_categories()
    if not existing:
        return [], None

    lookup = {name.lower(): name for name in existing}
    matched, phrase = set(), None

    for word, synonyms in CATEGORY_SYNONYMS.items():
        if not re.search(rf'\b{re.escape(word)}\b', text):
            continue
        phrase = phrase or word
        for synonym in synonyms:
            for lowered, original in lookup.items():
                if synonym in lowered or lowered in synonym:
                    matched.add(original)

    # Direct hits on the household's own names, which is what catches a
    # category nobody thought to add a synonym for.
    for lowered, original in lookup.items():
        if len(lowered) > 2 and re.search(rf'\b{re.escape(lowered)}\b', text):
            matched.add(original)
            phrase = phrase or lowered

    return sorted(matched), phrase


def _known_categories():
    return [name for (name,) in
            db.session.query(Transaction.category).distinct().all() if name]


def _merchant_from(text, categories):
    """A quoted or capitalised merchant name, when the question names one.

    Only fires on an explicit signal — a quoted string, or "at X" / "from X".
    Guessing a merchant out of an arbitrary noun is how "show my Amazon
    purchases" and "show my monthly purchases" become the same query.
    """
    quoted = re.search(r'["“\']([^"”\']{2,40})["”\']', text)
    if quoted:
        return quoted.group(1).strip()

    at = re.search(r'\b(?:at|from|with)\s+([a-z0-9][a-z0-9 &.\'-]{1,30}?)'
                   r'(?=\s+(?:last|this|in|on|during|over|for|between)\b|[?.,]|$)',
                   text)
    if at:
        candidate = at.group(1).strip()
        if candidate and candidate not in {c.lower() for c in categories}:
            return candidate

    # A known merchant named bare -- "my Amazon purchases". Checked against the
    # ledger so only real payees match.
    for name in _known_merchant_words():
        if re.search(rf'\b{re.escape(name)}\b', text):
            return name
    return None


def _known_merchant_words():
    """Distinct first words of descriptions, longest first.

    First words because bank descriptions are `AMAZON MKTPL*2H4TG` — the payee
    is the head of the string and the rest is noise. Longest first so `whole
    foods` wins over `whole`.
    """
    rows = db.session.query(Transaction.description).distinct().limit(500).all()
    words = set()
    for (description,) in rows:
        head = re.split(r'[^a-zA-Z&]+', (description or '').strip())
        if head and len(head[0]) > 3:
            words.add(head[0].lower())
    return sorted(words, key=len, reverse=True)


def _unmatched(text, window_phrase, category_phrase, merchant):
    """Meaningful words the parser did not account for.

    Returned so a caller can tell "I answered the whole question" from "I
    answered the part I understood", which is the difference between a good
    answer and a confident non-answer.
    """
    consumed = ' '.join(filter(None, [window_phrase, category_phrase, merchant]))
    stop = {'how', 'much', 'did', 'i', 'spend', 'on', 'in', 'the', 'a', 'my',
            'me', 'what', 'was', 'is', 'show', 'list', 'of', 'for', 'at',
            'from', 'to', 'and', 'total', 'me', 'do', 'have', 'which', 'that',
            'this', 'last', 'past', 'over', 'during', 'between', 'biggest',
            'largest', 'most', 'expensive', 'top', 'many', 'money', 'spent',
            'purchases', 'purchase', 'transactions', 'transaction', 'merchant',
            'charged', 'save', 'saved', 'savings', 'rate', 'income', 'earned',
            'made', 'buy', 'bought', 'all', 'me', 'us', 'we', 'per', 'each'}
    words = [w for w in re.findall(r'[a-z]{3,}', text)
             if w not in stop and w not in consumed and w not in _MONTHS]
    return sorted(set(words))


# ── Execution ───────────────────────────────────────────────────────────────

def _totals(query):
    """Spend, income and count for a filtered query, in one aggregate."""
    from sqlalchemy import Float, case, cast

    amount = cast(Transaction.amount, Float)
    row = (query.with_entities(
        func.sum(case((Transaction.amount < 0, -amount), else_=0.0)),
        func.sum(case((Transaction.amount > 0, amount), else_=0.0)),
        func.count(Transaction.id)).one())
    return (round(float(row[0] or 0.0), 2), round(float(row[1] or 0.0), 2),
            int(row[2] or 0))


def _rows(query, *, limit, biggest_first):
    order = (Transaction.amount.asc() if biggest_first
             else Transaction.date.desc())
    rows = query.order_by(order, Transaction.id.desc()).limit(limit).all()
    return [{'id': t.id, 'date': t.date.isoformat(),
             'description': t.description,
             'amount': round(float(t.amount), 2),
             'category': t.category, 'account': t.account_name}
            for t in rows]


def _merchants(query, *, limit):
    from sqlalchemy import Float, cast

    rows = (query.with_entities(
        Transaction.description,
        func.sum(-cast(Transaction.amount, Float)).label('total'),
        func.count(Transaction.id).label('n'))
        .filter(Transaction.amount < 0)
        .group_by(Transaction.description)
        .order_by(func.sum(Transaction.amount).asc())
        .limit(limit).all())
    return [{'description': r.description, 'total': round(float(r.total or 0), 2),
             'transactions': int(r.n or 0)} for r in rows]


__all__ = ['search', 'parse', 'CATEGORY_SYNONYMS', 'INTENTS',
           'DEFAULT_ROW_LIMIT']
