"""A complete, believable household, generated from nothing.

The demo account exists so a prospect can be shown what Dough *does* without
anybody's real ledger being on the screen. That is a harder problem than
"insert some rows", because almost every screen in this application is
**derived**: the Insights page is an opinion about transactions, Recurring is a
clustering of them, Anomalies is a set of statistics over them, and the net
worth chart is two years of snapshots. Data that is merely present makes those
pages render; only data with the right *shape* makes them say anything.

So this module is written against the thresholds the services actually use,
and each one is named where it is honoured:

  * `anomalies.MIN_SAMPLE_FOR_STATS` (8) — a category needs at least eight
    charges before a large purchase can be called unusual, so the variable
    spending categories are generated at a rate that clears it in every month.
  * `anomalies.DUPLICATE_WINDOW_DAYS` (3) — the planted double-post is two days
    apart, not two weeks.
  * `trends.MIN_MONTHS_FOR_TREND` (3) and `MIN_MONTHLY_AVERAGE` ($25) — a
    category below either is dropped from the trends list entirely, which is
    why the small categories still get real monthly volume.
  * `health.DEFAULT_MONTHS` (6) and `MIN_MONTHS_FOR_STABILITY` (3) — the
    health score needs six months of income and outgo to be measured at all.
  * `networth.snapshot_history` reads 730 days, so that is how many daily
    snapshots are written.

`HISTORY_MONTHS` is 24 rather than 12 for the same reason: the twelve-month
baselines above have to sit on top of a year that already exists, or the
oldest month of the demo is the month everything is compared against.

## Anchored to today, seeded, and re-runnable

Every date is computed from `today`, never hard-coded. A demo whose newest
transaction is from last spring reads as an abandoned account, and that is
exactly the impression the demo exists to avoid — so the fix is to re-run this,
not to edit a constant.

The RNG is seeded (`DEFAULT_SEED`), so two runs on the same day produce the
same household. Marketing screenshots taken a week apart therefore differ only
by the week, and a bug reported against the demo can be reproduced.

## What it refuses to do

`seed_demo_household` deletes every tenant-scoped row for the household it is
given. That is a destructive operation pointed at a household id, which is one
typo away from deleting a real family's ledger, so the *caller* cannot supply a
bare id and hope: `tools/seed_demo.py` resolves the household from a username
and requires `--yes`, and `assert_is_demo_household` re-checks the name here.

Allowed:   models, dough.tenancy, stdlib
Must not:  app, flask, blueprints, render_template
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

from dough.tenancy import tenant_scope, tenant_scoped_models, unscoped
from models import (AccountBalance, Budget, CategoryRule, FinancialAccount,
                    Goal, GoalContribution, Holding, Household,
                    InstitutionConnection, MarketPrice, PortfolioSnapshotRow,
                    SyncRun, Transaction, db)

#: Two years. See the module docstring — the twelve-month baselines need a
#: year of history *underneath* the year being scored.
HISTORY_MONTHS = 24

#: `networth.snapshot_history(days=730)` is what the net-worth chart reads.
SNAPSHOT_DAYS = 730

#: Fixed so the demo is reproducible. Changing it regenerates a different but
#: equally valid household; it is not load-bearing beyond reproducibility.
DEFAULT_SEED = 20260819

#: The demo household is identified by the account that owns it. A household
#: whose users are not in this set is not a demo household, and this module
#: will not delete anything belonging to it.
DEMO_USERNAMES = frozenset({'RankParsley'})


# ═══════════════════════════════════════════════════════════════════════════
# The household being portrayed
#
# Two earners, mid-thirties, roughly $145k gross between them, a mortgage, one
# car loan, one student loan left, and about $285k of net worth spread over a
# taxable brokerage, a Roth, a 401(k) and a small crypto position.
#
# The figures are chosen to be *legible* rather than impressive. Every number
# on the Investments page is reconciled: an account's balance is the sum of its
# holdings, each holding's value is shares times price, and the final day of
# the snapshot series equals the net worth the accounts add up to today. A demo
# where the chart and the tiles disagree is the one thing a prospect will
# notice, and the only thing they will remember.
# ═══════════════════════════════════════════════════════════════════════════

#: (slug, display name, [(external suffix, account name, type, mask, balance)])
#: Balances are today's. The brokerage ones are recomputed from HOLDINGS below
#: rather than trusted, so the two lists cannot drift apart.
CONNECTIONS = [
    ('plaid', 'Chase', [
        ('chk', 'Everyday Checking', 'checking', '4412', 6480.00),
        ('sav', 'Rainy Day Savings', 'savings', '8830', 41500.00),
    ]),
    ('plaid', 'Capital One', [
        ('cc', 'Quicksilver Card', 'credit', '3391', 1845.22),
    ]),
    ('plaid', 'Vanguard', [
        ('brk', 'Joint Brokerage Account', 'brokerage', '7712', None),
        ('rth', 'Roth IRA', 'brokerage', '5520', None),
    ]),
    ('plaid', 'Fidelity', [
        ('401k', 'Northwind Labs 401(k)', 'brokerage', '3308', None),
    ]),
    ('coinbase', 'Coinbase', [
        ('cb', 'Coinbase Portfolio', 'crypto', '9017', None),
    ]),
]

#: (account key, ticker, name, shares, price, avg_cost, asset class)
#: `avg_cost` is deliberately a mix: two positions are under water, because a
#: portfolio where every line is green looks generated, and the red rows are
#: what demonstrate that the gain/loss column means something.
HOLDINGS = [
    ('brk',  'VTI',    'Vanguard Total Stock Market ETF',        260,   312.40,   246.80, 'ETF'),
    ('brk',  'VXUS',   'Vanguard Total International Stock ETF', 340,    72.15,    61.40, 'ETF'),
    ('brk',  'VMFXX',  'Vanguard Federal Money Market Fund',    2140,     1.00,     None, 'Cash'),
    ('rth',  'VFIAX',  'Vanguard 500 Index Fund Admiral',         96,   675.45,   512.30, 'Mutual Fund'),
    ('rth',  'VBTLX',  'Vanguard Total Bond Market Index Adm',   420,     9.62,    10.35, 'Mutual Fund'),
    ('401k', 'FXAIX',  'Fidelity 500 Index Fund',                160,   215.30,   178.90, 'Mutual Fund'),
    ('cb',   'BTC',    'Bitcoin',                               0.18, 96800.00, 61200.00, 'Crypto'),
    ('cb',   'ETH',    'Ethereum',                               2.40,  3420.00,  3760.00, 'Crypto'),
]

# ── Transactions ────────────────────────────────────────────────────────────
#
# Transaction.account_name is the ledger's own two-value vocabulary
# ('Checking' / 'Savings'), which is what templates/upload.html offers and what
# the filter bar builds its dropdown from. It is not the institution account
# name — those live on FinancialAccount and are linked through account_id.

CHECKING = 'Checking'
SAVINGS = 'Savings'

#: Monthly obligations. (day of month, description, amount, category).
#: The categories are the household's own words, and `recurring.py` reads those
#: words to sort bills from subscriptions — 'Mortgage', 'Utilities',
#: 'Insurance Payment', 'Student Loan' and 'Auto Loan' all carry a word in
#: `BILL_CATEGORY_WORDS`, and 'Credit Card' matches a phrase. Renaming any of
#: them to something that does not will move it into the subscription tier.
FIXED_BILLS = [
    (1,  'SUNRISE MORTGAGE SERVICING', 2145.00, 'Mortgage'),
    (8,  'NORTHSTAR MOBILE',            142.00, 'Utilities'),
    (12, 'GRANITE MUTUAL AUTO POLICY',  168.40, 'Insurance Payment'),
    (14, 'SUMMIT AUTO FINANCE',         412.66, 'Auto Loan'),
    (18, 'NELNET STUDENT LOAN SVC',     285.00, 'Student Loan'),
]

#: Fixed-price monthly charges in a category `recurring.py` reads as a
#: subscription. Kept genuinely identical month to month: the Recurring page's
#: whole claim is that it can spot a fixed price, and a jittered one is a
#: weaker demonstration, not a more realistic one.
SUBSCRIPTIONS = [
    (3,  'NETFLIX.COM',           22.99),
    (7,  'SPOTIFY USA',           11.99),
    (11, 'APPLE ICLOUD STORAGE',   2.99),
    (16, 'IRONSIDE FITNESS CLUB', 49.00),
    (20, 'THE ATLANTIC DIGITAL',   8.25),
]

#: Variable spending: (category, [merchants], charges per month, low, high).
#: Every count clears `anomalies.MIN_SAMPLE_FOR_STATS` over the baseline year,
#: and every low×count clears `trends.MIN_MONTHLY_AVERAGE`.
VARIABLE_SPENDING = [
    ('Groceries', ['WHOLE FOODS MARKET', "TRADER JOE'S #402", 'KROGER #418',
                   'COSTCO WHOLESALE #77'], (4, 6), 42.00, 190.00),
    ('Food',      ['ROASTED BEAN CAFE', 'PHO 88', 'BURRITO REPUBLIC',
                   'SALT & OAK KITCHEN', 'DOORDASH', 'PIZZERIA VOLARE'],
                                            (7, 12), 11.50, 86.00),
    ('Gas',       ['SHELL OIL 4471', 'COSTCO GAS #77', 'WAWA 812'],
                                            (3, 5), 31.00, 68.00),
    ('Shopping',  ['AMAZON.COM*2K4LP', 'TARGET T-1188', 'HOME DEPOT #6602',
                   'REI CO-OP'],            (3, 6), 16.00, 240.00),
    ('Entertainment', ['REGAL CINEMAS', 'STEAM GAMES', 'THE BLUE ROOM',
                       'EVENTBRITE'],       (1, 3), 18.00, 95.00),
    ('Healthcare', ['CVS PHARMACY #3391', 'LAKESIDE FAMILY DENTAL',
                    'QUEST DIAGNOSTICS'],   (1, 2), 24.00, 210.00),
]

#: Travel is seasonal rather than monthly — it is what makes the category-spike
#: detector and the seasonal reading on the trends page have something true to
#: find. Months are calendar months (1-12).
TRAVEL_MONTHS = {6: (2, 3), 7: (2, 4), 8: (1, 3), 12: (2, 3)}

#: Take-home pay. Riley is paid every other Friday, Sam on the 1st and 15th —
#: two different cadences on purpose, because `anomalies.missing_income` infers
#: each inflow's own gap and a single cadence never exercises that.
RILEY_PAY = ('PAYROLL NORTHWIND LABS', 2650.00)
SAM_PAY = ('DIRECT DEP MERIDIAN HEALTH', 1720.00)

#: Monthly money movement. The savings transfer is written to both sides of the
#: ledger, which is what makes the Transfers view and the transfer-exclusion in
#: `analytics` visible rather than theoretical.
MONTHLY_TRANSFER = 900.00
MONTHLY_BROKERAGE = 1000.00
MONTHLY_CRYPTO = 150.00


# ═══════════════════════════════════════════════════════════════════════════
# Small date helpers
# ═══════════════════════════════════════════════════════════════════════════

def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_months(day: date, months: int) -> date:
    """`day` shifted by whole months, clamped into the target month's length."""
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, _days_in_month(year, month)))


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - date(year, month, 1)).days


def _on(year: int, month: int, day: int) -> date:
    """A date in that month, clamped — the 31st of February is the 28th."""
    return date(year, month, min(day, _days_in_month(year, month)))


def _months_back(today: date, count: int):
    """`count` month-start dates, oldest first, ending with `today`'s month."""
    first = _month_start(today)
    return [_add_months(first, -offset) for offset in range(count - 1, -1, -1)]


# ═══════════════════════════════════════════════════════════════════════════
# Transactions
# ═══════════════════════════════════════════════════════════════════════════

class _Ledger:
    """Collects transaction rows and enforces the ledger's own unique index.

    `idx_transaction_unique` is (household, account, date, description, amount).
    A generator that lands two identical charges on one day would raise
    IntegrityError at flush — a long way from the line that caused it — so
    collisions are dropped here, where the cause is obvious.

    The planted duplicate (`_plant_anomalies`) is *not* caught by this: it is
    the same merchant and amount on a different day, which is what a real
    double-post looks like and what `anomalies.duplicates` searches for.
    """

    def __init__(self):
        self.rows = []
        self._seen = set()

    def add(self, account, when, description, amount, category, flagged=False):
        """`flagged` marks the row for the Anomalies *review queue*.

        That page is a different thing from the Anomalies findings: it lists
        transactions carrying `anomaly_score == -1.0`, which is the stored
        IsolationForest verdict, while `anomalies.detect()` computes its
        findings live and stores nothing. A demo that only planted the
        statistical anomalies would leave the review queue empty — rendering a
        200 with nothing in it, which is the failure mode this whole module is
        written against.
        """
        key = (account, when, description, round(float(amount), 2))
        if key in self._seen:
            return False
        self._seen.add(key)
        self.rows.append({'account_name': account, 'date': when,
                          'description': description,
                          'amount': Decimal(f'{float(amount):.2f}'),
                          'category': category, 'flagged': flagged})
        return True


def _income(ledger, months, today):
    """Both paychecks across the window."""
    start = months[0]
    # Anchored to a Friday on or after the window opens, then every 14 days.
    friday = start + timedelta(days=(4 - start.weekday()) % 7)
    description, base = RILEY_PAY
    # March has two paydays before the 15th in most years, so the bonus is
    # tracked by year rather than tested by date. Paying it twice would put an
    # extra $3,400 into one month and quietly distort every income trend, the
    # health score's stability factor, and the missing-income baseline.
    bonus_years = set()
    while friday <= today:
        # A raise two thirds of the way through, so the income trend has a step
        # in it rather than being a flat line.
        amount = base * (1.04 if friday >= _add_months(start, 16) else 1.0)
        if friday.month == 3 and friday.year not in bonus_years:
            bonus_years.add(friday.year)
            ledger.add(CHECKING, friday, 'NORTHWIND LABS BONUS', 3400.00, 'Income')
        ledger.add(CHECKING, friday, description, round(amount, 2), 'Income')
        friday += timedelta(days=14)

    description, amount = SAM_PAY
    for month in months:
        for day in (1, 15):
            when = _on(month.year, month.month, day)
            if when <= today:
                ledger.add(CHECKING, when, description, amount, 'Income')


def _bills(ledger, months, today, rng):
    """Fixed obligations, utilities, subscriptions and the card payment."""
    for month in months:
        for day, description, amount, category in FIXED_BILLS:
            when = _on(month.year, month.month, day)
            if when <= today:
                ledger.add(CHECKING, when, description, -amount, category)

        # Energy is seasonal — high in winter and midsummer, low in the
        # shoulder months. This is what gives the Utilities trend a shape the
        # seasonal reading can describe.
        winter = month.month in (12, 1, 2)
        summer = month.month in (7, 8)
        base = 205.0 if winter else 178.0 if summer else 118.0
        when = _on(month.year, month.month, 5)
        if when <= today:
            ledger.add(CHECKING, when, 'MERIDIAN ENERGY CO-OP',
                       -round(base + rng.uniform(-14, 14), 2), 'Utilities')

        when = _on(month.year, month.month, 6)
        if when <= today:
            ledger.add(CHECKING, when, 'CLEARWATER MUNICIPAL WATER',
                       -round(rng.uniform(48, 72), 2), 'Utilities')

        for day, description, amount in SUBSCRIPTIONS:
            when = _on(month.year, month.month, day)
            if when <= today:
                ledger.add(CHECKING, when, description, -amount, 'Subscriptions')

        # A subscription that goes up. `anomalies.bill_increases` needs a fixed
        # price on both sides of the step -- BILL_INCREASE_PCT is 10% and
        # BILL_INCREASE_USD is $5, and FIXED_PRICE_TOLERANCE rejects a merchant
        # whose price merely wanders -- so this is exactly $79.99 until six
        # months ago and exactly $89.99 from then on.
        when = _on(month.year, month.month, 9)
        if when <= today:
            raised = when >= _add_months(_month_start(today), -5)
            ledger.add(CHECKING, when, 'BEACON INTERNET',
                       -(89.99 if raised else 79.99), 'Utilities')

        # The card payment, and the one bill with real variance.
        #
        # Kept deliberately small relative to the card's balance. Everything
        # this household buys is already generated as a Checking charge, so a
        # card payment sized like a real one would count the same spending
        # twice -- inflating `networth.monthly_outgo`, depressing the savings
        # rate, and pulling the health score down for a reason that exists
        # nowhere in the data. The card is portrayed as carrying a few
        # recurring charges, which is what its ~$1,845 balance reflects.
        when = _on(month.year, month.month, 22)
        if when <= today:
            ledger.add(CHECKING, when, 'CAPITAL ONE CARD PAYMENT',
                       -round(rng.uniform(210, 480), 2), 'Credit Card')



def _variable_spending(ledger, months, today, rng):
    """Groceries, dining, fuel and the rest — the noise the statistics need.

    Amounts are drawn per charge rather than per month so each category has a
    real distribution: `anomalies.large_purchases` scores against the category's
    own median and MAD, and a category generated at a constant amount has a MAD
    of zero and falls into the point-mass branch instead, which is a much less
    interesting thing to show.
    """
    for month in months:
        for category, merchants, (low_n, high_n), low, high in VARIABLE_SPENDING:
            for _ in range(rng.randint(low_n, high_n)):
                when = _on(month.year, month.month, rng.randint(1, 28))
                if when > today:
                    continue
                ledger.add(CHECKING, when, rng.choice(merchants),
                           -round(rng.uniform(low, high), 2), category)

        low_n, high_n = TRAVEL_MONTHS.get(month.month, (0, 0))
        for _ in range(rng.randint(low_n, high_n)):
            when = _on(month.year, month.month, rng.randint(1, 28))
            if when > today:
                continue
            ledger.add(CHECKING, when,
                       rng.choice(['DELTA AIR LINES', 'MARRIOTT BONVOY',
                                   'AIRBNB * HMTQ4', 'HERTZ RENT-A-CAR']),
                       -round(rng.uniform(180, 940), 2), 'Travel')


def _movement(ledger, months, today, rng):
    """Savings transfers, investment contributions and savings interest.

    The transfer is written on both sides. `analytics.base_query` excludes
    transfers from spending, so a one-sided transfer would quietly overstate
    the savings balance's growth without ever appearing as an outflow — the
    demo would then disagree with itself on the one page that adds both up.
    """
    for month in months:
        when = _on(month.year, month.month, 2)
        if when <= today:
            ledger.add(CHECKING, when, 'TRANSFER TO SAVINGS',
                       -MONTHLY_TRANSFER, 'Transfer')
            ledger.add(SAVINGS, when, 'TRANSFER FROM CHECKING',
                       MONTHLY_TRANSFER, 'Transfer')
            ledger.add(CHECKING, when, 'VANGUARD BUY - BROKERAGE',
                       -MONTHLY_BROKERAGE, 'Investments')

        # Crypto contributions stop two thirds of the way through, so the
        # Investments trend has something to say beyond "up".
        when = _on(month.year, month.month, 15)
        if when <= today and when < _add_months(months[0], 16):
            ledger.add(CHECKING, when, 'COINBASE PURCHASE',
                       -MONTHLY_CRYPTO, 'Investments')

        when = _on(month.year, month.month, _days_in_month(month.year, month.month))
        if when <= today:
            ledger.add(SAVINGS, when, 'INTEREST PAYMENT',
                       round(rng.uniform(96, 148), 2), 'Income')


def _plant_anomalies(ledger, today):
    """Three findings the Anomalies page will discover on its own.

    Planted rather than hoped for. Random spending does occasionally throw an
    outlier, but a demo that depends on it is a demo that is sometimes empty on
    the one screen whose whole subject is finding things.

    The fourth finding — a bill increase — is generated in `_bills`, and the
    fifth, a category spike, falls out of `TRAVEL_MONTHS` on its own.
    """
    # A large purchase: far above the Shopping median, and well clear of
    # MIN_LARGE_PURCHASE_USD.
    ledger.add(CHECKING, today - timedelta(days=35), 'BEST BUY #1142',
               -1899.00, 'Shopping', flagged=True)

    # A double-post: same merchant, same amount, two days apart. Inside
    # DUPLICATE_WINDOW_DAYS (3), and on different dates so the ledger's own
    # unique index does not reject the second one.
    ledger.add(CHECKING, today - timedelta(days=22), "TRADER JOE'S #402",
               -87.43, 'Groceries')
    ledger.add(CHECKING, today - timedelta(days=20), "TRADER JOE'S #402",
               -87.43, 'Groceries', flagged=True)

    # An uncategorized charge, so the Rules page has a live example to fix and
    # the dashboard's "needs attention" tile is not empty.
    ledger.add(CHECKING, today - timedelta(days=9), 'SQ *MERIDIAN MKT',
               -64.20, 'Uncategorized', flagged=True)

    # Three more for the review queue, so it reads as a queue rather than a
    # single stray row. Spread across weeks because the page sorts by date.
    for days, description, amount, category in (
            (5,  'ONE-TIME TRANSFER - ZELLE', -1250.00, 'Transfer'),
            (16, 'MARRIOTT BONVOY',            -1480.55, 'Travel'),
            (28, 'LAKESIDE FAMILY DENTAL',      -940.00, 'Healthcare')):
        ledger.add(CHECKING, today - timedelta(days=days), description,
                   amount, category, flagged=True)


# ═══════════════════════════════════════════════════════════════════════════
# Accounts, holdings and the net-worth series
# ═══════════════════════════════════════════════════════════════════════════

def _account_value(key):
    """What a brokerage/crypto account is worth: the sum of what is in it.

    The balance is derived rather than declared so `CONNECTIONS` and `HOLDINGS`
    cannot drift. An account tile reading $107,895 above a holdings list adding
    to $104,000 is the kind of detail that quietly costs a demo its credibility.
    """
    return round(sum(shares * price
                     for acct, _t, _n, shares, price, _c, _a in HOLDINGS
                     if acct == key), 2)


def _declared_balance(key):
    """The cash balance `CONNECTIONS` states for an account key.

    Cash is declared rather than derived (there are no holdings to add up), and
    reading it back through this helper is what keeps the snapshot series and
    the account tiles quoting one number instead of two.
    """
    return next(balance
                for _slug, _name, specs in CONNECTIONS
                for k, _n, _t, _m, balance in specs
                if k == key)


def _build_accounts(today):
    """Create the connections and their accounts. Returns {key: FinancialAccount}."""
    accounts = {}
    now = datetime.combine(today, datetime.min.time()).replace(hour=6, minute=12)
    for slug, display_name, specs in CONNECTIONS:
        connection = InstitutionConnection(
            institution=slug, item_id=f'demo-{display_name.lower()}',
            display_name=display_name, status='connected',
            last_sync_at=now, last_sync_status='success')
        db.session.add(connection)
        db.session.flush()          # need connection.id for the accounts below
        for key, name, account_type, mask, balance in specs:
            if balance is None:
                balance = _account_value(key)
            account = FinancialAccount(
                connection_id=connection.id, external_id=f'demo-{key}',
                name=name, account_type=account_type, mask=mask,
                balance=Decimal(f'{balance:.2f}'),
                available_balance=Decimal(f'{balance:.2f}'),
                is_active=True, last_synced_at=now)
            db.session.add(account)
            accounts[key] = account
    db.session.flush()
    return accounts


def _build_holdings(accounts, today):
    """Positions, plus the shared price cache they are quoted from."""
    now = datetime.combine(today, datetime.min.time()).replace(hour=6, minute=12)
    for key, ticker, name, shares, price, avg_cost, asset_class in HOLDINGS:
        account = accounts[key]
        db.session.add(Holding(
            ticker=ticker, name=name,
            shares=Decimal(f'{shares:.6f}'),
            current_value=Decimal(f'{shares * price:.2f}'),
            asset_class=asset_class, account_name=account.name,
            source='sync', account_id=account.id,
            external_id=f'demo-{key}-{ticker}',
            avg_cost=None if avg_cost is None else Decimal(f'{avg_cost:.4f}'),
            current_price=Decimal(f'{price:.4f}'), last_synced_at=now))

        # MarketPrice is deliberately NOT tenant-scoped -- it is a shared cache
        # of public quotes (see the class docstring). So this upserts the demo's
        # symbols rather than clearing the table: wiping it would blank the
        # price cache for every real household in the installation.
        #
        # No `unscoped()` here, and that is the point: the backstop only filters
        # tenant-scoped entities, so this query is already unfiltered. Wrapping
        # it would add a token to `grep -rn 'unscoped()'` -- which that module's
        # docstring calls a complete audit of where the safety net is off -- for
        # a table the safety net never covered.
        row = MarketPrice.query.filter_by(symbol=ticker).first()
        if row is None:
            row = MarketPrice(symbol=ticker)
            db.session.add(row)
        row.name = name
        row.price = Decimal(f'{price:.4f}')
        row.asset_class = asset_class
        row.source = 'demo'
        row.as_of = now


def _build_snapshots(today, rng):
    """Two years of daily net-worth rows, ending exactly at today's totals.

    Generated backwards from the real closing figures for the reason in the
    persona comment: the chart's last point and the tiles beside it have to be
    the same number. Walking forwards from a guessed starting balance cannot
    guarantee that, and being off by a few hundred dollars on the one screen
    people look at longest is worse than not drawing the chart.

    The shape is a drift down into the past with a drawdown around eight months
    back, so the line has a dip to recover from rather than being a
    straight-to-the-corner arrow nobody believes.
    """
    checking = _declared_balance('chk')
    savings = _declared_balance('sav')
    brokerage = _account_value('brk') + _account_value('rth') + _account_value('401k')
    crypto = _account_value('cb')

    rows = []
    for offset in range(SNAPSHOT_DAYS):
        when = today - timedelta(days=offset)
        years_back = offset / 365.0

        # Cash grows with the monthly transfer; investments compound and wobble.
        cash_growth = 1.0 - (0.016 * offset / 30.44)
        market = (1.0 - 0.11 * years_back) * _drawdown(offset)
        noise = 1.0 + rng.uniform(-0.004, 0.004)

        chk = max(900.0, checking * (1.0 - 0.004 * years_back) * noise)
        sav = max(2500.0, savings * cash_growth)
        brk = max(4000.0, brokerage * market * noise)
        cry = max(300.0, crypto * (1.0 - 0.24 * years_back) * _drawdown(offset) * noise)

        rows.append(PortfolioSnapshotRow(
            snapshot_date=when,
            checking=Decimal(f'{chk:.2f}'), savings=Decimal(f'{sav:.2f}'),
            total_cash=Decimal(f'{chk + sav:.2f}'),
            brokerage=Decimal(f'{brk:.2f}'), crypto=Decimal(f'{cry:.2f}'),
            total_investments=Decimal(f'{brk + cry:.2f}'),
            net_worth=Decimal(f'{chk + sav + brk + cry:.2f}')))

    # Today's row is written last and exactly, overriding the noise above so
    # the series terminates on the same figures the account tiles show.
    latest = rows[0]
    latest.checking = Decimal(f'{checking:.2f}')
    latest.savings = Decimal(f'{savings:.2f}')
    latest.total_cash = Decimal(f'{checking + savings:.2f}')
    latest.brokerage = Decimal(f'{brokerage:.2f}')
    latest.crypto = Decimal(f'{crypto:.2f}')
    latest.total_investments = Decimal(f'{brokerage + crypto:.2f}')
    latest.net_worth = Decimal(f'{checking + savings + brokerage + crypto:.2f}')

    db.session.add_all(rows)
    return len(rows)


def _drawdown(offset):
    """A ~14% market dip centred eight months back, recovered since.

    Returns a multiplier on the *historical* value, so a value below 1.0 here
    means the market was lower then than the trend line alone would say.
    """
    centre, width = 243, 70          # days back, half-width
    distance = abs(offset - centre)
    if distance >= width:
        return 1.0
    return 1.0 - 0.14 * (1.0 - distance / width)


# ═══════════════════════════════════════════════════════════════════════════
# Plans: budgets, goals and rules
# ═══════════════════════════════════════════════════════════════════════════

#: Set a little above and a little below what the household actually spends, so
#: the Budgets page shows a mix of on-track, close and over. A demo where every
#: bar is green demonstrates nothing about what the page is for.
BUDGETS = [
    ('Groceries', 780.00), ('Food', 520.00), ('Gas', 240.00),
    ('Shopping', 420.00), ('Entertainment', 180.00), ('Travel', 300.00),
    ('Subscriptions', 110.00), ('Utilities', 520.00), ('Healthcare', 180.00),
]

#: One goal per kind the Goals page renders differently, plus one already
#: achieved so the achieved state is visible without anybody having to finish
#: a goal during the demo.
GOALS = [
    ('Emergency fund', 'emergency_fund', 30000.00, 22400.00, 600.00, 'active',
     None, 'Six months of essentials.'),
    ('Kitchen remodel', 'home', 18000.00, 6150.00, 400.00, 'active', 14,
     'Cabinets and counters first.'),
    ('Japan, spring', 'vacation', 9000.00, 3275.00, 250.00, 'active', 20,
     'Two weeks, flights booked early.'),
    ('Pay off the car', 'debt_payoff', 14200.00, 9860.00, 350.00, 'active', 11,
     'Extra $150 a month on top of the payment.'),
    ('New laptop', 'custom', 2400.00, 2400.00, 200.00, 'achieved', None, None),
]

#: A handful of rules, some marked `ai` so the Rules page's distinction between
#: what the household wrote and what Dough wrote is visible on arrival.
RULES = [
    ('Groceries', 'whole foods', 'user'), ('Groceries', 'trader joe', 'user'),
    ('Groceries', 'kroger', 'ai'), ('Food', '/doordash|pho 88/', 'user'),
    ('Gas', 'shell oil', 'user'), ('Gas', 'wawa', 'ai'),
    ('Shopping', '/amazon|amzn/', 'user'), ('Shopping', 'target t-', 'ai'),
    ('Subscriptions', 'netflix', 'user'), ('Subscriptions', 'spotify', 'user'),
    ('Utilities', 'meridian energy', 'user'), ('Mortgage', 'sunrise mortgage', 'user'),
    ('Auto Loan', 'summit auto', 'user'), ('Student Loan', 'nelnet', 'user'),
    ('Income', 'payroll', 'user'), ('Transfer', 'transfer to savings', 'user'),
]


def _build_plans(months, today, rng):
    """Budgets, goals with their contribution history, and category rules."""
    for category, limit in BUDGETS:
        db.session.add(Budget(category=category, account_name='both',
                              monthly_limit=Decimal(f'{limit:.2f}')))

    for position, (category, keyword, source) in enumerate(RULES):
        db.session.add(CategoryRule(category=category, keyword=keyword,
                                    position=position, source=source))

    for name, kind, target, saved, monthly, status, due_months, note in GOALS:
        goal = Goal(name=name, kind=kind,
                    target_amount=Decimal(f'{target:.2f}'),
                    saved_amount=Decimal(f'{saved:.2f}'),
                    monthly_target=Decimal(f'{monthly:.2f}'),
                    target_date=(_add_months(today, due_months)
                                 if due_months else None),
                    status=status, note=note,
                    achieved_at=(datetime.combine(today - timedelta(days=48),
                                                  datetime.min.time())
                                 if status == 'achieved' else None))
        db.session.add(goal)
        db.session.flush()          # goal.id, for the contributions below
        _build_contributions(goal, saved, monthly, months, today, rng)


def _build_contributions(goal, saved, monthly, months, today, rng):
    """Deposits that add up to exactly `saved`.

    `Goal.saved_amount` is stored, not derived (see the model), so nothing
    forces these to reconcile — which is exactly why they are made to. "You put
    aside $400 last month" is the sentence the Goals page exists to say, and it
    is a lie if the history behind it sums to something else.

    The last contribution absorbs the rounding, so the total is exact rather
    than nearly right.
    """
    recent = [m for m in months if m >= _add_months(_month_start(today), -11)]
    if not recent:
        return
    amounts = [round(monthly * rng.uniform(0.7, 1.25), 2) for _ in recent]
    total = sum(amounts)
    if total <= 0:
        return
    # Scale to `saved`, then fix the residue on the final deposit.
    amounts = [round(a * saved / total, 2) for a in amounts]
    amounts[-1] = round(amounts[-1] + (saved - sum(amounts)), 2)

    for month, amount in zip(recent, amounts):
        # Clamped to today: run on the 1st or 2nd of a month and the current
        # month's deposit would otherwise be dated in the future, which the
        # Goals page reads as momentum that has not happened yet.
        when = min(_on(month.year, month.month, 3), today)
        db.session.add(GoalContribution(
            goal_id=goal.id, amount=Decimal(f'{amount:.2f}'),
            occurred_on=when, note=None))


def _build_sync_history(today, rng):
    """Recent sync runs, so the Connections and Sync History pages have a past.

    One of them failed and recovered. A history of nothing but green ticks
    tells a prospect nothing about how the app behaves when an institution is
    down, which is the question anybody who has used an aggregator will ask.
    """
    connections = InstitutionConnection.query.all()
    for offset in range(14, -1, -1):
        when = datetime.combine(today - timedelta(days=offset),
                                datetime.min.time()).replace(hour=6, minute=12)
        for connection in connections:
            failed = offset == 6 and connection.display_name == 'Coinbase'
            db.session.add(SyncRun(
                connection_id=connection.id,
                institution=connection.institution,
                trigger='scheduled',
                status='error' if failed else 'success',
                started_at=when,
                finished_at=when + timedelta(seconds=rng.randint(3, 19)),
                accounts_synced=0 if failed else 1,
                balances_updated=0 if failed else 1,
                holdings_synced=0 if failed else 2,
                transactions_added=0 if failed else rng.randint(0, 6),
                transactions_skipped=0 if failed else rng.randint(0, 3),
                error_message=('The institution declined the request. '
                               'Reconnect to continue syncing.' if failed else None)))


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

class NotADemoHousehold(Exception):
    """The household asked for is not the demo one, so nothing was deleted.

    Raised *before* any write. This is the only thing standing between a
    mistyped id and a real family's ledger, so it is an exception rather than a
    warning, and the caller cannot pass a flag to skip it.
    """


def assert_is_demo_household(household_id):
    """Raise unless every user on `household_id` is a known demo account.

    Every, not any: a household with a real member is a real household even if
    a demo account was invited into it. `AppUser` is imported here rather than
    at module scope only to keep the import list above honest about what this
    module reasons over — it is identity, not tenant data.
    """
    from models import AppUser

    with unscoped():
        household = db.session.get(Household, household_id)
        if household is None:
            raise NotADemoHousehold(f'No household with id {household_id}.')
        usernames = {u.username for u in
                     AppUser.query.filter_by(household_id=household_id).all()}

    if not usernames:
        raise NotADemoHousehold(
            f'Household {household_id} ({household.name!r}) has no users, so '
            'there is no way to confirm it is the demo account. Refusing.')
    unknown = usernames - DEMO_USERNAMES
    if unknown:
        raise NotADemoHousehold(
            f'Household {household_id} ({household.name!r}) belongs to '
            f'{sorted(unknown)}, which is not a demo account. Refusing to '
            f'delete its data. Demo accounts: {sorted(DEMO_USERNAMES)}.')
    return household


def wipe_demo_household(household_id):
    """Delete every tenant-scoped row for the household. Returns {table: count}.

    Driven off `tenant_scoped_models()` rather than a list kept here, for the
    reason that function exists: a model added next phase is wiped the moment
    it inherits the mixin, and a demo that silently keeps stale rows from one
    table is a demo that contradicts itself.

    Deleted parent-last so foreign keys inside the household (goal
    contributions, sync errors, transactions pointing at accounts) come out
    before what they reference.
    """
    order = {'goal_contributions': 0, 'sync_errors': 1, 'sync_history': 2,
             'transactions': 3, 'holdings': 4, 'financial_accounts': 5,
             'connected_accounts': 6}
    models = sorted(tenant_scoped_models(),
                    key=lambda m: order.get(m.__tablename__, 99))

    removed = {}
    with tenant_scope(household_id):
        for model in models:
            count = (db.session.query(model)
                     .filter(model.household_id == household_id)
                     .delete(synchronize_session=False))
            if count:
                removed[model.__tablename__] = count
        db.session.commit()
    return removed


def seed_demo_household(household_id, *, today=None, seed=DEFAULT_SEED,
                        history_months=HISTORY_MONTHS):
    """Wipe `household_id` and regenerate the whole demo. Returns {what: count}.

    Destructive by design — the demo is meant to be reset, and a seeder that
    appended would double every figure on the second run. `assert_is_demo_household`
    is called first and is not optional.
    """
    assert_is_demo_household(household_id)
    removed = wipe_demo_household(household_id)

    today = today or date.today()
    rng = random.Random(seed)
    months = _months_back(today, history_months)

    with tenant_scope(household_id):
        accounts = _build_accounts(today)
        _build_holdings(accounts, today)

        ledger = _Ledger()
        _income(ledger, months, today)
        _bills(ledger, months, today, rng)
        _variable_spending(ledger, months, today, rng)
        _movement(ledger, months, today, rng)
        _plant_anomalies(ledger, today)

        # Transactions carry both the ledger's account name and a link to the
        # synced account, because the two are read by different pages: the
        # filter bar groups by `account_name`, while the account drill-down
        # joins through `account_id`.
        account_ids = {CHECKING: accounts['chk'].id, SAVINGS: accounts['sav'].id}
        imported = datetime.combine(today, datetime.min.time())
        for row in ledger.rows:
            db.session.add(Transaction(
                account_name=row['account_name'], date=row['date'],
                description=row['description'], amount=row['amount'],
                category=row['category'], imported_at=imported,
                # -1.0 is the IsolationForest's "outlier" verdict, which is what
                # the /anomalies review queue filters on. See `_Ledger.add`.
                anomaly_score=-1.0 if row['flagged'] else None,
                anomaly_reviewed=False,
                source='sync', account_id=account_ids[row['account_name']],
                external_id=None))

        # The manual cash rows are the fallback `SyncRepository.compute_totals`
        # uses for an account type that has never synced. These have synced, so
        # these rows are never read for the demo — they are written anyway so
        # the numbers still agree if a connection is later disconnected.
        for account_type, key in (('checking', 'chk'), ('savings', 'sav')):
            db.session.add(AccountBalance(account_type=account_type,
                                          starting_balance=_declared_balance(key)))

        _build_plans(months, today, rng)
        _build_sync_history(today, rng)
        snapshots = _build_snapshots(today, rng)
        db.session.commit()

    return {'removed': sum(removed.values()), 'transactions': len(ledger.rows),
            'accounts': len(accounts), 'holdings': len(HOLDINGS),
            'snapshots': snapshots, 'budgets': len(BUDGETS),
            'goals': len(GOALS), 'rules': len(RULES),
            'months_of_history': history_months}


__all__ = ['seed_demo_household', 'wipe_demo_household',
           'assert_is_demo_household', 'NotADemoHousehold',
           'DEMO_USERNAMES', 'HISTORY_MONTHS', 'DEFAULT_SEED']
