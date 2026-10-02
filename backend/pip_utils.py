"""Pip math — symbol-aware pip size + price conversions.

Pip conventions used (matches MT5 standard for common brokers):
  XAUUSD (Gold)   1 pip = 0.10 USD  → 150 pips = $15  move
  XAGUSD (Silver) 1 pip = 0.01 USD
  BTCUSD          1 pip = 1.00 USD  → 150 pips = $150 move
  ETHUSD          1 pip = 0.10 USD
  US30/NAS100     1 pip = 1.00
  Major FX pairs  1 pip = 0.0001    → 150 pips = 15 cents on EURUSD
  JPY pairs       1 pip = 0.01      → any *JPY pair (USDJPY, CHFJPY, ...)
"""
from typing import Optional


PIP_SIZE = {
    "XAUUSD": 0.10,
    "XAGUSD": 0.01,
    "BTCUSD": 1.00,
    "ETHUSD": 0.10,
    "US30": 1.00,
    "NAS100": 1.00,
    "SPX500": 0.10,
    "GER40": 1.00,
    "UK100": 1.00,
    "USDJPY": 0.01,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "AUDJPY": 0.01,
    # Default below is for 4-digit FX majors
}
DEFAULT_PIP = 0.0001

# USD value of 1 pip per 1.00 STANDARD lot, by symbol. Used by risk sizing to
# translate a USD risk budget into an MT5 lot quantity.
# Contract spec convention:
#   XAUUSD  100 oz × $0.10/oz pip = $10/lot/pip
#   XAGUSD  5000 oz × $0.01/oz pip = $50/lot/pip
#   BTCUSD  1 BTC × $1.00 pip = $1/lot/pip
#   FX majors 100,000 × 0.0001 = $10/lot/pip
#   JPY pairs ~$6.50/lot/pip (USDJPY mid-rate approx)
PIP_VALUE_USD_PER_STANDARD_LOT = {
    "XAUUSD": 10.0,
    "XAGUSD": 50.0,
    "BTCUSD": 1.0,
    "ETHUSD": 0.10,
    "US30": 1.0,
    "NAS100": 1.0,
    "SPX500": 1.0,
    "GER40": 1.0,
    "UK100": 1.0,
    "USDJPY": 6.50,
    "EURJPY": 6.50,
    "GBPJPY": 6.50,
    "AUDJPY": 6.50,
}
DEFAULT_PIP_VALUE_USD = 10.0  # FX majors default

# Lot-size multiplier vs standard, by broker account type. Microcent/cent
# accounts move the decimal so a "1 lot" trade risks much less USD.
ACCOUNT_TYPE_LOT_MULTIPLIER = {
    "standard":  1.0,
    "demo":      1.0,    # demo behaves like standard
    "cent":      0.01,   # 1 cent lot = 0.01 standard
    "microcent": 0.001,  # 1 microcent lot = 0.001 standard
}

# FX-major bases NOT in PIP_SIZE (they use DEFAULT_PIP) — needed so broker
# suffixed tickers (EURUSD#, GBPUSD.r) still resolve to the STOIC base.
FX_MAJOR_BASES = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    "EURGBP", "EURCHF", "EURAUD", "EURNZD", "EURCAD",
    "GBPCHF", "GBPAUD", "GBPNZD", "GBPCAD",
    "AUDNZD", "AUDCAD", "AUDCHF", "NZDCAD", "NZDCHF", "CADCHF",
)

# Broker-alias bases (GOLD# → XAUUSD etc.) — mirrors broker_symbol_detector.
SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "DOW": "US30",
    "NAS": "NAS100",
    "DAX": "GER40",
}


def base_symbol(symbol: Optional[str]) -> str:
    """Resolve broker-suffixed/aliased tickers (XAUUSD.fx, GOLD#, XAUUSD-ECN)
    to the STOIC base symbol. Without this, pip math on a suffixed gold
    symbol fell back to the 0.0001 FX pip — a $1 move became "10000 pips"."""
    if not symbol:
        return ""
    s = symbol.upper()
    for known in PIP_SIZE:
        if s.startswith(known):
            return known
    for alias, base in SYMBOL_ALIASES.items():
        if s.startswith(alias):
            return base
    for fx in FX_MAJOR_BASES:
        if s.startswith(fx):
            return fx
    return s


_FX_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SGD",
           "SEK", "NOK", "ZAR", "MXN", "PLN", "TRY", "HKD", "CNH", "DKK"}


def _fx_pair(symbol: Optional[str]) -> Optional[str]:
    """The 6-letter FX pair a (possibly broker-suffixed) ticker starts with,
    e.g. CHFJPY-ECN → CHFJPY; None for non-FX symbols."""
    s = (symbol or "").upper()
    p = s[:6]
    if len(p) == 6 and p[:3] in _FX_CCY and p[3:] in _FX_CCY and p[:3] != p[3:]:
        return p
    return None


def pip_size(symbol: Optional[str]) -> float:
    """Return the pip size for a symbol (broker-suffix aware).

    Any *JPY FX pair uses 0.01 (CHFJPY/CADJPY/NZDJPY were falling back to
    the 0.0001 default — a 100× pip-count error)."""
    if not symbol:
        return DEFAULT_PIP
    base = base_symbol(symbol)
    if base in PIP_SIZE:
        return PIP_SIZE[base]
    fx = _fx_pair(base)
    if fx and fx.endswith("JPY"):
        return 0.01
    return DEFAULT_PIP


def pips_to_price(symbol: Optional[str], pips: float) -> float:
    """Convert N pips into a price distance."""
    return float(pips) * pip_size(symbol)


def price_to_pips(symbol: Optional[str], price_diff: float) -> float:
    """Convert a price distance into pips."""
    ps = pip_size(symbol)
    if ps <= 0:
        return 0.0
    return float(price_diff) / ps


def pip_value_usd_per_lot_strict(symbol: Optional[str]) -> Optional[float]:
    """Round 10 item 3 — AUTHORITATIVE per-lot pip value or None.

    Returns a value only when the symbol is in the explicit contract table
    or is a USD-quoted FX major (standard $10/pip/lot). Cross pairs, exotic
    contracts and unknown symbols return None so risk restoration can mark
    exposure UNKNOWN and fail closed instead of silently assuming $10."""
    base = base_symbol(symbol)
    if base in PIP_VALUE_USD_PER_STANDARD_LOT:
        return PIP_VALUE_USD_PER_STANDARD_LOT[base]
    if len(base) == 6 and base.endswith("USD") and base.isalpha():
        return DEFAULT_PIP_VALUE_USD
    return None


def pip_value_usd_per_lot(symbol: Optional[str], account_type: Optional[str] = None,
                          price: Optional[float] = None) -> float:
    """USD value of 1 pip per 1.00 lot in the given account's lot convention.

    Standard / demo accounts use the broker's full contract size. Cent and
    microcent accounts use 1/100 and 1/1000 respectively, so the per-lot
    pip value is scaled down accordingly. Used by risk sizing to translate
    a USD risk budget into an MT5 lot quantity.

    `price` (optional): the pair's current rate. For USD-BASE FX pairs
    (USDJPY/USDCHF/USDCAD) the exact standard-lot pip value is
    100,000 × pip_size / price (e.g. USDCHF @0.80 → $12.50, not $10).
    Without a price the static table / $10 default is used. Non-USD
    crosses still use the static approximation (no conversion rate here).
    """
    sym = (symbol or "").upper()
    b = base_symbol(sym)
    base = PIP_VALUE_USD_PER_STANDARD_LOT.get(b, DEFAULT_PIP_VALUE_USD)
    fx = _fx_pair(b)
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        px = 0.0
    if fx and fx.startswith("USD") and px > 0:
        base = 100_000.0 * pip_size(fx) / px
    atype = (account_type or "standard").lower()
    mult = ACCOUNT_TYPE_LOT_MULTIPLIER.get(atype, 1.0)
    return base * mult
