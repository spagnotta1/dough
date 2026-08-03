"""Transfer detection: money that moved between the household's own accounts.

## The bug this exists to fix

Every surface that reports income already knows to exclude the `Transfer`
category — `analytics._is_transfer()`, `core.dashboard`'s `is_transfer(t)`,
`finance_context`'s prose rule to the model. All of them read
`Transaction.category`, and **nothing was ever writing it.**

Categories come from `CategoryRule` rows, a household starts with none
([Phase 11A.2], see `rules.DEFAULT_RULES`), and the AI suggester was explicitly
told to *skip* transfers. So $2,000 moved from checking to savings landed as
`-2000 Uncategorized` in one account and `+2000 Uncategorized` in the other,
the `+2000` was counted as income, and a household that shuffled money between
its own accounts read as earning far more than it did. The exclusion was there;
the label it excluded on never arrived.

## Why pairing rather than keywords

A keyword rule cannot express this. `TRANSFER` in a description is a hint, not
a fact — "TRANSFER TO SAVINGS" is one, "WIRE TRANSFER TO LANDLORD" is rent —
and the descriptions banks write for the two halves of the same movement often
share no words at all (`ONLINE TRANSFER 8842` against `DEPOSIT FROM CHECKING`).

What identifies a transfer is not language, it is **arithmetic**: an amount
leaving one account the household owns and the same amount arriving in another
within a few days. That is checkable, and when it checks out the two rows net
to zero by construction — which is exactly what "net out transfers" means. Both
halves get the `Transfer` category, and every existing exclusion then does the
right thing without a single one of them changing.

Language is still used, but only to *widen* the window (see `_MAX_GAP_DAYS`):
a pair that also says "transfer" is allowed to settle over a longer weekend
than a pair identified by arithmetic alone.

## Where this runs

After categorization, never instead of it — `blueprints/rules._recategorize`,
`rules.ai_apply`, the CSV importer and the sync repository all call
`net_out_transfers()` as a second pass. The order matters and the layering is
deliberate:

    category = rules(description)      # pure function of description + rules
    category = 'Transfer' if paired    # a fact about the row, not its text

Re-deriving from rules resets the whole ledger first, so this pass is
idempotent: run it twice and the second run changes nothing, and a pair that
stops pairing (a row was deleted) loses the label on the next re-derivation
rather than being stuck with it.

Pairing wins over a rule that claimed one of the halves. That is the intended
precedence — a rule matched a description, this matched the actual movement of
money, and the movement is the stronger evidence.

## Household scoping

Every query goes through `Transaction.query`, which is `TenantScopedQuery`, so
two households cannot pair against each other's rows. No function here takes a
household argument, so there is none to pass wrongly.

Allowed:   models, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

import re

from models import Transaction, db

#: The label both halves of a matched pair receive. Lowercased, this is one of
#: `analytics.TRANSFER_CATEGORIES`, which is what makes every income and
#: spending total in the application skip these rows.
TRANSFER_CATEGORY = 'Transfer'

#: Bank wording that suggests — never proves — an internal movement. Used only
#: to widen the date window a pair may span, so a false hit costs nothing on
#: its own: something still has to match it in amount, in another account.
_TRANSFER_LANGUAGE = re.compile(
    r'\b(transfer|xfer|trnsfr|to\s+savings|from\s+savings|to\s+checking|'
    r'from\s+checking|online\s+banking|internal|acct\s*to\s*acct)\b',
    re.IGNORECASE)

#: How many days apart the two halves may fall. Banks post the debit and the
#: credit on different days, and an ACH between two institutions routinely
#: takes a business day or three; a same-day-only rule would miss most real
#: transfers. The wider window is only granted when a description also says so
#: — arithmetic alone gets the tight one, because the chance of an unrelated
#: equal-and-opposite coincidence grows with every extra day allowed.
_MAX_GAP_DAYS = 3
_MAX_GAP_DAYS_WORDED = 7

#: Amounts below this are not considered. Two accounts producing an unrelated
#: equal-and-opposite $3.00 within the window is plausible in a way that an
#: unrelated equal-and-opposite $2,000 is not, and mislabelling a small
#: purchase as a transfer removes it from spending silently.
_MIN_AMOUNT = 5.00


def find_transfer_pairs(transactions=None):
    """Matched `(outgoing, incoming)` pairs among this household's rows.

    Separate from `net_out_transfers` so the matching can be tested, explained
    and previewed without writing anything.

    A pair requires all four of:

    - equal absolute amounts, to the cent;
    - opposite signs;
    - **different accounts** — this is what makes it a transfer rather than a
      refund, and it is why a merchant crediting back the exact amount it
      charged does not match;
    - dates within the window `_MAX_GAP_DAYS`, or `_MAX_GAP_DAYS_WORDED` when
      either description carries transfer language.

    Matching is greedy over the closest date first, and each row is used at
    most once. That matters for a household making the same $500 sweep every
    month: without the used-set, twelve debits would each match all twelve
    credits, and without the closest-first ordering January's debit could pair
    with March's credit and leave February's unmatched in the middle.
    """
    rows = transactions if transactions is not None else Transaction.query.all()

    # Bucketed by absolute amount, so the scan is linear in the ledger rather
    # than quadratic: only rows that could possibly pair are ever compared.
    buckets = {}
    for row in rows:
        amount = round(float(row.amount), 2)
        if abs(amount) < _MIN_AMOUNT:
            continue
        buckets.setdefault(abs(amount), []).append(row)

    pairs = []
    for candidates in buckets.values():
        outgoing = sorted((r for r in candidates if float(r.amount) < 0),
                          key=lambda r: (r.date, r.id))
        incoming = sorted((r for r in candidates if float(r.amount) > 0),
                          key=lambda r: (r.date, r.id))
        if not outgoing or not incoming:
            continue

        used = set()
        for debit in outgoing:
            best = None
            for credit in incoming:
                if credit.id in used or not _different_accounts(debit, credit):
                    continue
                gap = abs((credit.date - debit.date).days)
                if gap > _gap_allowed(debit, credit):
                    continue
                # Ties break on id, so a re-run pairs the same rows.
                if best is None or (gap, credit.id) < (best[0], best[1].id):
                    best = (gap, credit)
            if best is not None:
                used.add(best[1].id)
                pairs.append((debit, best[1]))

    return pairs


def net_out_transfers(commit=True):
    """Label both halves of every matched pair `Transfer`. Returns rows changed.

    The write half of `find_transfer_pairs`. Only ever *adds* the label: rows
    that stop pairing are cleaned up by the rules re-derivation that runs
    before this, not by a sweep here, because this function cannot tell a
    transfer it labelled last week from one the user labelled by hand.

    `commit=False` is for callers already inside a transaction — the CSV
    importer commits per row and would rather this joined the last one.
    """
    changed = 0
    for debit, credit in find_transfer_pairs():
        for row in (debit, credit):
            if row.category != TRANSFER_CATEGORY:
                row.category = TRANSFER_CATEGORY
                changed += 1
    if changed and commit:
        db.session.commit()
    return changed


def looks_like_transfer(description):
    """Whether a description carries transfer language.

    Exposed for the AI suggester, which uses it to tell the model which
    descriptions the arithmetic has *already* settled so it does not spend a
    rule on them.
    """
    return bool(_TRANSFER_LANGUAGE.search(description or ''))


# ── internals ───────────────────────────────────────────────────────────────

def _different_accounts(left, right):
    """Whether two rows belong to different accounts.

    `account_id` is authoritative when both rows have one — two synced accounts
    can carry the same display name from different institutions. CSV imports
    have no `account_id` at all, so `account_name` is the fallback rather than
    the primary key it looks like.
    """
    if left.account_id is not None and right.account_id is not None:
        return left.account_id != right.account_id
    return (left.account_name or '') != (right.account_name or '')


def _gap_allowed(debit, credit):
    if (looks_like_transfer(debit.description)
            or looks_like_transfer(credit.description)):
        return _MAX_GAP_DAYS_WORDED
    return _MAX_GAP_DAYS


__all__ = ['TRANSFER_CATEGORY', 'find_transfer_pairs', 'net_out_transfers',
           'looks_like_transfer']
