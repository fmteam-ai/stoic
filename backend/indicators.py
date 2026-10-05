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
    """Wilder RSI (fix plan A14): seeded with the simple average of the first
    `period` changes, then smoothed with alpha = 1/period over the whole series."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(d for d in deltas[:period] if d > 0) / period
    avg_l = sum(-d for d in deltas[:period] if d < 0) / period
    for d in deltas[period:]:
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def atr(history: list, period: int = 14) -> Optional[float]:
    """Wilder ATR over daily candles (fix plan A7 — atr_14 was never produced)."""
    if len(history) < period + 1:
        return None
    trs = []
    for i in range(1, len(history)):
        h, lo, pc = float(history[i]["high"]), float(history[i]["low"]), float(history[i - 1]["close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


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
        # fix plan A7 — names the confluence / VaR / microstructure layers read
        "ma_20": _round(sma(closes, 20)),
        "ma_200": _round(sma(closes, min(200, n))),
        "ma_200_full": n >= 200,
        "atr_14": _round(atr(history, 14)),
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
