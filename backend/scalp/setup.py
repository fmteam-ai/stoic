"""Scalp subsystem · Step 4 — the ONE primary setup.

Micro-pullback momentum continuation. No breakout / reversal / market-making
variants until this setup proves positive out-of-sample expectancy.
"""
DEFAULTS = {
    "impulse_min_pips": 1.5,       # 30s directional impulse floor
    "impulse_vol_mult": 1.8,       # or ≥ mult × short vol, whichever larger
    "pullback_min_frac": 0.15,     # retrace of the impulse
    "pullback_max_frac": 0.60,
    "resume_min_pips": 0.1,        # 1s return back in trend direction
}


def detect(feats: dict, state, params: dict | None = None) -> dict | None:
    """Return a candidate {direction, impulse_pips, pullback_frac, ...} or None."""
    p = {**DEFAULTS, **(params or {})}
    ret_30s = feats["ret_30s"]
    vol_short = feats["vol_short"]
    impulse_floor = max(p["impulse_min_pips"], p["impulse_vol_mult"] * vol_short)
    if abs(ret_30s) < impulse_floor:
        return None
    direction = "BUY" if ret_30s > 0 else "SELL"

    # pullback depth measured from the 30s extreme back to current mid
    now = feats["_now_ms"]
    pip = state.pip
    window = [(tm, mid) for tm, _, _, mid in state.ticks if tm >= now - 30_000]
    if len(window) < 10:
        return None
    mids = [m for _, m in window]
    cur = mids[-1]
    if direction == "BUY":
        extreme = max(mids)
        pullback_pips = (extreme - cur) / pip
    else:
        extreme = min(mids)
        pullback_pips = (cur - extreme) / pip
    impulse_pips = abs(ret_30s)
    frac = pullback_pips / impulse_pips if impulse_pips > 0 else 0.0
    if not (p["pullback_min_frac"] <= frac <= p["pullback_max_frac"]):
        return None

    # resumption: short-term flow turning back with the trend
    resume = feats["ret_1s"] if direction == "BUY" else -feats["ret_1s"]
    accel = feats["accel"] if direction == "BUY" else -feats["accel"]
    if resume < p["resume_min_pips"] or accel <= 0:
        return None

    return {
        "direction": direction,
        "impulse_pips": round(impulse_pips, 2),
        "pullback_pips": round(pullback_pips, 2),
        "pullback_frac": round(frac, 3),
        "resume_pips": round(resume, 2),
        "accel_pips": round(accel, 3),
    }
