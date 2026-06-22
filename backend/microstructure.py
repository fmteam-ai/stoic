"""Market microstructure helpers — session detection + regime classification.

Session detection lets the AI know if XAUUSD is trading in low-volume Tokyo
hours (mean-reversion bias) or London/NY overlap (trend bias), and whether
BTCUSD is in weekend-thin-liquidity mode.

Regime classification labels the current market state from indicators so the
AI can adapt its strategy (trend / range / high-vol-chop) instead of using one
model for every environment.
"""
from datetime import datetime, timezone

# UTC trading session ranges
SESSIONS = {
    "tokyo":  (0, 9),   # 00:00 - 09:00 UTC
    "london": (7, 16),  # 07:00 - 16:00 UTC
    "ny":     (13, 22), # 13:00 - 22:00 UTC
}


def current_session(now: datetime = None) -> dict:
    """Identify active trading session(s) and overlap status for the current UTC hour."""
    now = now or datetime.now(timezone.utc)
    h = now.hour
    weekday = now.weekday()  # 0=Mon, 6=Sun
    active = [name for name, (s, e) in SESSIONS.items() if s <= h < e]
    primary = "london_ny_overlap" if {"london", "ny"} <= set(active) else (active[0] if active else "off-hours")
    is_weekend = weekday >= 5
    return {
        "utc_hour": h,
        "weekday": weekday,
        "active_sessions": active,
        "primary": primary,
        "is_weekend": is_weekend,
        "is_high_volume_window": primary == "london_ny_overlap",
    }


def session_bias_for(symbol: str, session: dict) -> dict:
    """Return a regime hint for the AI based on symbol + current session.

    XAUUSD: trend-bias during London/NY overlap, range-bias during Tokyo
    BTCUSD: thinner liquidity + whale-driven during weekends
    """
    sym = symbol.upper()
    if sym in ("XAUUSD", "XAGUSD"):
        if session["is_high_volume_window"]:
            return {"preferred_strategy": "trend_following",
                    "note": "London/NY overlap — gold typically forms clean directional trends"}
        if session["primary"] == "tokyo":
            return {"preferred_strategy": "mean_reversion",
                    "note": "Tokyo session — gold typically consolidates in tight ranges"}
        return {"preferred_strategy": "neutral", "note": "Off-hours — proceed with caution"}
    if sym.endswith("USD") and sym[:-3] in ("BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"):
        if session["is_weekend"]:
            return {"preferred_strategy": "counter_trend",
                    "note": "Weekend crypto — thin liquidity, watch for whale-driven liquidation fakeouts"}
        return {"preferred_strategy": "trend_following", "note": "Weekday crypto — normal flow"}
    # Forex generic
    if session["is_weekend"]:
        return {"preferred_strategy": "skip", "note": "FX market closed on weekends"}
    return {"preferred_strategy": "trend_following", "note": "Weekday FX — normal flow"}


def classify_regime(indicators: dict) -> dict:
    """Label the current market regime from already-computed indicators.

    Buckets:
      - HIGH_VOL_TREND: directional move with elevated volatility
      - LOW_VOL_TREND: directional move with calm tape
      - RANGE: price oscillating, mean-reverting setups favoured
      - CHOP: high vol + no direction (the alpha-destroying state — bot should HOLD)
    """
    if not indicators:
        return {"regime": "unknown", "reason": "insufficient data"}

    vol = indicators.get("volatility_30d_pct") or 0
    sma20 = indicators.get("sma_20")
    sma50 = indicators.get("sma_50")
    sma200 = indicators.get("sma_200")
    rsi = indicators.get("rsi_14") or 50
    price = indicators.get("current_price") or 0

    # Trend strength: distance between SMAs scaled by price
    trend_strength = 0.0
    if sma20 and sma50 and price > 0:
        trend_strength = abs(sma20 - sma50) / price * 100  # % spread

    is_trending = trend_strength > 0.8 and (
        (sma20 and sma50 and sma200 and sma20 > sma50 > sma200) or  # bullish stack
        (sma20 and sma50 and sma200 and sma20 < sma50 < sma200)     # bearish stack
    )
    is_high_vol = vol > 2.5
    is_range = trend_strength < 0.3 and 35 < rsi < 65

    if is_trending and is_high_vol:
        regime = "HIGH_VOL_TREND"
        reason = f"Strong directional bias (SMA spread {trend_strength:.2f}%) with elevated vol {vol:.2f}%"
    elif is_trending and not is_high_vol:
        regime = "LOW_VOL_TREND"
        reason = f"Clean trend (SMA spread {trend_strength:.2f}%) in calm market (vol {vol:.2f}%)"
    elif is_range:
        regime = "RANGE"
        reason = f"Tight range (SMA spread {trend_strength:.2f}%, RSI {rsi})"
    elif is_high_vol and not is_trending:
        regime = "CHOP"
        reason = f"High vol {vol:.2f}% without direction — avoid trend entries"
    else:
        regime = "TRANSITIONAL"
        reason = "Mixed signals — wait for confirmation"

    return {
        "regime": regime,
        "reason": reason,
        "trend_strength": round(trend_strength, 3),
        "volatility_pct": round(vol, 2),
    }
