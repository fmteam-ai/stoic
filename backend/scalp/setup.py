"""Scalp subsystem · Step 4 — the ONE primary setup.

v1.60.29 A1 — REDESIGNED. The micro-pullback (30 s impulse, 1.3-pip targets, 13 s median hold) measured on 343
labeled shadow samples: −41 pips BEFORE costs, −363 net, PF 0.27, expectancy −1.06 pips/trade — targets smaller than
the costs and a negative raw signal. The primary setup is now a 5-minute RANGE BREAK WITH MOMENTUM:
  • direction = sign of the 5-minute return, |ret_5m| ≥ max(3 pips, 2 × vol_long)
  • fresh break of the prior-5-minute range (measured up to 30 s ago) by ≥ 0.3 pip, within the last 30 s
  • spread ≤ 0.8 pip and in the lower 60 % of the session's spread distribution
  • not chasing: the last minute carries < 60 % of the 5-minute move and |TWAP deviation| ≤ 0.7 × range
Geometry (forecast.py): stop 3–6 pips from the range, target ≥ 5 pips (1.6 R), hold up to 15 minutes.
"""
DEFAULTS = {
    "momentum_min_pips": 3.0,      # |ret_5m| floor
    "momentum_vol_mult": 2.0,      # or ≥ mult × 5-min vol, whichever larger
    "break_min_pips": 0.3,         # how far beyond the prior range the mid must be
    "max_spread_pips": 0.8,
    "max_spread_pctl": 0.6,
    "late_move_frac": 0.6,         # ret_1m / ret_5m above this = chasing
    "max_twap_dev_frac": 0.7,      # |twap_dev| / range above this = overextended
}

# v1.60.31 item 3 — gold is a different animal in pips (pip = 0.01): spreads run 15–35 pips and a 5-minute range is
# 50–200 pips, so the EURUSD thresholds (0.8-pip spread cap, 3-pip momentum, 0.3-pip break) could never fire.
SYMBOL_OVERRIDES = {
    "XAUUSD": {"momentum_min_pips": 30.0, "break_min_pips": 5.0, "max_spread_pips": 35.0},
}


def params_for(symbol: str | None, params: dict | None = None) -> dict:
    """DEFAULTS ← per-symbol overrides ← preset params (presets tune selectivity, never the instrument scale)."""
    return {**DEFAULTS, **SYMBOL_OVERRIDES.get((symbol or "").upper(), {}), **(params or {})}

# legacy micro-pullback thresholds (kept for the shadow dataset's older rows / tests)
MICRO_PULLBACK_DEFAULTS = {
    "impulse_min_pips": 1.5, "impulse_vol_mult": 1.8,
    "pullback_min_frac": 0.15, "pullback_max_frac": 0.60, "resume_min_pips": 0.1,
}


def detect(feats: dict, state, params: dict | None = None, symbol: str | None = None) -> dict | None:
    """Range-break-with-momentum candidate {direction, setup, ...} or None."""
    p = params_for(symbol, params)
    ret_5m = feats.get("ret_5m")
    rng = feats.get("range_5m_pips")
    if ret_5m is None or rng is None or rng <= 0:
        return None                                   # < 5 minutes of ticks — no opinion yet
    if feats["spread_pips"] > p["max_spread_pips"]:
        return None
    # percentile rule only bites when the spread is actually elevated (a constant spread reads pctl 1.0)
    if feats.get("spread_pctl", 1.0) > p["max_spread_pctl"] and feats["spread_pips"] > 0.75 * p["max_spread_pips"]:
        return None
    floor = max(p["momentum_min_pips"], p["momentum_vol_mult"] * float(feats.get("vol_long") or 0))
    if abs(ret_5m) < floor:
        return None
    direction = "BUY" if ret_5m > 0 else "SELL"
    brk = feats["break_up_pips"] if direction == "BUY" else feats["break_dn_pips"]
    if brk is None or brk < p["break_min_pips"]:
        return None
    ret_1m = float(feats.get("ret_1m") or 0)
    if direction == "SELL":
        ret_1m = -ret_1m
    if ret_1m / abs(ret_5m) > p["late_move_frac"]:
        return None                                   # most of the move happened in the last minute — chasing
    dev = float(feats.get("twap_dev_pips") or 0)
    if abs(dev) > p["max_twap_dev_frac"] * rng:
        return None
    return {
        "direction": direction,
        "setup": "range_break_momentum_v2",
        "ret_5m_pips": round(ret_5m, 2),
        "range_5m_pips": round(rng, 2),
        "break_pips": round(brk, 2),
        "late_frac": round(ret_1m / abs(ret_5m), 3),
        "twap_dev_pips": round(dev, 2),
        # fields the forecast / model still read
        "impulse_pips": round(abs(ret_5m), 2),
        "pullback_pips": round(max(0.0, rng - brk), 2),
        "pullback_frac": 0.0,
        "resume_pips": round(ret_1m, 2),
        "accel_pips": round(float(feats.get("accel") or 0), 3),
    }


def detect_micro_pullback(feats: dict, state, params: dict | None = None) -> dict | None:
    """LEGACY (pre-v1.60.29) — kept only so older shadow rows and tests stay interpretable."""
    p = {**MICRO_PULLBACK_DEFAULTS, **(params or {})}
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
