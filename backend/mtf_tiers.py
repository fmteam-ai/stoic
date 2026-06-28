"""Multi-Timeframe (MTF) tier indicator pack.

Computes structured indicator snapshots at three lookback tiers from the
same daily OHLC series, giving the AI (and the MTF veto gate) an explicit
multi-timeframe view to reason about:

    SHORT  (≈ H4 / intraday proxy) — last 5-10 daily bars
    MEDIUM (≈ D1)                    — last 20-50 daily bars
    LONG   (≈ W1 / structural)       — last 50-200 daily bars

Each tier exposes:
    - sma_fast / sma_slow            (trend baseline)
    - sma_fast_slope_pct             (direction + magnitude)
    - rsi                            (momentum)
    - close_vs_sma_fast_pct          (extension)
    - direction ∈ {"UP","DOWN","FLAT"}

Plus an `alignment` summary that tallies tier votes and reports per-action
support so the gate and the prompt can both consume the same structure.
"""
from __future__ import annotations
from typing import Optional

from indicators import sma, rsi


_TIER_CONFIG = {
    # name: (fast_period, slow_period, slope_lookback_bars, rsi_period)
    "SHORT": (5, 10, 3, 7),       # last week — intraday/H4 proxy
    "MEDIUM": (20, 50, 5, 14),    # last month — D1
    "LONG": (50, 200, 10, 21),    # last quarter+ — W1/structural
}


def _slope_pct(values: list, lookback: int) -> Optional[float]:
    """Percent change of `values[-1]` vs `values[-1-lookback]`."""
    if not values or len(values) < lookback + 1:
        return None
    head = values[-(lookback + 1)]
    tail = values[-1]
    if head is None or tail is None or head == 0:
        return None
    return ((tail - head) / head) * 100.0


def _direction(slope_pct: Optional[float], threshold: float = 0.10) -> str:
    """Classify direction. threshold expressed in percent."""
    if slope_pct is None:
        return "FLAT"
    if slope_pct > threshold:
        return "UP"
    if slope_pct < -threshold:
        return "DOWN"
    return "FLAT"


def _sma_series(closes: list, period: int, tail_len: int) -> list:
    """Last `tail_len` SMA(period) values for slope computation."""
    out: list = []
    n = len(closes)
    start = max(0, n - tail_len)
    for i in range(start, n):
        if i + 1 < period:
            out.append(None)
            continue
        window = closes[i + 1 - period : i + 1]
        out.append(sum(window) / period)
    return out


def _build_tier(closes: list, fast_p: int, slow_p: int,
                slope_lookback: int, rsi_p: int) -> dict:
    """Compute one tier snapshot. Returns FLAT-safe dict on insufficient data."""
    if len(closes) < max(slow_p, rsi_p + 1):
        return {
            "sma_fast": None,
            "sma_slow": None,
            "sma_fast_slope_pct": None,
            "rsi": None,
            "close_vs_sma_fast_pct": None,
            "direction": "FLAT",
            "ready": False,
        }

    sma_fast_val = sma(closes, fast_p)
    sma_slow_val = sma(closes, slow_p)
    rsi_val = rsi(closes, rsi_p)
    current = closes[-1]

    fast_series = _sma_series(closes, fast_p, slope_lookback + 1)
    fast_slope_pct = _slope_pct(fast_series, slope_lookback)

    # Direction logic:
    #   primary signal — sma_fast slope (momentum)
    #   confirmation   — sma_fast vs sma_slow (structure)
    # Both must agree for non-FLAT.
    slope_dir = _direction(fast_slope_pct, threshold=0.05)
    if sma_fast_val is not None and sma_slow_val is not None and sma_slow_val > 0:
        structure_dir = "UP" if sma_fast_val > sma_slow_val else (
            "DOWN" if sma_fast_val < sma_slow_val else "FLAT"
        )
    else:
        structure_dir = "FLAT"

    if slope_dir == "UP" and structure_dir == "UP":
        direction = "UP"
    elif slope_dir == "DOWN" and structure_dir == "DOWN":
        direction = "DOWN"
    elif slope_dir == "FLAT" and structure_dir in ("UP", "DOWN"):
        direction = structure_dir  # structure breaks the tie when momentum is quiet
    else:
        direction = "FLAT"

    close_vs_fast_pct = None
    if sma_fast_val and sma_fast_val > 0:
        close_vs_fast_pct = round(((current - sma_fast_val) / sma_fast_val) * 100, 3)

    return {
        "sma_fast": round(sma_fast_val, 5) if sma_fast_val is not None else None,
        "sma_slow": round(sma_slow_val, 5) if sma_slow_val is not None else None,
        "sma_fast_slope_pct": round(fast_slope_pct, 4) if fast_slope_pct is not None else None,
        "rsi": round(rsi_val, 2) if rsi_val is not None else None,
        "close_vs_sma_fast_pct": close_vs_fast_pct,
        "direction": direction,
        "ready": True,
    }


def compute_mtf_tiers(history: list) -> dict:
    """Compute the three-tier MTF indicator pack from daily OHLC history.

    Returns a dict shaped like:
        {
            "SHORT":  {... tier dict ...},
            "MEDIUM": {... tier dict ...},
            "LONG":   {... tier dict ...},
            "alignment": {
                "buy_support": 0-3,
                "sell_support": 0-3,
                "dominant": "UP"|"DOWN"|"MIXED",
                "all_aligned_up": bool,
                "all_aligned_down": bool,
            },
            "ready": bool,   # True iff at least the MEDIUM tier has data
        }
    """
    if not history:
        return _empty_tiers()
    closes = [c["close"] for c in history if c.get("close") is not None]
    if len(closes) < 30:
        return _empty_tiers()

    tiers: dict = {}
    for name, (fast_p, slow_p, slope_lb, rsi_p) in _TIER_CONFIG.items():
        tiers[name] = _build_tier(closes, fast_p, slow_p, slope_lb, rsi_p)

    up_votes = sum(1 for t in tiers.values() if t["direction"] == "UP")
    down_votes = sum(1 for t in tiers.values() if t["direction"] == "DOWN")

    if up_votes > down_votes:
        dominant = "UP"
    elif down_votes > up_votes:
        dominant = "DOWN"
    else:
        dominant = "MIXED"

    tiers["alignment"] = {
        "buy_support": up_votes,
        "sell_support": down_votes,
        "dominant": dominant,
        "all_aligned_up": up_votes == 3,
        "all_aligned_down": down_votes == 3,
    }
    tiers["ready"] = tiers["MEDIUM"]["ready"]
    return tiers


def _empty_tiers() -> dict:
    empty = {
        "sma_fast": None, "sma_slow": None,
        "sma_fast_slope_pct": None, "rsi": None,
        "close_vs_sma_fast_pct": None,
        "direction": "FLAT", "ready": False,
    }
    return {
        "SHORT": dict(empty),
        "MEDIUM": dict(empty),
        "LONG": dict(empty),
        "alignment": {
            "buy_support": 0, "sell_support": 0,
            "dominant": "MIXED",
            "all_aligned_up": False, "all_aligned_down": False,
        },
        "ready": False,
    }
