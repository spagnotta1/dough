"""Feature 9 — can this household afford a thing, and what does that depend on?

The brief's examples are a vacation, a car payment, a bigger retirement
contribution, a move, a house. They divide into exactly two shapes, and the
arithmetic differs completely:

- **A one-off cost** comes out of savings. What matters is what it does to the
  cash buffer and how long it would take to rebuild.
- **A recurring commitment** comes out of monthly surplus. What matters is
  whether the surplus survives it, every month, including the bad ones.

A scenario can be both — a car is a deposit *and* a payment — so `assess` takes
both and reports on whichever were given.

## What this module refuses to do

**It never answers "yes".** `verdict` is a band with a reason attached, and the
bands are deliberately worded as descriptions of the arithmetic rather than as
permission: `comfortable`, `tight`, `not_without_changes`, `cannot_assess`. A
service that told somebody they could afford a house would be making a promise
about a future it cannot see, on data that covers a few months of their past.

**It never hides the assumptions.** Every figure the verdict rests on is
returned in `assumptions`, in the form "this is what I used and here is where it
came from", because the honest answer to most affordability questions is "it
depends on whether next year looks like last year", and the user is the only one
who knows that.

**It reports its own reliability.** A household whose monthly surplus swings by
more than the commitment being considered has not got a stable surplus to commit,
and `confidence` says so. This is the difference between "you have $600 a month
spare" and "you averaged $600 a month spare across months that ranged from
-$400 to +$1,900".

## Medians, not means

Monthly income and spending are summarised with the **median** month, not the
mean. One tax refund or one annual insurance premium moves a mean enough to
change the verdict, and the resulting advice would be wrong in the direction
that encourages spending. The mean is returned alongside, because the gap
between the two is itself informative and is surfaced as an uncertainty.

Allowed:   models, `dashboard_intel`, sibling services, SQLAlchemy, stdlib
Must not:  app, render_template/url_for/redirect/flash/jsonify, anthropic,
           blueprints or route decorators, `request`, `session`
"""

from __future__ import annotations

from dough.services import analytics, health
from dough.services.analytics import lookback_window
from dough.services.networth import compute_net_worth, monthly_outgo

#: How much history the assessment reads. Six months is enough for a median to
#: mean something and recent enough to describe the household's present life.
DEFAULT_MONTHS = 6

#: Cash buffer, in months of normal outgo, that a one-off purchase should leave
#: behind. Three is the conservative end of the usual advice; `GOOD_RUNWAY_
#: MONTHS` in `dashboard_intel` is six and is the target, not the floor.
MIN_RUNWAY_AFTER_MONTHS = 3.0

#: A recurring commitment above this share of the median surplus is "tight"
#: even when it technically fits — committing every spare dollar leaves nothing
#: for the month something goes wrong.
COMFORTABLE_SURPLUS_SHARE = 0.5

#: And above this share it does not fit in any meaningful sense.
MAX_SURPLUS_SHARE = 0.9

VERDICTS = ('comfortable', 'tight', 'not_without_changes', 'cannot_assess')


def assess(*, one_off=0.0, monthly=0.0, months=DEFAULT_MONTHS, anchor=None,
           label=None):
    """Assess one scenario against this household's actual finances.

    `one_off` is a single cost (a holiday, a deposit). `monthly` is an ongoing
    commitment (a payment, a rent increase, a larger contribution). Either may
    be zero; both together describe a car or a house.

    Returns structured data only. `dough/ai/copilot.py` turns it into prose, and
    keeping the judgement here means the model cannot arrive at a different
    verdict from the numbers underneath it.
    """
    one_off = max(0.0, float(one_off or 0.0))
    monthly = max(0.0, float(monthly or 0.0))

    baseline = capacity(months, anchor=anchor)
    result = {
        'scenario': {'label': label, 'one_off': round(one_off, 2),
                     'monthly': round(monthly, 2)},
        'capacity': baseline,
        'one_off_impact': None,
        'monthly_impact': None,
        'assumptions': _assumptions(baseline, months),
        'uncertainties': _uncertainties(baseline, monthly),
    }

    if not baseline['can_assess']:
        result['verdict'] = 'cannot_assess'
        result['reason'] = baseline['why_not']
        return result

    if one_off:
        result['one_off_impact'] = _one_off_impact(one_off, baseline)
    if monthly:
        result['monthly_impact'] = _monthly_impact(monthly, baseline)

    result['verdict'], result['reason'] = _verdict(result)
    result['confidence'] = baseline['confidence']
    return result


def capacity(months=DEFAULT_MONTHS, *, anchor=None):
    """What this household can actually commit, measured rather than assumed.

    The shared baseline every scenario is judged against. Returns
    `can_assess: False` with a reason rather than guessing when there is not
    enough history — an affordability verdict from two months of data is a
    confident statement about somebody's life built on almost nothing.
    """
    # `months + 1` because the last month in the window is the current one and
    # is about to be dropped. Asking for six months of history should yield six
    # *complete* months, not five -- and with the off-by-one, `_confidence`
    # could never reach 'high', because its six-month threshold was unreachable
    # by construction.
    window = lookback_window(months + 1, anchor)
    series = analytics.monthly_series(window.start, window.end)

    # The current month is partial, and including it drags the median down by
    # however many days are left in it -- which would systematically understate
    # capacity, and understating it is still being wrong.
    keys = list(series)
    complete = keys[:-1] if len(keys) > 1 else keys

    # Months before the household's first record are not zero-income months,
    # they are months this household did not exist for. Counting them would let
    # somebody with two months of history look like six mostly-empty ones --
    # which passes the "enough history" check and then reports a median income
    # of zero. The same distinction `trends._trim_leading_absence` draws, for
    # the same reason.
    first = next((i for i, k in enumerate(complete)
                  if series[k]['income'] or series[k]['spending']), None)
    complete = complete[first:] if first is not None else []

    incomes = [series[k]['income'] for k in complete]
    spends = [series[k]['spending'] for k in complete]

    net_worth = compute_net_worth()
    outgo = monthly_outgo(months)
    stability = health.cash_flow_stability(months, anchor=anchor)

    if len(complete) < 3 or not any(incomes):
        return {
            'can_assess': False,
            'why_not': ('I need at least three complete months with income '
                        f'recorded; I have {len(complete)} month(s)'),
            'months_measured': len(complete),
            'cash': net_worth['cash'],
            'confidence': 'none',
        }

    median_income = _median(incomes)
    median_spending = _median(spends)
    surplus = median_income - median_spending
    flows = [i - s for i, s in zip(incomes, spends)]

    return {
        'can_assess': True,
        'why_not': None,
        'months_measured': len(complete),
        'median_monthly_income': round(median_income, 2),
        'median_monthly_spending': round(median_spending, 2),
        'median_monthly_surplus': round(surplus, 2),
        'mean_monthly_surplus': round(sum(flows) / len(flows), 2),
        'worst_month_surplus': round(min(flows), 2),
        'best_month_surplus': round(max(flows), 2),
        'months_in_deficit': sum(1 for f in flows if f < 0),
        'cash': net_worth['cash'],
        'investments': net_worth['investments'],
        'typical_monthly_outgo': outgo,
        'runway_months': (round(net_worth['cash'] / outgo, 1) if outgo else None),
        'volatility': stability['coefficient_of_variation'],
        'confidence': _confidence(len(complete), stability, flows),
    }


# ── The two shapes ──────────────────────────────────────────────────────────

def _one_off_impact(amount, baseline):
    """What paying it from cash would do, and how long saving for it would take."""
    cash = baseline['cash']
    outgo = baseline['typical_monthly_outgo']
    surplus = baseline['median_monthly_surplus']
    cash_after = cash - amount

    return {
        'amount': round(amount, 2),
        'cash_now': round(cash, 2),
        'cash_after': round(cash_after, 2),
        'covered_by_cash': cash_after >= 0,
        'runway_after_months': (round(cash_after / outgo, 1)
                                if outgo and cash_after > 0 else 0.0),
        'runway_floor_months': MIN_RUNWAY_AFTER_MONTHS,
        'leaves_healthy_buffer': bool(
            outgo and cash_after / outgo >= MIN_RUNWAY_AFTER_MONTHS),
        # None, not infinity: a household with no surplus cannot save for this
        # at all, and "999 months" reads as a long wait rather than as "never".
        'months_to_save_from_surplus': (round(amount / surplus, 1)
                                        if surplus > 0 else None),
        'share_of_cash_pct': (round(amount / cash * 100, 1) if cash > 0 else None),
    }


def _monthly_impact(amount, baseline):
    """What committing it every month would do to the surplus."""
    surplus = baseline['median_monthly_surplus']
    income = baseline['median_monthly_income']
    after = surplus - amount

    return {
        'amount': round(amount, 2),
        'surplus_now': round(surplus, 2),
        'surplus_after': round(after, 2),
        'fits_in_surplus': after >= 0,
        'share_of_surplus_pct': (round(amount / surplus * 100, 1)
                                 if surplus > 0 else None),
        'share_of_income_pct': (round(amount / income * 100, 1)
                                if income > 0 else None),
        # The month that actually decides it. A commitment that fits the median
        # month and not the worst one is a commitment that fails occasionally,
        # which is exactly when a household can least handle it.
        'survives_worst_month': baseline['worst_month_surplus'] - amount >= 0,
        'worst_month_surplus_after': round(
            baseline['worst_month_surplus'] - amount, 2),
        'annual_cost': round(amount * 12, 2),
    }


# ── Judgement ───────────────────────────────────────────────────────────────

def _verdict(result):
    """A band and a reason. Never a yes.

    The bands describe the arithmetic — what the numbers do — rather than
    granting permission, because this module cannot see next year and the user
    can. Worst case is judged as well as typical case: the median month is what
    usually happens, and the worst month is what decides whether a commitment
    is survivable.
    """
    reasons = []
    worst = 'comfortable'

    def worsen(band, why):
        nonlocal worst
        order = {'comfortable': 0, 'tight': 1, 'not_without_changes': 2}
        if order[band] > order[worst]:
            worst = band
        reasons.append(why)

    one_off = result['one_off_impact']
    if one_off:
        if not one_off['covered_by_cash']:
            worsen('not_without_changes',
                   f"it is more than the {_money(one_off['cash_now'])} of cash on hand")
        elif not one_off['leaves_healthy_buffer']:
            worsen('tight',
                   f"it would leave {one_off['runway_after_months']} months of "
                   f'cash, under the {MIN_RUNWAY_AFTER_MONTHS:g}-month floor I use')
        else:
            reasons.append(
                f"it leaves {one_off['runway_after_months']} months of cash")

    monthly = result['monthly_impact']
    if monthly:
        surplus = result['capacity']['median_monthly_surplus']
        share = (monthly['amount'] / surplus) if surplus > 0 else None

        if surplus <= 0:
            worsen('not_without_changes',
                   'there is no surplus in a typical month to commit')
        elif share is not None and share > MAX_SURPLUS_SHARE:
            worsen('not_without_changes',
                   f"it would take {monthly['share_of_surplus_pct']}% of a "
                   'typical month’s surplus')
        elif share is not None and share > COMFORTABLE_SURPLUS_SHARE:
            worsen('tight',
                   f"it would take {monthly['share_of_surplus_pct']}% of a "
                   'typical month’s surplus')
        else:
            reasons.append(
                f"it would take {monthly['share_of_surplus_pct']}% of a "
                'typical month’s surplus')

        if not monthly['survives_worst_month']:
            worsen('tight',
                   'in the worst month of the period it would not have fitted')

    if result['capacity']['confidence'] == 'low':
        worsen('tight', 'the month-to-month picture is uneven enough that a '
                        'typical month is a weak guide')

    return worst, '; '.join(reasons) if reasons else 'nothing to assess'


def _assumptions(baseline, months):
    """Everything the verdict rests on, stated so it can be disagreed with."""
    if not baseline['can_assess']:
        return [f'I looked at the last {months} months of your records.']

    return [
        f"I used the last {baseline['months_measured']} complete months; the "
        'current month is partial and is left out so it cannot drag the '
        'figures down.',
        f"A typical month for you is {_money(baseline['median_monthly_income'])} "
        f"in and {_money(baseline['median_monthly_spending'])} out — the median "
        'month, not the average, so one refund or one annual bill does not '
        'move it.',
        'Transfers between your own accounts are movement, not spending.',
        f"Cash on hand is {_money(baseline['cash'])}. I have not counted "
        f"{_money(baseline['investments'])} of investments as available.",
        'I assume the months ahead look broadly like the months behind, which '
        'is the assumption you are best placed to check.',
    ]


def _uncertainties(baseline, monthly):
    """What could make this reading wrong. Never empty when it applies."""
    if not baseline['can_assess']:
        return ['There is not enough history for me to say anything useful yet.']

    notes = []
    if baseline['months_in_deficit']:
        notes.append(
            f"{baseline['months_in_deficit']} of the "
            f"{baseline['months_measured']} months spent more than they earned.")

    median, mean = baseline['median_monthly_surplus'], baseline['mean_monthly_surplus']
    if median and abs(mean - median) > abs(median) * 0.25:
        notes.append(
            f'Your average month ({_money(mean)} spare) and your typical month '
            f'({_money(median)} spare) differ a lot, which usually means one or '
            'two unusual months are in the window.')

    spread = baseline['best_month_surplus'] - baseline['worst_month_surplus']
    if monthly and spread > monthly * 2:
        notes.append(
            f"Your monthly surplus ranged from {_money(baseline['worst_month_surplus'])} "
            f"to {_money(baseline['best_month_surplus'])} — a wider swing than the "
            'commitment itself, so a typical month is a weak guide here.')

    if baseline['runway_months'] is not None and baseline['runway_months'] < 3:
        notes.append(
            f"Your cash covers about {baseline['runway_months']} months of normal "
            'spending before any of this.')

    notes.append('This is a reading of your records, not advice, and not a '
                 'prediction. It cannot see a job change, a rate change or a '
                 'bill that has not arrived yet.')
    return notes


def _confidence(months, stability, flows):
    """How much weight the verdict deserves."""
    variation = stability.get('coefficient_of_variation')
    if months < 4 or (variation is not None and variation > 1.0):
        return 'low'
    if months < 6 or (variation is not None and variation > 0.5):
        return 'moderate'
    if any(f < 0 for f in flows):
        return 'moderate'
    return 'high'


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _money(value):
    return f'${value:,.0f}'


__all__ = ['assess', 'capacity', 'VERDICTS', 'DEFAULT_MONTHS',
           'MIN_RUNWAY_AFTER_MONTHS', 'COMFORTABLE_SURPLUS_SHARE',
           'MAX_SURPLUS_SHARE']
