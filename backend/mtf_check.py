"""Multi-Timeframe (MTF) trend confirmation gate.

A candidate BUY/SELL signal must agree with the dominant higher-timeframe
trend before passing. Trades aligned with the broader trend show materially
higher expectancy than counter-trend trades.

Two paths are supported:

1. **Tier-based** (preferred, iter-67+) — consumes the `mtf_tiers` pack
   produced by `mtf_tiers.compute_mtf_tiers(history)`. The gate looks at the
   per-tier direction (SHORT / MEDIUM / LONG) and the aggregated alignment
   vote. The gate is permissive on weak chop (one tier disagreeing is fine)
   but strict in clearly counter-trend setups (≥2 tiers disagree with the
   signal direction).

2. **Legacy slope-on-daily** — kept as a safety fallback if `mtf_tiers` is
   not supplied or has insufficient history. Same 2-of-3 majority rule
   (SMA20 slope · SMA50 vs SMA200 · price vs SMA50).
"""
from typing import Optional

from mtf_tiers import compute_mtf_tiers


def _slope(values: list, lookback: int = 5) -> Optional[float]:
    if not values or len(values) < lookback + 1:
        return None
    head = values[-(lookback + 1)]
    tail = values[-1]
    if head is None or tail is None or head == 0:
        return None
    return (tail - head) / head


def _direction_label(d: Optional[float]) -> str:
    if d is None:
        return "FLAT"
    if d > 0.001:
        return "UP"
    if d < -0.001:
        return "DOWN"
    return "FLAT"


def _legacy_gate(action: str, history: list, indicators: dict) -> dict:
    """Legacy 3-vote gate on the daily series only — used when tiered data
    is unavailable (cold start, insufficient bars)."""
    if not history or len(history) < 60 or action not in ("BUY", "SELL"):
        return {
            "aligned": True, "htf_trend": "FLAT",
            "votes": {"sma20_slope": "FLAT", "sma50_vs_200": "FLAT", "price_vs_sma50": "FLAT"},
            "reason": "", "checked": False, "mode": "legacy",
        }

    closes = [c["close"] for c in history if c.get("close") is not None]
    if len(closes) < 60:
        return {
            "aligned": True, "htf_trend": "FLAT",
            "votes": {"sma20_slope": "FLAT", "sma50_vs_200": "FLAT", "price_vs_sma50": "FLAT"},
            "reason": "", "checked": False, "mode": "legacy",
        }

    def _sma_series(period: int, length: int = 6) -> list:
        out = []
        for i in range(len(closes) - length, len(closes)):
            if i + 1 < period:
                out.append(None)
                continue
            window = closes[i + 1 - period : i + 1]
            out.append(sum(window) / period)
        return out

    sma20_series = _sma_series(20, length=6)
    sma20_slope_d = _slope(sma20_series, lookback=5)
    sma20_dir = _direction_label(sma20_slope_d)

    sma50 = indicators.get("sma_50")
    sma200 = indicators.get("sma_200")
    if sma50 is not None and sma200 is not None and sma200 > 0:
        sma50_vs_200_dir = "UP" if sma50 > sma200 else ("DOWN" if sma50 < sma200 else "FLAT")
    else:
        sma50_vs_200_dir = "FLAT"

    current_price = indicators.get("current_price") or closes[-1]
    if sma50 is not None and current_price > 0:
        price_vs_sma50_dir = "UP" if current_price > sma50 else ("DOWN" if current_price < sma50 else "FLAT")
    else:
        price_vs_sma50_dir = "FLAT"

    votes = {
        "sma20_slope": sma20_dir,
        "sma50_vs_200": sma50_vs_200_dir,
        "price_vs_sma50": price_vs_sma50_dir,
    }
    up_votes = sum(1 for v in votes.values() if v == "UP")
    down_votes = sum(1 for v in votes.values() if v == "DOWN")
    htf_trend = "UP" if up_votes > down_votes else ("DOWN" if down_votes > up_votes else "FLAT")

    aligned = True
    reason = ""
    if action == "BUY" and down_votes >= 2:
        aligned = False
        reason = f"H-TF trend is DOWN ({down_votes}/3 checks bearish). BUY counter-trend — vetoed by MTF gate."
    elif action == "SELL" and up_votes >= 2:
        aligned = False
        reason = f"H-TF trend is UP ({up_votes}/3 checks bullish). SELL counter-trend — vetoed by MTF gate."

    return {
        "aligned": aligned, "htf_trend": htf_trend,
        "votes": votes, "reason": reason,
        "checked": True, "mode": "legacy",
    }


def _tier_gate(action: str, mtf_tiers: dict) -> dict:
    """Tier-based gate (preferred). Uses the SHORT/MEDIUM/LONG direction
    votes from `mtf_tiers.compute_mtf_tiers`."""
    if action not in ("BUY", "SELL"):
        return {
            "aligned": True, "htf_trend": "FLAT",
            "votes": {}, "reason": "", "checked": False, "mode": "tiered",
            "tier_directions": {},
        }

    alignment = mtf_tiers.get("alignment") or {}
    up_votes = int(alignment.get("buy_support") or 0)
    down_votes = int(alignment.get("sell_support") or 0)

    tier_dirs = {
        name: (mtf_tiers.get(name) or {}).get("direction", "FLAT")
        for name in ("SHORT", "MEDIUM", "LONG")
    }

    htf_trend = "UP" if up_votes > down_votes else ("DOWN" if down_votes > up_votes else "FLAT")

    aligned = True
    reason = ""
    if action == "BUY" and down_votes >= 2:
        bearish_tiers = [n for n, d in tier_dirs.items() if d == "DOWN"]
        aligned = False
        reason = (
            f"MTF tier check: {down_votes}/3 tiers bearish "
            f"({', '.join(bearish_tiers)}). BUY counter-trend — vetoed."
        )
    elif action == "SELL" and up_votes >= 2:
        bullish_tiers = [n for n, d in tier_dirs.items() if d == "UP"]
        aligned = False
        reason = (
            f"MTF tier check: {up_votes}/3 tiers bullish "
            f"({', '.join(bullish_tiers)}). SELL counter-trend — vetoed."
        )

    return {
        "aligned": aligned,
        "htf_trend": htf_trend,
        "votes": {
            "short": tier_dirs["SHORT"],
            "medium": tier_dirs["MEDIUM"],
            "long": tier_dirs["LONG"],
        },
        "tier_directions": tier_dirs,
        "reason": reason,
        "checked": True,
        "mode": "tiered",
        "buy_support": up_votes,
        "sell_support": down_votes,
    }


def multi_timeframe_gate(action: str, history: list, indicators: dict,
                         mtf_tiers: Optional[dict] = None) -> dict:
    """Return MTF gate decision. Prefer tiered analysis when available.

    Returns dict with: aligned (bool), htf_trend (UP/DOWN/FLAT), votes,
    reason, checked, mode ("tiered"|"legacy").
    """
    # If caller didn't pass tiers, try to derive them from history.
    if mtf_tiers is None:
        try:
            mtf_tiers = compute_mtf_tiers(history)
        except Exception:
            mtf_tiers = None

    # Use tiered path only when MEDIUM tier has enough data.
    if mtf_tiers and mtf_tiers.get("ready"):
        return _tier_gate(action, mtf_tiers)

    # Fall back to legacy 3-vote on daily series.
    return _legacy_gate(action, history, indicators)
