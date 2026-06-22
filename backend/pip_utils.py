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
