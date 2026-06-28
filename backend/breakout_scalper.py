"""Breakout Scalper feature (iter-69).

Donchian-20 channel break + ATR confirmation. Returns a structured feature
dict the AI prompt can consume to recognise momentum continuation setups:

    {
      "signal": "BUY" | "SELL" | "NONE",
      "channel_high": float,   # 20-bar Donchian upper
      "channel_low":  float,   # 20-bar Donchian lower
      "channel_width_pct": float,
      "atr": float,
      "break_distance_atr": float,   # how far current close pushed beyond the band, in ATR units
      "ready": bool,
    }

Rules:
    BUY  triggers when close > channel_high (prior 20-bar) AND break magnitude ≥ 0.25 ATR.
    SELL triggers when close < channel_low  (prior 20-bar) AND break magnitude ≥ 0.25 ATR.

We require the 20-bar window to EXCLUDE the current bar so the break is
measured against established structure, not the bar that just printed.
"""
from __future__ import annotations
from typing import Optional

DONCHIAN_PERIOD = 20
ATR_PERIOD = 14
MIN_BREAK_ATR = 0.25


def _atr(history: list, period: int = ATR_PERIOD) -> Optional[float]:
    """Average True Range over the last `period` bars (excluding current bar)."""
    if len(history) < period + 1:
        return None
    trs: list[float] = []
    # Use the last `period` bars BEFORE the current one for ATR baseline.
    for i in range(len(history) - period - 1, len(history) - 1):
        if i <= 0:
            continue
        h = history[i]["high"]
        l = history[i]["low"]
        prev_c = history[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs) if trs else None


def compute_breakout_scalper(history: list) -> dict:
    """Return the Breakout Scalper feature dict for the latest bar."""
    empty = {
        "signal": "NONE",
        "channel_high": None, "channel_low": None,
        "channel_width_pct": None, "atr": None,
        "break_distance_atr": 0.0,
        "ready": False,
    }
    if not history or len(history) < DONCHIAN_PERIOD + 2:
        return empty

    # Donchian channel from the prior 20 bars — exclude the bar we're testing.
    window = history[-(DONCHIAN_PERIOD + 1):-1]
    if len(window) < DONCHIAN_PERIOD:
        return empty
    ch_high = max(b["high"] for b in window)
    ch_low = min(b["low"] for b in window)
    current = history[-1]["close"]

    atr = _atr(history)
    if atr is None or atr <= 0:
        return {**empty, "channel_high": round(ch_high, 5),
                "channel_low": round(ch_low, 5)}

    signal = "NONE"
    break_distance = 0.0
    if current > ch_high:
        break_distance = (current - ch_high) / atr
        if break_distance >= MIN_BREAK_ATR:
            signal = "BUY"
    elif current < ch_low:
        break_distance = (ch_low - current) / atr
        if break_distance >= MIN_BREAK_ATR:
            signal = "SELL"

    channel_width_pct = ((ch_high - ch_low) / current * 100) if current > 0 else None

    return {
        "signal": signal,
        "channel_high": round(ch_high, 5),
        "channel_low": round(ch_low, 5),
        "channel_width_pct": round(channel_width_pct, 3) if channel_width_pct is not None else None,
        "atr": round(atr, 5),
        "break_distance_atr": round(float(break_distance), 3),
        "ready": True,
    }
