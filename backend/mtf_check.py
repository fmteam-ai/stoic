"""Multi-Timeframe (MTF) trend confirmation gate.

Given a candidate signal (BUY/SELL) on the primary timeframe (here Claude's
analysis of the daily indicator snapshot), we require the *higher* timeframe
trend to agree before letting the trade through. This is a well-known
edge-preserver: trades aligned with the dominant trend have materially higher
expectancy than counter-trend trades.

Since our data feed is daily candles, the "higher timeframe" is approximated
with two confluence checks on the same series:

  1. **SMA20 slope** (≈ short-trend / "H4 proxy"):
        slope_short > 0  → uptrend
        slope_short < 0  → downtrend

  2. **SMA50 vs SMA200** (≈ medium-trend / "D1 proxy"):
        SMA50 > SMA200    → structural uptrend
        SMA50 < SMA200    → structural downtrend

  3. **Current price vs SMA50** as a final tie-breaker.

A BUY is GATED OUT (vetoed) when *any two of three* checks disagree with the
signal direction. SELL is gated symmetrically. This makes the filter
permissive on weak chop (one disagreement is fine) but strict in clearly
counter-trend setups (everything pointing the other way).
"""
from typing import Optional


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


def multi_timeframe_gate(action: str, history: list, indicators: dict) -> dict:
    """Return a dict with: aligned (bool), htf_trend (UP/DOWN/FLAT), votes, reason."""
    if not history or len(history) < 60 or action not in ("BUY", "SELL"):
        # Not enough data, or HOLD: filter is a no-op pass.
        return {
            "aligned": True,
            "htf_trend": "FLAT",
            "votes": {"sma20_slope": "FLAT", "sma50_vs_200": "FLAT", "price_vs_sma50": "FLAT"},
            "reason": "",
            "checked": False,
        }

    closes = [c["close"] for c in history if c.get("close") is not None]
    if len(closes) < 60:
        return {
            "aligned": True,
            "htf_trend": "FLAT",
            "votes": {"sma20_slope": "FLAT", "sma50_vs_200": "FLAT", "price_vs_sma50": "FLAT"},
            "reason": "",
            "checked": False,
        }

    # Build SMA series (last N points) needed for slope
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

    # Tally: BUY needs majority UP; SELL needs majority DOWN.
    up_votes = sum(1 for v in votes.values() if v == "UP")
    down_votes = sum(1 for v in votes.values() if v == "DOWN")

    if up_votes > down_votes:
        htf_trend = "UP"
    elif down_votes > up_votes:
        htf_trend = "DOWN"
    else:
        htf_trend = "FLAT"

    aligned = True
    reason = ""
    if action == "BUY" and down_votes >= 2:
        aligned = False
        reason = (
            f"H-TF trend is DOWN ({down_votes}/3 checks bearish). "
            f"BUY counter-trend — vetoed by MTF gate."
        )
    elif action == "SELL" and up_votes >= 2:
        aligned = False
        reason = (
            f"H-TF trend is UP ({up_votes}/3 checks bullish). "
            f"SELL counter-trend — vetoed by MTF gate."
        )

    return {
        "aligned": aligned,
        "htf_trend": htf_trend,
        "votes": votes,
        "reason": reason,
        "checked": True,
    }
