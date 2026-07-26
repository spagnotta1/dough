"""Portfolio intelligence — the layer that turns holdings into a position.

The investments route already knows *what* the user owns. This module decides
what that ownership *means*: how concentrated it is, where the risk sits, what
changed, how healthy the whole shape is, and where it plausibly goes next.

Everything here is a pure function over plain dicts and lists — no Flask, no
SQLAlchemy, no clock reads except the ``today`` you pass in. Same contract as
``dashboard_intel``: the reasoning stays testable in isolation and the route
stays thin.

Two honesty rules run through the whole file, because a wealth screen that
quietly invents numbers is worse than one that shows fewer of them:

*   **Reference data is labelled as such.** Sector, region, market-cap band and
    dividend yield are not in the sync payload, so they come from the small
    curated tables below plus name heuristics. Every function that leans on
    them reports ``coverage`` — the share of portfolio value it could actually
    classify — and the UI states it.
*   **Modelled figures are never dressed as measurements.** Projections and the
    benchmark reference line carry their assumptions in the returned dict, and
    nothing here claims a live market feed the app does not have.

Vocabulary:

``severity``
    ``critical`` (real money at risk right now), ``warning`` (a shape worth
    correcting), ``info`` (a fact worth knowing), ``positive`` (something is
    going right — worth saying so).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

# ── Tunables ────────────────────────────────────────────────────────────────
# Judgement calls, not facts. They live together so a reader can argue with
# all of them in one place.

CONCENTRATION_WARN_PCT = 20.0    # a single position this large wants a look
CONCENTRATION_CRIT_PCT = 30.0
TOP3_WARN_PCT = 55.0             # three names carrying more than half
SECTOR_WARN_PCT = 35.0           # one sector dominating the equity sleeve
IDLE_CASH_PCT = 35.0             # cash share of net worth that reads as drag
IDLE_CASH_MIN_USD = 5_000.0      # ...but only once the dollars are real
THIN_CASH_PCT = 5.0              # too little dry powder is its own risk
INTERNATIONAL_TARGET_PCT = 20.0  # a common floor for ex-US exposure
MOVE_NOTABLE_PCT = 1.0           # daily move worth leading with
POSITION_MOVE_USD = 100.0        # per-holding change worth naming

# A broad index fund is not one holding, and treating it as one is the
# single most misleading thing a concentration metric can do: it tells
# someone with 80% in VTI that they carry the same risk as someone with 80%
# in one stock. Look-through splits a diversified fund's weight across this
# many nominal underlying names. The exact figure barely matters — what
# matters is that it is large enough for the fund to stop registering as
# single-name risk.
LOOKTHROUGH_NAMES = 60

TRADING_DAYS = 252
MIN_BENCHMARK_DAYS = 90       # below this a benchmark comparison is noise
SPARSE_HISTORY_POINTS = 30    # below this, measured stats carry a caveat
DEFAULT_RETURN_PCT = 7.0         # long-run nominal equity return, modelled
DEFAULT_INFLATION_PCT = 2.5
DEFAULT_VOLATILITY_PCT = 15.0    # fallback when history is too short to measure

_AVG_MONTH_DAYS = 30.44


# ══════════════════════════════════════════════════════════════════════════
# Reference data
#
# None of this arrives from the aggregator. It is a small curated table, not a
# data feed: good enough to tell someone they are 60% technology, never good
# enough to trade on. Anything it cannot place lands in "Unclassified" and is
# reported as uncovered rather than silently folded into a bucket.
# ══════════════════════════════════════════════════════════════════════════

SECTOR_BY_TICKER = {
    # Technology
    'AAPL': 'Technology', 'MSFT': 'Technology', 'NVDA': 'Technology',
    'AVGO': 'Technology', 'AMD': 'Technology', 'INTC': 'Technology',
    'CRM': 'Technology', 'ORCL': 'Technology', 'ADBE': 'Technology',
    'CSCO': 'Technology', 'QCOM': 'Technology', 'TXN': 'Technology',
    'IBM': 'Technology', 'NOW': 'Technology', 'PANW': 'Technology',
    'MU': 'Technology', 'AMAT': 'Technology', 'SHOP': 'Technology',
    'SNOW': 'Technology', 'PLTR': 'Technology', 'UBER': 'Technology',
    # Communication services
    'GOOGL': 'Communication', 'GOOG': 'Communication', 'META': 'Communication',
    'NFLX': 'Communication', 'DIS': 'Communication', 'T': 'Communication',
    'VZ': 'Communication', 'CMCSA': 'Communication', 'TMUS': 'Communication',
    # Consumer
    'AMZN': 'Consumer Discretionary', 'TSLA': 'Consumer Discretionary',
    'HD': 'Consumer Discretionary', 'MCD': 'Consumer Discretionary',
    'NKE': 'Consumer Discretionary', 'SBUX': 'Consumer Discretionary',
    'LOW': 'Consumer Discretionary', 'TGT': 'Consumer Discretionary',
    'BKNG': 'Consumer Discretionary', 'F': 'Consumer Discretionary',
    'GM': 'Consumer Discretionary', 'ABNB': 'Consumer Discretionary',
    'WMT': 'Consumer Staples', 'COST': 'Consumer Staples',
    'PG': 'Consumer Staples', 'KO': 'Consumer Staples', 'PEP': 'Consumer Staples',
    'PM': 'Consumer Staples', 'MO': 'Consumer Staples', 'CL': 'Consumer Staples',
    'MDLZ': 'Consumer Staples', 'KHC': 'Consumer Staples',
    # Financials
    'BRK.B': 'Financials', 'BRK-B': 'Financials', 'JPM': 'Financials',
    'BAC': 'Financials', 'WFC': 'Financials', 'GS': 'Financials',
    'MS': 'Financials', 'C': 'Financials', 'SCHW': 'Financials',
    'AXP': 'Financials', 'V': 'Financials', 'MA': 'Financials',
    'PYPL': 'Financials', 'BLK': 'Financials', 'COIN': 'Financials',
    # Healthcare
    'UNH': 'Healthcare', 'JNJ': 'Healthcare', 'LLY': 'Healthcare',
    'PFE': 'Healthcare', 'ABBV': 'Healthcare', 'MRK': 'Healthcare',
    'TMO': 'Healthcare', 'ABT': 'Healthcare', 'DHR': 'Healthcare',
    'BMY': 'Healthcare', 'AMGN': 'Healthcare', 'CVS': 'Healthcare',
    'ISRG': 'Healthcare', 'GILD': 'Healthcare', 'MDT': 'Healthcare',
    # Industrials
    'CAT': 'Industrials', 'BA': 'Industrials', 'HON': 'Industrials',
    'GE': 'Industrials', 'UNP': 'Industrials', 'UPS': 'Industrials',
    'RTX': 'Industrials', 'LMT': 'Industrials', 'DE': 'Industrials',
    'MMM': 'Industrials', 'FDX': 'Industrials',
    # Energy / materials / utilities / real estate
    'XOM': 'Energy', 'CVX': 'Energy', 'COP': 'Energy', 'SLB': 'Energy',
    'EOG': 'Energy', 'PSX': 'Energy', 'MPC': 'Energy',
    'LIN': 'Materials', 'SHW': 'Materials', 'FCX': 'Materials',
    'NEM': 'Materials', 'DOW': 'Materials',
    'NEE': 'Utilities', 'DUK': 'Utilities', 'SO': 'Utilities',
    'D': 'Utilities', 'AEP': 'Utilities',
    'AMT': 'Real Estate', 'PLD': 'Real Estate', 'SPG': 'Real Estate',
    'O': 'Real Estate', 'EQIX': 'Real Estate', 'PSA': 'Real Estate',
}

# Funds are classified by what they hold, so a broad index fund is its own
# bucket rather than being forced into a sector it does not have.
_SECTOR_NAME_HINTS = (
    (('TREASURY', 'BOND', 'AGGREGATE', 'FIXED INCOME', 'MUNI', 'TIPS',
      'CORPORATE DEBT', 'GOVT'), 'Fixed Income'),
    (('MONEY MARKET', 'CASH RESERVES', 'SWEEP', 'FEDERAL MONEY'), 'Cash & Equivalents'),
    (('REIT', 'REAL ESTATE'), 'Real Estate'),
    (('TECHNOLOGY', 'SEMICONDUCTOR', 'SOFTWARE', 'INFORMATION TECH'), 'Technology'),
    (('HEALTH', 'BIOTECH', 'PHARMA', 'MEDICAL'), 'Healthcare'),
    (('FINANCIAL', 'BANK', 'INSURANCE'), 'Financials'),
    (('ENERGY', 'OIL', 'GAS ', 'PETROLEUM'), 'Energy'),
    (('UTILIT',), 'Utilities'),
    (('INDUSTRIAL', 'AEROSPACE', 'TRANSPORT'), 'Industrials'),
    (('MATERIAL', 'GOLD', 'SILVER', 'COMMODIT', 'MINING'), 'Materials'),
    (('CONSUMER STAPLE',), 'Consumer Staples'),
    (('CONSUMER DISCRETION', 'RETAIL'), 'Consumer Discretionary'),
    (('COMMUNICATION', 'MEDIA', 'TELECOM'), 'Communication'),
    # Broad-market wording is checked last: "TOTAL STOCK MARKET INDEX" should
    # not be caught by a narrower rule above it.
    (('S&P 500', 'S&P500', 'TOTAL STOCK', 'TOTAL MARKET', 'BROAD MARKET',
      'TOTAL WORLD', 'TOTAL INTERNATIONAL', 'DEVELOPED MARKET',
      'EMERGING MARKET', 'ALL-WORLD', 'INDEX FUND', 'TARGET RETIREMENT',
      'BALANCED', 'GROWTH INDEX', 'VALUE INDEX', '500 INDEX', 'NASDAQ',
      'RUSSELL', 'DOW JONES', 'EXTENDED MARKET'), 'Diversified Fund'),
)

_BROAD_FUND_TICKERS = {
    'VTI', 'VOO', 'VT', 'VTSAX', 'VFIAX', 'VTWAX', 'SPY', 'IVV', 'QQQ', 'DIA',
    'SCHB', 'SCHX', 'ITOT', 'SWTSX', 'FXAIX', 'FSKAX', 'FZROX', 'VXF', 'IWM',
    'VTHR', 'SPTM', 'SPLG',
}
_INTERNATIONAL_TICKERS = {
    'VXUS', 'VEU', 'VEA', 'VWO', 'IXUS', 'EFA', 'EEM', 'IEFA', 'IEMG', 'SCHF',
    'SCHE', 'VTIAX', 'FTIHX', 'FZILX', 'VGK', 'VPL', 'EWJ', 'INDA', 'MCHI',
    'FXI', 'VSS', 'ACWI', 'VT', 'VTWAX', 'IDEV',
}
_BOND_TICKERS = {
    'BND', 'AGG', 'BNDX', 'VBTLX', 'VTEB', 'MUB', 'TLT', 'IEF', 'SHY', 'GOVT',
    'LQD', 'HYG', 'JNK', 'TIP', 'VTIP', 'SCHZ', 'FXNAX', 'VCIT', 'VCSH', 'SGOV',
    'BIL', 'VGIT', 'VGSH',
}

# Approximate trailing yields, in percent. Directionally right for planning,
# not a quote. Anything absent falls back to the asset-class default.
YIELD_BY_TICKER = {
    'VTI': 1.3, 'VOO': 1.3, 'SPY': 1.2, 'IVV': 1.3, 'VT': 1.9, 'VTSAX': 1.3,
    'VFIAX': 1.3, 'FXAIX': 1.3, 'FSKAX': 1.3, 'QQQ': 0.6, 'DIA': 1.6,
    'VXUS': 3.0, 'VEA': 3.1, 'VWO': 2.8, 'VTIAX': 3.0, 'IEFA': 3.0, 'EFA': 2.9,
    'BND': 3.7, 'AGG': 3.6, 'BNDX': 4.3, 'VBTLX': 3.7, 'TLT': 4.0, 'SHY': 4.2,
    'LQD': 4.5, 'HYG': 6.5, 'JNK': 6.9, 'MUB': 3.0, 'VTEB': 3.1, 'SGOV': 5.0,
    'BIL': 5.0, 'VMFXX': 4.8, 'SPAXX': 4.7, 'SWVXX': 4.7,
    'VYM': 2.9, 'SCHD': 3.4, 'DVY': 3.6, 'VIG': 1.8, 'NOBL': 2.1,
    'VNQ': 3.8, 'SCHH': 3.5, 'O': 5.4, 'AMT': 3.2, 'PLD': 3.4, 'SPG': 5.1,
    'AAPL': 0.5, 'MSFT': 0.7, 'NVDA': 0.03, 'GOOGL': 0.5, 'AMZN': 0.0,
    'META': 0.3, 'TSLA': 0.0, 'BRK.B': 0.0, 'BRK-B': 0.0,
    'JPM': 2.1, 'BAC': 2.4, 'WFC': 2.3, 'V': 0.7, 'MA': 0.5,
    'JNJ': 3.1, 'PFE': 6.2, 'ABBV': 3.5, 'MRK': 2.9, 'LLY': 0.7, 'UNH': 1.6,
    'KO': 3.0, 'PEP': 3.4, 'PG': 2.4, 'WMT': 1.1, 'COST': 0.6, 'MO': 7.8,
    'PM': 4.6, 'XOM': 3.3, 'CVX': 4.2, 'T': 5.1, 'VZ': 6.3,
    'HD': 2.4, 'MCD': 2.3, 'NKE': 1.8, 'DIS': 0.8, 'NFLX': 0.0,
    'IBM': 3.2, 'CSCO': 2.9, 'INTC': 1.5, 'AVGO': 1.2, 'QCOM': 1.9, 'TXN': 2.8,
    'CAT': 1.5, 'BA': 0.0, 'HON': 2.0, 'GE': 0.7, 'UNP': 2.2, 'UPS': 4.4,
    'LMT': 2.7, 'RTX': 2.3, 'MMM': 2.7, 'NEE': 2.9, 'DUK': 3.9, 'SO': 3.4,
    'LIN': 1.2, 'SHW': 0.8, 'ORCL': 0.9, 'CRM': 0.0, 'ADBE': 0.0, 'AMD': 0.0,
}

# Asset-class fallbacks when the ticker is unknown. Deliberately conservative:
# a stock we cannot identify is assumed to pay nothing rather than assumed to
# pay the market average, so the income estimate errs low.
YIELD_BY_CLASS = {
    'Stock': 0.0, 'ETF': 1.4, 'Mutual Fund': 1.6, 'Bond': 4.0,
    'Cash': 4.5, 'Crypto': 0.0, 'Other': 0.0,
}

# Rough per-asset-class annualized volatility, in percent. Used only when the
# snapshot history is too short to measure the portfolio's own.
VOLATILITY_BY_CLASS = {
    'Stock': 22.0, 'ETF': 16.0, 'Mutual Fund': 15.0, 'Bond': 6.0,
    'Cash': 0.5, 'Crypto': 65.0, 'Other': 18.0,
}

_MEGA_CAP = {
    'AAPL', 'MSFT', 'NVDA', 'GOOGL', 'GOOG', 'AMZN', 'META', 'BRK.B', 'BRK-B',
    'LLY', 'AVGO', 'TSLA', 'JPM', 'WMT', 'V', 'MA', 'XOM', 'UNH', 'ORCL',
    'COST', 'JNJ', 'PG', 'HD', 'NFLX', 'ABBV',
}


def _upper(value):
    return str(value or '').upper()


def classify_sector(ticker, name, asset_class):
    """Best-effort sector for a holding, or ``'Unclassified'``.

    Ticker lookup wins over name matching: ``T`` is AT&T, not every fund with
    a T in its name.
    """
    tick = _upper(ticker)
    if asset_class == 'Crypto':
        return 'Digital Assets'
    if asset_class == 'Cash':
        return 'Cash & Equivalents'
    if tick in SECTOR_BY_TICKER:
        return SECTOR_BY_TICKER[tick]
    if tick in _BOND_TICKERS:
        return 'Fixed Income'
    if tick in _BROAD_FUND_TICKERS or tick in _INTERNATIONAL_TICKERS:
        return 'Diversified Fund'
    label = _upper(name)
    for needles, sector in _SECTOR_NAME_HINTS:
        if any(n in label for n in needles):
            return sector
    if asset_class == 'Bond':
        return 'Fixed Income'
    return 'Unclassified'


def classify_region(ticker, name, asset_class):
    """United States / International / Global / Digital Assets / Unclassified."""
    tick = _upper(ticker)
    if asset_class == 'Crypto':
        return 'Digital Assets'
    if asset_class == 'Cash':
        return 'Cash & Equivalents'
    label = _upper(name)
    if tick in {'VT', 'VTWAX', 'ACWI'} or 'ALL-WORLD' in label or 'TOTAL WORLD' in label:
        return 'Global'
    if tick in _INTERNATIONAL_TICKERS:
        return 'International'
    if any(n in label for n in ('INTERNATIONAL', 'EX-US', 'EX US', 'EMERGING',
                                'DEVELOPED MARKET', 'EUROPE', 'PACIFIC',
                                'JAPAN', 'CHINA', 'INDIA', 'GLOBAL')):
        return 'International'
    if tick in SECTOR_BY_TICKER or tick in _BROAD_FUND_TICKERS or tick in _BOND_TICKERS:
        return 'United States'
    if any(n in label for n in ('S&P 500', 'TOTAL STOCK MARKET', 'US ', 'U.S.',
                                'NASDAQ', 'RUSSELL', 'DOW JONES', 'TREASURY')):
        return 'United States'
    return 'Unclassified'


def classify_market_cap(ticker, name, asset_class):
    """Mega / Large / Mid / Small cap, or a non-equity bucket."""
    tick = _upper(ticker)
    if asset_class == 'Crypto':
        return 'Digital Assets'
    if asset_class in ('Cash', 'Bond'):
        return 'Non-Equity'
    label = _upper(name)
    if 'SMALL' in label or tick in {'IWM', 'VB', 'VTWO', 'IJR', 'SCHA', 'VSS'}:
        return 'Small Cap'
    if 'MID' in label or tick in {'IJH', 'VO', 'MDY', 'SCHM'}:
        return 'Mid Cap'
    if tick in _MEGA_CAP:
        return 'Mega Cap'
    if tick in _BOND_TICKERS:
        return 'Non-Equity'
    if tick in SECTOR_BY_TICKER or tick in _BROAD_FUND_TICKERS or tick in _INTERNATIONAL_TICKERS:
        return 'Large Cap'
    if any(n in label for n in ('LARGE', '500 INDEX', 'TOTAL STOCK', 'GROWTH', 'VALUE')):
        return 'Large Cap'
    return 'Unclassified'


def estimate_yield(ticker, asset_class):
    """Estimated trailing dividend/interest yield in percent."""
    tick = _upper(ticker)
    if tick in YIELD_BY_TICKER:
        return YIELD_BY_TICKER[tick]
    return YIELD_BY_CLASS.get(asset_class, 0.0)


# ── Small helpers ───────────────────────────────────────────────────────────

def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    raise TypeError(f'unsupported date value: {value!r}')


def _safe_div(numerator, denominator, default=0.0):
    return numerator / denominator if denominator else default


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _pct(part, whole):
    return round(_safe_div(part, whole) * 100.0, 2)


def _money(value):
    """Whole dollars, the resolution people reason in when scanning."""
    sign = '-' if value < 0 else ''
    return f'{sign}${abs(value):,.0f}'


def _pct_str(value, places=1):
    return f'{value:+.{places}f}%'


# ══════════════════════════════════════════════════════════════════════════
# Positions
# ══════════════════════════════════════════════════════════════════════════

def build_positions(holdings, include_cash=False):
    """Enrich raw holding dicts into the position rows the whole page uses.

    ``holdings`` are ``Holding.to_dict()`` shapes. Cash-class rows are the
    brokerage sweep; they are counted as cash at the net-worth level, so by
    default they stay out of the invested portfolio to avoid double counting.

    Every derived classification carries ``*_known`` alongside it, so a caller
    can tell "this is Technology" apart from "we could not tell".
    """
    rows = []
    for h in holdings:
        asset_class = h.get('asset_class') or 'Other'
        if not include_cash and asset_class == 'Cash':
            continue
        value = float(h.get('current_value') or 0.0)
        ticker = h.get('ticker') or ''
        name = h.get('name') or ticker
        sector = classify_sector(ticker, name, asset_class)
        region = classify_region(ticker, name, asset_class)
        cap = classify_market_cap(ticker, name, asset_class)
        div_yield = estimate_yield(ticker, asset_class)
        rows.append({
            'id': h.get('id'),
            'ticker': ticker,
            'name': name,
            'shares': float(h.get('shares') or 0.0),
            'value': round(value, 2),
            'asset_class': asset_class,
            'account': h.get('account_name') or 'Brokerage',
            'account_id': h.get('account_id'),
            'source': h.get('source') or 'manual',
            'avg_cost': h.get('avg_cost'),
            'price': h.get('current_price'),
            'cost_basis': h.get('cost_basis'),
            'gain': h.get('gain_loss'),
            'gain_pct': h.get('gain_pct'),
            'sector': sector,
            'sector_known': sector != 'Unclassified',
            # A broad fund is many companies in one row. Every concentration
            # figure downstream depends on knowing the difference.
            'diversified': sector == 'Diversified Fund',
            'region': region,
            'region_known': region != 'Unclassified',
            'market_cap': cap,
            'market_cap_known': cap != 'Unclassified',
            'yield_pct': div_yield,
            'yield_known': _upper(ticker) in YIELD_BY_TICKER,
            'income': round(value * div_yield / 100.0, 2),
            'last_synced_at': h.get('last_synced_at'),
        })

    total = sum(r['value'] for r in rows)
    for r in rows:
        r['weight'] = _pct(r['value'], total)
    rows.sort(key=lambda r: -r['value'])
    return rows


def allocation(positions, key, known_key=None):
    """Value and share by any position key, largest first.

    Returns ``{'buckets': [...], 'total': float, 'coverage': pct}`` where
    coverage is the share of value the classifier could actually place. A
    breakdown that is 40% "Unclassified" is a different claim from one that
    is 2%, and the caller needs to be able to say so.
    """
    total = sum(p['value'] for p in positions)
    grouped = {}
    for p in positions:
        grouped[p[key]] = grouped.get(p[key], 0.0) + p['value']

    known_value = total
    if known_key:
        known_value = sum(p['value'] for p in positions if p.get(known_key))

    buckets = [
        {'label': label, 'value': round(value, 2), 'pct': _pct(value, total)}
        for label, value in sorted(grouped.items(), key=lambda kv: -kv[1])
    ]
    return {
        'buckets': buckets,
        'total': round(total, 2),
        'coverage': _pct(known_value, total) if known_key else 100.0,
    }


# ══════════════════════════════════════════════════════════════════════════
# Concentration
# ══════════════════════════════════════════════════════════════════════════

def _position_summary(position, total):
    return {
        'ticker': position['ticker'],
        'name': position['name'],
        'value': position['value'],
        'pct': _pct(position['value'], total),
    }


def concentration(positions):
    """How much of the portfolio rides on how few *companies*.

    Two different questions live here, and conflating them is how a portfolio
    screen ends up telling someone that 80% in a total-market fund is the same
    risk as 80% in one stock:

    ``top1_pct`` / ``top3_pct`` / ``top5_pct``
        Position-level facts. "Three rows are 98% of the money" is true and
        worth saying, whatever those rows contain.

    ``single_name_*`` and ``effective_positions``
        Risk. Broad funds are looked through — their weight is spread across
        ``LOOKTHROUGH_NAMES`` nominal holdings — so a diversified fund stops
        registering as a concentrated bet. ``effective_positions`` is the
        reciprocal HHI over those look-through weights: the number of equally
        sized *names* that would carry the same risk.
    """
    total = sum(p['value'] for p in positions)
    if not positions or total <= 0:
        return {
            'positions': 0, 'top1_pct': 0.0, 'top3_pct': 0.0, 'top5_pct': 0.0,
            'hhi': 0.0, 'effective_positions': 0.0, 'largest': None,
            'single_name_top1_pct': 0.0, 'single_name_top3_pct': 0.0,
            'single_name_largest': None, 'diversified_pct': 0.0,
        }

    ordered = sorted(positions, key=lambda p: -p['value'])
    weights = [_safe_div(p['value'], total) for p in ordered]

    # Look-through weights: one entry per nominal name.
    name_weights = []
    for p, w in zip(ordered, weights):
        if p.get('diversified'):
            name_weights.extend([w / LOOKTHROUGH_NAMES] * LOOKTHROUGH_NAMES)
        else:
            name_weights.append(w)
    sum_sq = sum(w * w for w in name_weights)

    singles = [p for p in ordered if not p.get('diversified')]
    single_weights = [_safe_div(p['value'], total) for p in singles]

    def head_pct(values, n):
        return round(sum(values[:n]) * 100.0, 2)

    return {
        'positions': len(ordered),
        'top1_pct': head_pct(weights, 1),
        'top3_pct': head_pct(weights, 3),
        'top5_pct': head_pct(weights, 5),
        'hhi': round(sum_sq * 10_000, 1),
        'effective_positions': round(_safe_div(1.0, sum_sq), 1),
        'largest': _position_summary(ordered[0], total),
        'single_name_top1_pct': head_pct(single_weights, 1),
        'single_name_top3_pct': head_pct(single_weights, 3),
        'single_name_largest': _position_summary(singles[0], total) if singles else None,
        'diversified_pct': _pct(
            sum(p['value'] for p in ordered if p.get('diversified')), total),
    }


# ══════════════════════════════════════════════════════════════════════════
# Performance, from the daily net-worth snapshots
# ══════════════════════════════════════════════════════════════════════════

def _series(history, field):
    """[(date, value)] for one snapshot field, oldest first."""
    out = []
    for row in history:
        try:
            out.append((_as_date(row['date']), float(row.get(field) or 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda pair: pair[0])
    return out


def _value_on_or_before(series, target):
    """The last observation at or before ``target``, or None."""
    best = None
    for day, value in series:
        if day <= target:
            best = (day, value)
        else:
            break
    return best


def performance(history, today, field='total_investments'):
    """Change over the standard windows, measured — never modelled.

    Each window reports ``available``: with two weeks of snapshots there is no
    honest YTD number, and showing 0.0% would read as "flat" rather than
    "unknown".
    """
    today = _as_date(today)
    series = _series(history, field)
    if not series:
        return {'current': 0.0, 'windows': {}, 'first_date': None, 'points': 0}

    current_day, current = series[-1]
    windows = {}
    targets = {
        'day': current_day - timedelta(days=1),
        'week': today - timedelta(days=7),
        'month': today - timedelta(days=30),
        'quarter': today - timedelta(days=91),
        'ytd': date(today.year, 1, 1) - timedelta(days=1),
        'year': today - timedelta(days=365),
        'all': series[0][0],
    }
    for name, target in targets.items():
        if name == 'all':
            base_day, base = series[0]
        else:
            found = _value_on_or_before(series, target)
            if found is None or found[0] == current_day:
                windows[name] = {'available': False}
                continue
            base_day, base = found
        change = current - base
        windows[name] = {
            'available': True,
            'from_date': base_day.strftime('%Y-%m-%d'),
            'from_value': round(base, 2),
            'change': round(change, 2),
            'change_pct': round(_safe_div(change, base) * 100.0, 2) if base else None,
        }

    first_day = series[0][0]
    span_days = max(1, (current_day - first_day).days)
    return {
        'current': round(current, 2),
        'as_of': current_day.strftime('%Y-%m-%d'),
        'first_date': first_day.strftime('%Y-%m-%d'),
        'points': len(series),
        'span_days': span_days,
        # A handful of readings taken while accounts were still being linked
        # will produce dramatic-looking swings. The figures are real; what
        # they measure is mostly setup, and the UI needs to be able to say so.
        'sparse': len(series) < SPARSE_HISTORY_POINTS,
        'windows': windows,
    }


def annualized_return(history, field='total_investments'):
    """CAGR over the snapshot span, or None when the span is too short.

    Under a year of history, annualizing amplifies noise into a headline —
    a 3% month becomes "42% a year". Below 120 days this returns None and the
    caller shows the raw period change instead.
    """
    series = _series(history, field)
    if len(series) < 2:
        return None
    (first_day, first), (last_day, last) = series[0], series[-1]
    days = (last_day - first_day).days
    if days < 120 or first <= 0 or last <= 0:
        return None
    years = days / 365.25
    return round(((last / first) ** (1 / years) - 1) * 100.0, 2)


def volatility(history, field='total_investments'):
    """Annualized standard deviation of daily returns, in percent.

    Returns ``(value, measured)``. When there are fewer than 30 usable daily
    returns the number is not a measurement of anything, so ``measured`` is
    False and the caller should fall back to the asset-class estimate.
    """
    series = _series(history, field)
    returns = []
    for (_, prev), (_, cur) in zip(series, series[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)
    if len(returns) < 30:
        return None, False
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0, 2), True


def estimated_volatility(positions):
    """Value-weighted volatility from the per-class table — a modelled figure.

    Ignores correlation entirely, so it overstates a diversified portfolio.
    It exists only so a brand-new account has *something* to show; anything
    with real history uses the measured number instead.
    """
    total = sum(p['value'] for p in positions)
    if total <= 0:
        return DEFAULT_VOLATILITY_PCT
    weighted = sum(VOLATILITY_BY_CLASS.get(p['asset_class'], 18.0) * p['value']
                   for p in positions)
    return round(weighted / total, 1)


def drawdown(history, field='total_investments'):
    """Deepest peak-to-trough decline in the snapshot record."""
    series = _series(history, field)
    if len(series) < 2:
        return {'max_pct': 0.0, 'peak': None, 'trough': None, 'current_pct': 0.0}

    peak_value = series[0][1]
    peak_day = series[0][0]
    worst = {'max_pct': 0.0, 'peak': None, 'trough': None}
    for day, value in series:
        if value > peak_value:
            peak_value, peak_day = value, day
        elif peak_value > 0:
            decline = (value - peak_value) / peak_value * 100.0
            if decline < worst['max_pct']:
                worst = {
                    'max_pct': round(decline, 2),
                    'peak': {'date': peak_day.strftime('%Y-%m-%d'), 'value': round(peak_value, 2)},
                    'trough': {'date': day.strftime('%Y-%m-%d'), 'value': round(value, 2)},
                }

    all_time_high = max(v for _, v in series)
    current = series[-1][1]
    worst['current_pct'] = round(_safe_div(current - all_time_high, all_time_high) * 100.0, 2)
    worst['all_time_high'] = round(all_time_high, 2)
    return worst


# ══════════════════════════════════════════════════════════════════════════
# Risk & diversification
# ══════════════════════════════════════════════════════════════════════════

_RISK_BANDS = (
    (78, 'aggressive',   'Aggressive'),
    (58, 'growth',       'Growth'),
    (38, 'balanced',     'Balanced'),
    (18, 'conservative', 'Conservative'),
    (0,  'defensive',    'Defensive'),
)

_SCORE_BANDS = (
    (80, 'strong', 'Strong'),
    (62, 'solid',  'Solid'),
    (40, 'watch',  'Needs work'),
    (0,  'weak',   'Fragile'),
)


def _band(score, table):
    for threshold, key, label in table:
        if score >= threshold:
            return key, label
    return table[-1][1], table[-2][2]


# Growth-asset weight per class: how much of a dollar there behaves like
# equity. This is what turns a holdings list into a risk posture.
_GROWTH_WEIGHT = {
    'Stock': 1.0, 'ETF': 0.9, 'Mutual Fund': 0.85, 'Other': 0.7,
    'Crypto': 1.4, 'Bond': 0.25, 'Cash': 0.0,
}


def risk_score(positions, cash=0.0, measured_vol=None):
    """0–100 risk posture, where higher means more growth exposure.

    Not a quality judgement — an aggressive portfolio is right for some people
    and wrong for others. The score exists so the reader can check that what
    they hold matches what they think they hold.
    """
    invested = sum(p['value'] for p in positions)
    base = invested + max(0.0, cash)
    if base <= 0:
        return {'score': 0, 'band': 'defensive', 'label': 'Defensive',
                'growth_pct': 0.0, 'volatility': 0.0, 'volatility_measured': False,
                'crypto_pct': 0.0, 'detail': 'Nothing invested yet.'}

    growth = sum(_GROWTH_WEIGHT.get(p['asset_class'], 0.8) * p['value'] for p in positions)
    growth_pct = _clamp(_safe_div(growth, base) * 100.0, 0.0, 140.0)
    crypto_pct = _pct(sum(p['value'] for p in positions if p['asset_class'] == 'Crypto'), base)

    vol = measured_vol if measured_vol is not None else estimated_volatility(positions)
    # Growth share carries the score; volatility nudges it, so two portfolios
    # that are both 90% equity still separate if one of them is 90% one stock.
    score = _clamp(growth_pct * 0.72 + _clamp(vol, 0, 60) * 0.55)
    key, label = _band(score, _RISK_BANDS)

    return {
        'score': int(round(score)),
        'band': key,
        'label': label,
        'growth_pct': round(growth_pct, 1),
        'crypto_pct': crypto_pct,
        'volatility': vol,
        'volatility_measured': measured_vol is not None,
        'detail': (f'{growth_pct:.0f}% of the balance sits in growth assets, at an '
                   f'{"observed" if measured_vol is not None else "estimated"} '
                   f'{vol:.0f}% annualized volatility.'),
    }


def diversification_score(positions, sector_alloc, region_alloc, conc):
    """0–100 on how spread the money is, plus the factors behind it.

    Four inputs, weighted by how much each actually reduces the chance of one
    bad event mattering:

    * **Concentration (35)** — the single largest determinant. One position at
      40% dominates every other consideration.
    * **Sector spread (25)** — sector risk is the one that surprises people;
      six tech names feel diversified and are not.
    * **Asset-class mix (20)** — equity-only is a posture, not a portfolio.
    * **Geography (20)** — a US-only book is a bet on one economy.
    """
    factors = []
    total = sum(p['value'] for p in positions)
    if total <= 0:
        return {'score': 0, 'band': 'weak', 'label': 'Fragile', 'factors': []}

    # Concentration — effective positions is the honest count.
    eff = conc['effective_positions']
    conc_score = _clamp(math.log(max(eff, 1.0)) / math.log(25.0) * 100.0)
    factors.append({
        'key': 'concentration', 'label': 'Position spread', 'weight': 35,
        'score': int(round(conc_score)),
        'detail': (f"Your largest position is {conc['top1_pct']:.0f}% of the portfolio; "
                   f"the money behaves like {eff:.1f} equally sized holdings."),
    })

    # Sector spread — measured over what we could classify.
    placed = [b for b in sector_alloc['buckets'] if b['label'] != 'Unclassified']
    if placed:
        placed_total = sum(b['value'] for b in placed)
        shares = [_safe_div(b['value'], placed_total) for b in placed]
        # A broad index fund is diversification, not a sector bet, so it is
        # scored as if it were spread rather than as one large "sector".
        broad = sum(s for b, s in zip(placed, shares) if b['label'] == 'Diversified Fund')
        top_share = max((s for b, s in zip(placed, shares)
                         if b['label'] != 'Diversified Fund'), default=0.0)
        effective_top = top_share * (1 - broad) + broad * 0.12
        sector_score = _clamp((1 - effective_top / 0.60) * 100.0)
        top_label = max(placed, key=lambda b: b['value'])['label']
        detail = (f"{top_label} is your largest sleeve at "
                  f"{max(shares) * 100:.0f}% of classified holdings.")
    else:
        sector_score, detail = 50.0, 'Not enough sector data to judge.'
    factors.append({'key': 'sector', 'label': 'Sector balance', 'weight': 25,
                    'score': int(round(sector_score)), 'detail': detail})

    # Asset-class mix.
    classes = {}
    for p in positions:
        classes[p['asset_class']] = classes.get(p['asset_class'], 0.0) + p['value']
    top_class = max(classes.values()) if classes else total
    class_score = _clamp((1 - _safe_div(top_class, total) / 0.92) * 100.0 + 12)
    factors.append({
        'key': 'asset_class', 'label': 'Asset-class mix', 'weight': 20,
        'score': int(round(class_score)),
        'detail': (f"{len(classes)} asset class{'es' if len(classes) != 1 else ''}, "
                   f"the largest at {_safe_div(top_class, total) * 100:.0f}%."),
    })

    # Geography.
    intl = sum(b['value'] for b in region_alloc['buckets']
               if b['label'] in ('International', 'Global'))
    intl_pct = _pct(intl, total)
    geo_score = _clamp(_safe_div(intl_pct, INTERNATIONAL_TARGET_PCT) * 100.0)
    factors.append({
        'key': 'geography', 'label': 'Geographic reach', 'weight': 20,
        'score': int(round(geo_score)),
        'detail': (f'{intl_pct:.0f}% of the portfolio is international or global '
                   f'against a {INTERNATIONAL_TARGET_PCT:.0f}% reference.'),
    })

    score = sum(f['score'] * f['weight'] for f in factors) / 100.0
    key, label = _band(score, _SCORE_BANDS)
    for f in factors:
        f['status'] = 'good' if f['score'] >= 70 else 'ok' if f['score'] >= 45 else 'poor'
    return {'score': int(round(score)), 'band': key, 'label': label, 'factors': factors}


# ══════════════════════════════════════════════════════════════════════════
# Portfolio health — the headline score
# ══════════════════════════════════════════════════════════════════════════

def health_score(*, positions, cash, net_worth, diversification, conc,
                 monthly_expenses=None):
    """One 0–100 read on the whole portfolio, with the factors and the fixes.

    Distinct from ``diversification_score``, which only asks whether the money
    is spread out. This asks whether the *whole shape* — spread, concentration,
    cash posture, emergency liquidity, and how much of it we can even measure —
    is one a careful advisor would sign off on.
    """
    factors = []
    total = sum(p['value'] for p in positions)
    base = total + max(0.0, cash)

    factors.append({
        'key': 'diversification', 'label': 'Diversification', 'weight': 30,
        'score': diversification['score'],
        'detail': f"Spread scores {diversification['score']}/100 across positions, "
                  f"sectors, classes and geography.",
    })

    # Concentration gets its own weight on top of its role inside
    # diversification: one oversized position is the failure mode that
    # actually wipes people out, and it deserves to move the headline twice.
    # Measured on single names — a large index-fund position is the shape
    # this factor is trying to encourage, not the risk it is warning about.
    top1 = conc['single_name_top1_pct']
    conc_score = _clamp(100.0 - max(0.0, top1 - 10.0) * 3.2)
    largest = conc.get('single_name_largest')
    if largest:
        detail = f"{largest['ticker']} is your largest single company at {top1:.0f}%."
    elif conc.get('largest'):
        detail = (f"Every position is a diversified fund, so no single company "
                  f"carries meaningful weight.")
    else:
        detail = 'No positions yet.'
    factors.append({
        'key': 'concentration', 'label': 'Single-company risk', 'weight': 22,
        'score': int(round(conc_score)), 'detail': detail,
    })

    # Cash posture — penalised at both ends.
    cash_pct = _pct(cash, net_worth) if net_worth else 0.0
    if cash_pct > IDLE_CASH_PCT:
        cash_score = _clamp(100.0 - (cash_pct - IDLE_CASH_PCT) * 2.4)
        cash_detail = f'{cash_pct:.0f}% of net worth is sitting in cash.'
    elif cash_pct < THIN_CASH_PCT:
        cash_score = _clamp(40.0 + cash_pct * 12.0)
        cash_detail = f'Only {cash_pct:.0f}% of net worth is liquid.'
    else:
        cash_score = 100.0
        cash_detail = f'{cash_pct:.0f}% in cash — a workable buffer.'
    factors.append({'key': 'cash', 'label': 'Cash allocation', 'weight': 18,
                    'score': int(round(cash_score)), 'detail': cash_detail})

    # Emergency liquidity, when we know what a month costs.
    if monthly_expenses and monthly_expenses > 0:
        months = _safe_div(cash, monthly_expenses)
        liq_score = _clamp(_safe_div(months, 6.0) * 100.0)
        liq_detail = f'Cash covers {months:.1f} months of typical spending.'
    else:
        months = None
        liq_score = 60.0
        liq_detail = 'Not enough spending history to judge the emergency buffer.'
    factors.append({'key': 'liquidity', 'label': 'Emergency liquidity', 'weight': 15,
                    'score': int(round(liq_score)), 'detail': liq_detail})

    # Data quality. A portfolio we cannot see properly cannot be scored
    # properly, and pretending otherwise is how a dashboard lies politely.
    with_basis = sum(p['value'] for p in positions if p.get('cost_basis') is not None)
    coverage = _pct(with_basis, total)
    factors.append({
        'key': 'visibility', 'label': 'Cost-basis coverage', 'weight': 15,
        'score': int(round(coverage)),
        'detail': f'{coverage:.0f}% of the portfolio reports a cost basis, '
                  f'so gains can be computed for that share.',
    })

    for f in factors:
        f['status'] = 'good' if f['score'] >= 70 else 'ok' if f['score'] >= 45 else 'poor'

    score = sum(f['score'] * f['weight'] for f in factors) / 100.0 if base > 0 else 0.0
    key, label = _band(score, _SCORE_BANDS)

    # Recommendations come from the weakest factors, worst first, so the list
    # is always the shortest path to a better score rather than a checklist.
    fixes = {
        'diversification': ('Broaden the portfolio',
                            'Add a broad-market or international fund so no single theme carries the result.'),
        'concentration': ('Trim the largest position',
                          'Bringing the top holding under 20% removes most single-name risk.'),
        'cash': ('Rebalance cash',
                 'Move idle cash toward the target allocation, or top the buffer back up if it is thin.'),
        'liquidity': ('Rebuild the emergency buffer',
                      'Aim for six months of typical spending in cash before adding to the market.'),
        'visibility': ('Fill in cost basis',
                       'Positions without a basis cannot report a gain — add it on manual holdings.'),
    }
    recommendations = []
    for f in sorted(factors, key=lambda x: (x['score'], -x['weight'])):
        if f['score'] >= 70 or f['key'] not in fixes:
            continue
        title, detail = fixes[f['key']]
        recommendations.append({
            'title': title, 'detail': detail, 'factor': f['key'],
            'lift': int(round((100 - f['score']) * f['weight'] / 100.0)),
        })
    return {
        'score': int(round(score)),
        'band': key,
        'label': label,
        'factors': factors,
        'recommendations': recommendations[:3],
        'months_of_cash': round(months, 1) if months else None,
    }


# ══════════════════════════════════════════════════════════════════════════
# Dividend income
# ══════════════════════════════════════════════════════════════════════════

def dividend_forecast(positions):
    """Estimated forward income from the yield reference table.

    ``coverage`` is the share of value whose yield came from a real table
    entry rather than an asset-class default — the difference between a
    figure worth planning around and a rough order of magnitude.
    """
    total = sum(p['value'] for p in positions)
    payers = [p for p in positions if p['income'] > 0]
    annual = sum(p['income'] for p in payers)
    known_value = sum(p['value'] for p in positions if p['yield_known'])

    contributors = sorted(payers, key=lambda p: -p['income'])[:8]
    return {
        'annual': round(annual, 2),
        'monthly': round(annual / 12.0, 2),
        'quarterly': round(annual / 4.0, 2),
        'portfolio_yield': round(_safe_div(annual, total) * 100.0, 2),
        'payers': len(payers),
        'coverage': _pct(known_value, total),
        'contributors': [
            {'ticker': p['ticker'], 'name': p['name'], 'income': p['income'],
             'yield_pct': p['yield_pct'], 'value': p['value'],
             'estimated': not p['yield_known']}
            for p in contributors
        ],
        'basis': 'Estimated from a built-in yield reference, not a live quote.',
    }


# ══════════════════════════════════════════════════════════════════════════
# Projection
# ══════════════════════════════════════════════════════════════════════════

def projection(*, value, years=10, monthly_contribution=0.0,
               annual_return_pct=DEFAULT_RETURN_PCT,
               volatility_pct=None, inflation_pct=DEFAULT_INFLATION_PCT):
    """Modelled portfolio value over time, with an ~80% confidence band.

    Deterministic compounding for the central path; the band is a lognormal
    spread at ±1.28σ scaled by the square root of time — the closed-form
    shape a Monte Carlo run would converge to, without the runtime.

    Everything here is an assumption, and the assumptions ride along in the
    return value so the UI can print them next to the numbers.
    """
    vol = volatility_pct if volatility_pct is not None else DEFAULT_VOLATILITY_PCT
    r = annual_return_pct / 100.0
    sigma = max(0.01, vol / 100.0)
    monthly_r = (1 + r) ** (1 / 12) - 1

    points = []
    balance = float(value)
    contributed = 0.0
    for month in range(1, int(years * 12) + 1):
        balance = balance * (1 + monthly_r) + monthly_contribution
        contributed += monthly_contribution
        if month % 12 and month != int(years * 12):
            continue
        t = month / 12.0
        spread = 1.2816 * sigma * math.sqrt(t)
        points.append({
            'year': round(t, 2),
            'expected': round(balance, 2),
            'low': round(balance * math.exp(-spread), 2),
            'high': round(balance * math.exp(spread), 2),
            'real': round(balance / ((1 + inflation_pct / 100.0) ** t), 2),
            'contributed': round(float(value) + contributed, 2),
        })

    final = points[-1] if points else {'expected': value, 'low': value,
                                       'high': value, 'real': value,
                                       'contributed': value}
    return {
        'points': points,
        'final': final,
        'growth': round(final['expected'] - final['contributed'], 2),
        'assumptions': {
            'annual_return_pct': annual_return_pct,
            'volatility_pct': round(vol, 1),
            'inflation_pct': inflation_pct,
            'monthly_contribution': round(monthly_contribution, 2),
            'years': years,
        },
        'basis': ('Modelled from the assumptions shown — not a forecast of any '
                  'particular market.'),
    }


# ══════════════════════════════════════════════════════════════════════════
# Benchmark reference
# ══════════════════════════════════════════════════════════════════════════

# The app has no market-data feed. Rather than invent index prices, a
# benchmark is drawn as a reference line compounding at a stated long-run
# rate from the same starting value — useful for "am I roughly keeping up",
# explicitly not a real index track. Every consumer must say so.
BENCHMARKS = {
    'sp500':     {'label': 'S&P 500', 'annual_pct': 10.5},
    'nasdaq':    {'label': 'NASDAQ 100', 'annual_pct': 13.5},
    'dow':       {'label': 'Dow Jones', 'annual_pct': 8.5},
    'total':     {'label': 'Total US Market', 'annual_pct': 10.0},
    'balanced':  {'label': '60/40 Balanced', 'annual_pct': 7.5},
}


def benchmark_compare(history, benchmark='sp500', field='total_investments'):
    """Portfolio track against a modelled reference line over the same span.

    Refuses to compare short spans. The first weeks of snapshots are mostly
    onboarding — accounts being linked, balances arriving in stages — so a
    two-week window against a long-run annual rate reports setup churn as
    performance, and does it with a confident-looking headline percentage.
    """
    spec = BENCHMARKS.get(benchmark) or BENCHMARKS['sp500']
    series = _series(history, field)
    if len(series) < 2:
        return {'available': False, 'benchmark': spec['label'],
                'basis': 'Not enough history yet to compare.'}

    span = (series[-1][0] - series[0][0]).days
    if span < MIN_BENCHMARK_DAYS:
        return {
            'available': False,
            'benchmark': spec['label'],
            'span_days': span,
            'basis': (f'Only {span} days of snapshots so far. A comparison against a '
                      f'long-run average needs at least {MIN_BENCHMARK_DAYS} days to '
                      f'mean anything — early readings are mostly accounts being '
                      f'connected, not performance.'),
        }

    start_day, start_value = series[0]
    daily = (1 + spec['annual_pct'] / 100.0) ** (1 / 365.25) - 1

    portfolio, reference = [], []
    for day, value in series:
        elapsed = (day - start_day).days
        portfolio.append({'date': day.strftime('%Y-%m-%d'),
                          'value': round(value, 2),
                          'index': round(_safe_div(value, start_value) * 100.0, 2)})
        ref = start_value * ((1 + daily) ** elapsed)
        reference.append({'date': day.strftime('%Y-%m-%d'),
                          'value': round(ref, 2),
                          'index': round(_safe_div(ref, start_value) * 100.0, 2)})

    port_pct = _safe_div(series[-1][1] - start_value, start_value) * 100.0
    ref_pct = _safe_div(reference[-1]['value'] - start_value, start_value) * 100.0
    return {
        'available': True,
        'benchmark': spec['label'],
        'benchmark_key': benchmark,
        'portfolio': portfolio,
        'reference': reference,
        'portfolio_pct': round(port_pct, 2),
        'benchmark_pct': round(ref_pct, 2),
        'excess_pct': round(port_pct - ref_pct, 2),
        'span_days': (series[-1][0] - start_day).days,
        'basis': (f"Reference line compounds at {spec['annual_pct']}% a year, the "
                  f"long-run average for {spec['label']}. It is a yardstick, not "
                  f"live index data."),
    }


# ══════════════════════════════════════════════════════════════════════════
# Storytelling — what the reader should know before touching a table
# ══════════════════════════════════════════════════════════════════════════

def portfolio_story(*, positions, perf, conc, sector_alloc, region_alloc,
                    dividends, cash, net_worth, today):
    """The four or five sentences that answer "what happened" on arrival."""
    beats = []
    day = perf['windows'].get('day', {})
    total = sum(p['value'] for p in positions)

    if day.get('available') and day.get('change_pct') is not None:
        change, pct = day['change'], day['change_pct']
        direction = 'up' if change >= 0 else 'down'
        beats.append({
            'key': 'today',
            'tone': 'positive' if change >= 0 else 'negative',
            'headline': f"Portfolio {direction} {_money(abs(change))} since the last sync",
            'detail': f"That is {_pct_str(pct)} on {_money(perf['current'])}, "
                      f"measured against the {day['from_date']} snapshot.",
        })
    else:
        beats.append({
            'key': 'today', 'tone': 'neutral',
            'headline': f"Portfolio at {_money(total)}",
            'detail': 'No prior snapshot yet — daily movement appears after the '
                      'next sync writes one.',
        })

    winners = [p for p in positions if (p.get('gain') or 0) > 0]
    losers = [p for p in positions if (p.get('gain') or 0) < 0]
    if winners:
        best = max(winners, key=lambda p: p['gain'])
        beats.append({
            'key': 'best', 'tone': 'positive',
            'headline': f"{best['ticker']} is your biggest winner",
            'detail': f"{_money(best['gain'])} unrealized"
                      + (f" ({_pct_str(best['gain_pct'])})" if best.get('gain_pct') is not None else '')
                      + f" on a {_money(best['value'])} position.",
        })
    if losers:
        worst = min(losers, key=lambda p: p['gain'])
        beats.append({
            'key': 'worst', 'tone': 'negative',
            'headline': f"{worst['ticker']} is furthest underwater",
            'detail': f"{_money(worst['gain'])} unrealized"
                      + (f" ({_pct_str(worst['gain_pct'])})" if worst.get('gain_pct') is not None else '')
                      + '. A loss you have not taken is also a tax-loss candidate.',
        })

    if conc.get('largest') and conc['top3_pct'] > 0:
        single = conc.get('single_name_largest')
        detail = (f"{conc['largest']['ticker']} alone is {conc['largest']['pct']:.0f}%. "
                  f"Looking through funds to the companies inside them, the money "
                  f"behaves like {conc['effective_positions']:.0f} equally sized names")
        detail += (f", the largest being {single['ticker']} at {single['pct']:.0f}%."
                   if single else '.')
        beats.append({
            'key': 'shape', 'tone': 'neutral',
            'headline': f"Your top 3 positions are {conc['top3_pct']:.0f}% of the portfolio",
            'detail': detail,
        })

    placed = [b for b in sector_alloc['buckets']
              if b['label'] not in ('Unclassified', 'Diversified Fund')]
    if placed:
        top = placed[0]
        beats.append({
            'key': 'sector', 'tone': 'neutral',
            'headline': f"{top['label']} is your largest sector at {top['pct']:.0f}%",
            'detail': f"{_money(top['value'])} of the portfolio. Sector labels come from "
                      f"a built-in reference covering {sector_alloc['coverage']:.0f}% "
                      f"of value.",
        })

    intl_pct = sum(b['pct'] for b in region_alloc['buckets']
                   if b['label'] in ('International', 'Global'))
    if region_alloc['coverage'] >= 50:
        beats.append({
            'key': 'geography', 'tone': 'neutral',
            'headline': f'{intl_pct:.0f}% of the portfolio is outside the US',
            'detail': f"Against a {INTERNATIONAL_TARGET_PCT:.0f}% reference. Region "
                      f"labels cover {region_alloc['coverage']:.0f}% of value.",
        })

    if dividends['annual'] > 0:
        beats.append({
            'key': 'income', 'tone': 'positive',
            'headline': f"About {_money(dividends['annual'])} a year in dividends",
            'detail': f"Roughly {_money(dividends['monthly'])} a month at a "
                      f"{dividends['portfolio_yield']:.2f}% portfolio yield — estimated, "
                      f"not a declared figure.",
        })

    return beats


def insights(*, positions, conc, sector_alloc, region_alloc, dividends,
             cash, net_worth, risk, perf, prev_sector_alloc=None):
    """Ranked, explained observations — the wealth-insight feed.

    Ordered critical → warning → info → positive, and within a severity by how
    much money the observation is about. Every item carries a ``why`` so the
    reader is never asked to act on an unexplained instruction.
    """
    items = []
    total = sum(p['value'] for p in positions)

    def add(severity, icon, title, detail, why, action=None, url=None, weight=0.0):
        items.append({'severity': severity, 'icon': icon, 'title': title,
                      'detail': detail, 'why': why, 'action_label': action,
                      'action_url': url, 'weight': weight})

    # ── Concentration ──
    # Judged on single companies. A large index-fund position is a different
    # animal from a large single-stock position, and saying otherwise is the
    # fastest way for a portfolio screen to lose the reader's trust.
    single = conc.get('single_name_largest')
    if single:
        if single['pct'] >= CONCENTRATION_CRIT_PCT:
            add('critical', 'alert',
                f"{single['ticker']} is {single['pct']:.0f}% of your portfolio",
                f"{_money(single['value'])} rides on one company.",
                'A single company above 30% means one company-specific event — an '
                'earnings miss, a lawsuit, a bad quarter — moves your whole net '
                'worth. Diversification is the only free lunch in investing, and '
                'this is where it is being left on the table.',
                'Review holdings', '#holdings', single['value'])
        elif single['pct'] >= CONCENTRATION_WARN_PCT:
            add('warning', 'target',
                f"{single['ticker']} now exceeds {CONCENTRATION_WARN_PCT:.0f}% of the portfolio",
                f"{single['pct']:.0f}% — {_money(single['value'])}.",
                'Past roughly a fifth of the portfolio, one company starts driving '
                'your returns more than your allocation does. Worth a deliberate '
                'decision rather than a drift.',
                'Review holdings', '#holdings', single['value'])

    if conc['single_name_top3_pct'] >= TOP3_WARN_PCT and len(positions) >= 4:
        add('warning', 'chart',
            f"Three companies are {conc['single_name_top3_pct']:.0f}% of the portfolio",
            f"The money behaves like {conc['effective_positions']:.1f} equally sized names.",
            'A long holdings list can hide real concentration. Effective name count '
            'is what actually determines how much single-company risk you carry.',
            weight=total * conc['single_name_top3_pct'] / 100)

    # The reverse case is worth saying out loud, because the raw position
    # table looks alarming and is not: one row can be thousands of companies.
    if conc.get('largest') and conc['largest']['pct'] >= CONCENTRATION_WARN_PCT \
            and conc['diversified_pct'] >= CONCENTRATION_WARN_PCT \
            and (not single or single['pct'] < CONCENTRATION_WARN_PCT):
        add('positive', 'check',
            f"{conc['largest']['ticker']} is {conc['largest']['pct']:.0f}% of the "
            f"portfolio — and that is fine",
            'It is a broad fund, so that weight is spread across its holdings.',
            'Concentration risk is about companies, not rows in a table. A single '
            'index-fund position holds hundreds or thousands of names, so a large '
            'weight in one is diversification rather than a bet.')

    # ── Sector ──
    placed = [b for b in sector_alloc['buckets']
              if b['label'] not in ('Unclassified', 'Diversified Fund', 'Cash & Equivalents')]
    if placed and placed[0]['pct'] >= SECTOR_WARN_PCT:
        top = placed[0]
        add('warning', 'chart',
            f"Overweight {top['label']} at {top['pct']:.0f}%",
            f"{_money(top['value'])} concentrated in one sector.",
            f"Sectors move together. When {top['label'].lower()} has a bad year, "
            f"every name in this sleeve tends to fall at once — the diversification "
            f"across tickers does not protect you from it.",
            weight=top['value'])

    if prev_sector_alloc:
        prev = {b['label']: b['pct'] for b in prev_sector_alloc.get('buckets', [])}
        for b in sector_alloc['buckets']:
            before = prev.get(b['label'])
            if before is None or b['label'] == 'Unclassified':
                continue
            delta = b['pct'] - before
            if abs(delta) >= 8.0:
                add('info', 'trend-up' if delta > 0 else 'trend-down',
                    f"{b['label']} exposure {'rose' if delta > 0 else 'fell'} "
                    f"{abs(delta):.0f} points",
                    f"Now {b['pct']:.0f}% of the portfolio, from {before:.0f}%.",
                    'Allocation drifts on its own as winners grow. Left alone, it '
                    'quietly changes the risk you are taking without a decision.',
                    weight=b['value'])

    # ── Geography ──
    intl_pct = sum(b['pct'] for b in region_alloc['buckets']
                   if b['label'] in ('International', 'Global'))
    if region_alloc['coverage'] >= 50 and intl_pct < 10 and total > 0:
        add('info', 'target',
            f"International exposure is {intl_pct:.0f}%",
            f'Against a {INTERNATIONAL_TARGET_PCT:.0f}% common reference point.',
            'A domestic-only portfolio is a concentrated bet on one economy and one '
            'currency. Adding ex-US exposure has historically reduced volatility '
            'without reducing long-run return.',
            weight=total * 0.1)

    # ── Cash ──
    cash_pct = _pct(cash, net_worth) if net_worth else 0.0
    if cash_pct >= IDLE_CASH_PCT and cash >= IDLE_CASH_MIN_USD:
        add('warning', 'wallet',
            f"{cash_pct:.0f}% of net worth is sitting in cash",
            f'{_money(cash)} is uninvested.',
            'Cash beyond the emergency buffer loses purchasing power to inflation '
            'every year. This is not a call to deploy all of it — just a note that '
            'the balance is currently a decision by default rather than by choice.',
            'See allocation', '#allocation', cash)
    elif 0 < cash_pct < THIN_CASH_PCT:
        add('warning', 'alert',
            f'Only {cash_pct:.0f}% of net worth is liquid',
            f'{_money(cash)} in cash.',
            'A thin buffer means an unexpected expense has to be funded by selling '
            'investments, possibly at a bad moment and with a tax bill attached.',
            weight=cash)

    # ── Risk ──
    if risk['crypto_pct'] >= 15:
        add('warning', 'alert',
            f"Crypto is {risk['crypto_pct']:.0f}% of the balance",
            'Digital assets carry several times equity volatility.',
            'At this weight, crypto rather than your equity allocation is the main '
            'driver of how much the portfolio swings.',
            weight=total * risk['crypto_pct'] / 100)

    # ── Wins ──
    if conc['positions'] >= 8 and conc['single_name_top1_pct'] < CONCENTRATION_WARN_PCT:
        add('positive', 'check', 'Position sizing looks healthy',
            f"{conc['positions']} holdings with no company above "
            f"{conc['single_name_top1_pct']:.0f}%.",
            'No single name can materially damage the portfolio on its own.')

    month = perf['windows'].get('month', {})
    if month.get('available') and (month.get('change_pct') or 0) > 0:
        add('positive', 'trend-up',
            f"Up {_pct_str(month['change_pct'])} over the last 30 days",
            f"{_money(month['change'])} gained since {month['from_date']}.",
            'Measured from your own daily snapshots, not a model.')

    if dividends['annual'] > 0 and dividends['portfolio_yield'] >= 2.0:
        add('positive', 'wallet',
            f"Around {_money(dividends['annual'])} a year in estimated income",
            f"A {dividends['portfolio_yield']:.2f}% portfolio yield.",
            'Dividend income keeps compounding whether or not prices cooperate.')

    order = {'critical': 0, 'warning': 1, 'info': 2, 'positive': 3}
    items.sort(key=lambda i: (order[i['severity']], -i['weight']))
    return items


# ══════════════════════════════════════════════════════════════════════════
# Account rollup
# ══════════════════════════════════════════════════════════════════════════

def account_rollup(positions, accounts, connections=None):
    """Per-account cards: value, positions, connection health, last sync.

    Accounts come from the sync layer; positions are matched by ``account_id``
    where synced and by name where manual, so a hand-entered Fidelity holding
    lands beside the synced one instead of in a phantom account.
    """
    by_conn = {c['id']: c for c in (connections or [])}
    cards = []
    claimed = set()

    for acct in accounts:
        rows = [p for p in positions if p.get('account_id') == acct['id']]
        for p in rows:
            claimed.add(id(p))
        conn = by_conn.get(acct.get('connection_id')) or {}
        value = sum(p['value'] for p in rows)
        cards.append({
            'key': f"acct-{acct['id']}",
            'name': acct['name'],
            'mask': acct.get('mask'),
            'institution': (acct.get('institution') or '').replace('_', ' ').title(),
            'account_type': acct.get('account_type'),
            'balance': round(float(acct.get('balance') or 0.0), 2),
            'holdings_value': round(value, 2),
            'positions': len(rows),
            'status': conn.get('status', 'connected'),
            'last_sync': acct.get('last_synced_at') or conn.get('last_sync_at'),
            'synced': True,
            'tickers': [p['ticker'] for p in rows[:6]],
        })

    manual = {}
    for p in positions:
        if id(p) in claimed:
            continue
        manual.setdefault(p['account'], []).append(p)
    for name, rows in manual.items():
        value = sum(p['value'] for p in rows)
        cards.append({
            'key': f'manual-{name}',
            'name': name,
            'mask': None,
            'institution': 'Entered manually',
            'account_type': 'brokerage',
            'balance': round(value, 2),
            'holdings_value': round(value, 2),
            'positions': len(rows),
            'status': 'manual',
            'last_sync': None,
            'synced': False,
            'tickers': [p['ticker'] for p in rows[:6]],
        })

    cards.sort(key=lambda c: -c['holdings_value'])
    return cards
