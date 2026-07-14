"""iter-142 · Deterministic setup score (quant review: replaces synthetic
confidence).

The old signal confidence was `profile.min_confidence + 5` — a constant that
always cleared its own gate and fed a single garbage bucket to calibration.
This module computes an honest 25-92 quality score from the live feature
pack the engine actually fired on. It is a SETUP SCORE, not a probability:
the calibrated p_win comes from calibration.calibrated_p_win, which maps
this score to the engine's realized win rate.
"""

BASE = 50.0
SCORE_MIN, SCORE_MAX = 25.0, 92.0
BASIS = "setup_score_v1"


def compute_setup_score(engine: str, action: str, feats: dict | None,
                        mtf_conf: dict | None = None,
                        entry_style: str = "trend") -> dict:
    f = feats or {}
    comps: list = []
    score = BASE

    def add(name: str, pts: float):
        nonlocal score
        pts = round(float(pts), 1)
        if pts:
            score += pts
            comps.append({"factor": name, "pts": pts})

    sign = 1.0 if action == "BUY" else -1.0
    mom = float(f.get("momentum_3h_pct") or 0)
    slope = float(f.get("ema20_slope_pct_2h") or 0)
    trend = f.get("trend")
    dch = f.get("donchian20")
    pos = f.get("range_pos_pct")
    day_rng = float(f.get("day_range_pct") or 0)
    vdist = float(f.get("vwap_dist_pct") or 0)

    if entry_style == "fade":
        # fades profit from stretch snapping back — reward the stretch,
        # punish momentum still running against the fade
        add("vwap_stretch", min(abs(vdist) / 0.5, 1.0) * 10)
        if mom * sign < 0:
            add("counter_momentum_risk", -min(abs(mom) / 0.30, 1.0) * 8)
        if trend == "FLAT":
            add("rangebound_regime", 6)
        elif (trend == "UP") != (action == "BUY"):
            add("fading_a_trend", -8)
    else:
        add("momentum", (min(abs(mom) / 0.30, 1.0) * 12) if mom * sign > 0
            else (-min(abs(mom) / 0.30, 1.0) * 8 if mom * sign < 0 else 0))
        add("ema_slope", (min(abs(slope) / 0.20, 1.0) * 10) if slope * sign > 0
            else (-min(abs(slope) / 0.20, 1.0) * 6 if slope * sign < 0 else 0))
        if trend in ("UP", "DOWN"):
            add("trend_alignment", 8 if (trend == "UP") == (action == "BUY") else -8)
        if dch in ("BREAK_UP", "BREAK_DOWN"):
            add("donchian_confirm",
                6 if (dch == "BREAK_UP") == (action == "BUY") else -6)

    if pos is not None:
        p = float(pos)
        room = (100 - p) if action == "BUY" else p
        if room >= 30:
            add("room_to_run", 4)
        elif room <= 8:
            add("chasing_extreme", -6)

    if 0.3 <= day_rng <= 2.0:
        add("healthy_activity", 4)
    elif day_rng > 2.5:
        add("chaotic_range", -4)
    elif day_rng < 0.15 and day_rng > 0:
        add("dead_tape", -4)

    if mtf_conf and mtf_conf.get("aligned"):
        add("mtf_cascade_aligned", 8)

    score = max(SCORE_MIN, min(SCORE_MAX, score))
    return {"score": round(score, 1), "components": comps, "basis": BASIS}
