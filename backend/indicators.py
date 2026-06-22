"""Technical indicator calculations for market history.

Pure functions over a list of OHLCV dicts (one per day):
    {"date": "YYYY-MM-DD", "open": float, "high": float, "low": float, "close": float, "volume": float}
"""
from typing import Optional


def sma(closes: list, period: int) -> Optional[float]:
    """Simple moving average over the last `period` closes."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ema(closes: list, period: int) -> Optional[float]:
    """Exponential moving average."""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    e = sum(closes[:period]) / period
    for px in closes[period:]:
        e = px * k + e * (1 - k)
    return e


def rsi(closes: list, period: int = 14) -> Optional[float]:
    """Relative Strength Index over the last `period` closes."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-(period + 1) + i] - closes[-(period + 1) + i - 1]
        (gains if d > 0 else losses).append(abs(d))
    avg_g = sum(gains) / period if gains else 0
    avg_l = sum(losses) / period if losses else 0
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def volatility(closes: list, period: int = 30) -> float:
    """Stdev of daily returns over last `period` closes, returned as percent."""
    n = len(closes)
    if n < period:
        return 0.0
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(n - period, n) if closes[i - 1]
    ]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return (var ** 0.5) * 100


def _round(v, ndigits=5):
    return round(v, ndigits) if v is not None else None


def compute_indicators(history: list) -> dict:
    """Compute lightweight TA indicators from daily candles."""
    if not history or len(history) < 30:
        return {}

    closes = [c["close"] for c in history]
    n = len(closes)
    current = closes[-1]
    high180 = max(c["high"] for c in history)
    low180 = min(c["low"] for c in history)
    pct_from_high = ((current - high180) / high180) * 100 if high180 else 0
    pct_from_low = ((current - low180) / low180) * 100 if low180 else 0
    six_month_return = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0

    return {
        "current_price": _round(current),
        "sma_20": _round(sma(closes, 20)),
        "sma_50": _round(sma(closes, 50)),
        "sma_200": _round(sma(closes, min(200, n))),
        "ema_12": _round(ema(closes, 12)),
        "ema_26": _round(ema(closes, 26)),
        "rsi_14": _round(rsi(closes, 14), 2),
        "high_180d": _round(high180),
        "low_180d": _round(low180),
        "pct_from_high": round(pct_from_high, 2),
        "pct_from_low": round(pct_from_low, 2),
        "six_month_return_pct": round(six_month_return, 2),
        "volatility_30d_pct": round(volatility(closes, 30), 2),
    }
