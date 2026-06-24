"""Pip math — symbol-aware pip size + price conversions.

Pip conventions used (matches MT5 standard for common brokers):
  XAUUSD (Gold)   1 pip = 0.10 USD  → 150 pips = $15  move
  XAGUSD (Silver) 1 pip = 0.01 USD
  BTCUSD          1 pip = 1.00 USD  → 150 pips = $150 move
  ETHUSD          1 pip = 0.10 USD
  US30/NAS100     1 pip = 1.00
  Major FX pairs  1 pip = 0.0001    → 150 pips = 15 cents on EURUSD
  JPY pairs       1 pip = 0.01      → USDJPY etc.
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


def pip_size(symbol: Optional[str]) -> float:
    """Return the pip size for a symbol."""
    if not symbol:
        return DEFAULT_PIP
    return PIP_SIZE.get(symbol.upper(), DEFAULT_PIP)


def pips_to_price(symbol: Optional[str], pips: float) -> float:
    """Convert N pips into a price distance."""
    return float(pips) * pip_size(symbol)


def price_to_pips(symbol: Optional[str], price_diff: float) -> float:
    """Convert a price distance into pips."""
    ps = pip_size(symbol)
    if ps <= 0:
        return 0.0
    return float(price_diff) / ps


def pip_value_usd_per_lot(symbol: Optional[str], account_type: Optional[str] = None) -> float:
    """USD value of 1 pip per 1.00 lot in the given account's lot convention.

    Standard / demo accounts use the broker's full contract size. Cent and
    microcent accounts use 1/100 and 1/1000 respectively, so the per-lot
    pip value is scaled down accordingly. Used by risk sizing to translate
    a USD risk budget into an MT5 lot quantity.
    """
    sym = (symbol or "").upper()
    base = PIP_VALUE_USD_PER_STANDARD_LOT.get(sym, DEFAULT_PIP_VALUE_USD)
    atype = (account_type or "standard").lower()
    mult = ACCOUNT_TYPE_LOT_MULTIPLIER.get(atype, 1.0)
    return base * mult
