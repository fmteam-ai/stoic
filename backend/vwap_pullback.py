"""VWAP-style pullback feature (iter-69).

True VWAP requires intraday tick volume — we only have daily OHLCV here.
We approximate intraday "fair value" with a rolling N-bar
typical-price weighted by volume:

    typical_price = (high + low + close) / 3
    vwap_proxy_N   = Σ(typical * volume) / Σ(volume)

Returns:

    {
      "vwap": float,                # rolling VWAP proxy
      "pullback_pct": float,        # (current - vwap) / vwap * 100
      "above_vwap": bool,           # close > vwap
      "regime": "near"/"above_extended"/"below_extended",
      "pullback_signal": "BUY"|"SELL"|"NONE",
      "ready": bool,
    }

Trading hint surfaced to the LLM:
    - Trend is UP and price has pulled back to within ±0.3% of VWAP → BUY pullback.
    - Trend is DOWN and price has rallied to within ±0.3% of VWAP → SELL pullback.
    - Price > vwap by > 1.5% → extended above (mean-reversion risk).
"""
from __future__ import annotations

VWAP_PERIOD = 20
NEAR_VWAP_PCT = 0.30      # within ±0.3% counts as a pullback to fair value
EXTENDED_PCT = 1.50       # > ±1.5% counts as extended


def compute_vwap_pullback(history: list, htf_trend: str = "FLAT") -> dict:
    """Build VWAP-proxy feature dict.

    `htf_trend` should be "UP" / "DOWN" / "FLAT" — typically passed from
    the multi-timeframe pack so we can label "pullback in uptrend" vs
    "pullback in downtrend" cleanly.
    """
    empty = {
        "vwap": None, "pullback_pct": None,
        "above_vwap": None, "regime": "unknown",
        "pullback_signal": "NONE", "ready": False,
    }
    if not history or len(history) < VWAP_PERIOD:
        return empty

    window = history[-VWAP_PERIOD:]
    sum_vol = sum((b.get("volume") or 0) for b in window)
    if sum_vol <= 0:
        # Volume missing — fall back to unweighted typical-price SMA.
        typicals = [(b["high"] + b["low"] + b["close"]) / 3.0 for b in window]
        vwap = sum(typicals) / len(typicals)
    else:
        vwap = sum(
            ((b["high"] + b["low"] + b["close"]) / 3.0) * (b.get("volume") or 0)
            for b in window
        ) / sum_vol

    current = history[-1]["close"]
    if vwap <= 0:
        return empty
    pullback_pct = (current - vwap) / vwap * 100.0
    above_vwap = current > vwap

    abs_pull = abs(pullback_pct)
    if abs_pull <= NEAR_VWAP_PCT:
        regime = "near"
    elif pullback_pct > EXTENDED_PCT:
        regime = "above_extended"
    elif pullback_pct < -EXTENDED_PCT:
        regime = "below_extended"
    elif pullback_pct > 0:
        regime = "above"
    else:
        regime = "below"

    pullback_signal = "NONE"
    if regime == "near":
        if htf_trend == "UP":
            pullback_signal = "BUY"   # trend up + price back to VWAP = entry
        elif htf_trend == "DOWN":
            pullback_signal = "SELL"

    return {
        "vwap": round(vwap, 5),
        "pullback_pct": round(pullback_pct, 3),
        "above_vwap": bool(above_vwap),
        "regime": regime,
        "pullback_signal": pullback_signal,
        "ready": True,
    }
