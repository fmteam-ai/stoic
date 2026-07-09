"""Per-account strategy engines (iter-127).

Each account's active preset selects EXACTLY ONE execution engine.
MTF engines run the top-down cascade (mtf_intraday.py) at different
strictness levels; the deterministic engines below read the live M15
feature pack (intraday_features.py) and return (signal|None, note).
"""

ENGINE_BY_PRESET = {
    "sniper": "mtf_strict",
    "balanced": "mtf_moderate",
    "trend_rider": "mtf_relaxed",
    "scalper": "hf_scalp",
    "fast_scalp": "hf_scalp_fast",
    "breakout": "breakout_m15",
    "mean_reversion": "range_fade",
}

MTF_MODE_BY_ENGINE = {
    "mtf_strict": "strict",
    "mtf_moderate": "moderate",
    "mtf_relaxed": "relaxed",
}

SCALP_ENGINES = {"hf_scalp", "hf_scalp_fast"}
SCALP_RISK_PCT_CAP = 0.25   # user-selected: 0.25% risk per HF scalp trade

# Engines whose entries are deterministic rule-hits on live M15 data —
# ensemble/forecast-model gates (built for the old daily LLM signal) are
# advisory-only for these scopes.
DETERMINISTIC_INTRADAY_SCOPES = {"hf_scalp", "hf_scalp_fast", "range_fade", "breakout_m15"}

ENGINE_LABELS = {
    "mtf_strict": "SNIPER · Strict MTF Cascade",
    "mtf_moderate": "BALANCED · Moderate MTF Cascade",
    "mtf_relaxed": "TREND RIDER · Relaxed MTF Cascade",
    "hf_scalp": "SCALPER · HF Momentum",
    "hf_scalp_fast": "FAST SCALP · HF Momentum (turbo)",
    "breakout_m15": "BREAKOUT · Donchian-20 M15",
    "range_fade": "MEAN REVERSION · Range Fade",
}


def resolve_engine(preset: str | None) -> str:
    if not preset or str(preset).startswith("custom:"):
        return "mtf_moderate"
    return ENGINE_BY_PRESET.get(str(preset), "mtf_moderate")


def hf_scalp_signal(feats: dict, fast: bool = False) -> tuple:
    """High-frequency momentum scalper: EMA-stack bursts + VWAP bounces."""
    slope_min = 0.05 if fast else 0.08   # EMA20 slope %/2h
    mom_min = 0.06 if fast else 0.10     # 3h momentum %
    if float(feats.get("atr15") or 0) <= 0:
        return None, "no ATR15 yet"
    trend = feats.get("trend")
    last = float(feats.get("last_price") or 0)
    ema20 = float(feats.get("ema20") or 0)
    slope = float(feats.get("ema20_slope_pct_2h") or 0)
    mom = float(feats.get("momentum_3h_pct") or 0)
    vdist = float(feats.get("vwap_dist_pct") or 0)
    pos = feats.get("range_pos_pct")
    dch = feats.get("donchian20")
    if trend == "UP":
        if pos is not None and float(pos) > 95 and dch == "INSIDE":
            return None, f"uptrend but price at {pos}% of session range without a breakout — no chase"
        if slope >= slope_min and mom >= mom_min and last >= ema20:
            return "BUY", (f"momentum burst: EMA20 slope +{slope}%/2h, "
                           f"3h momentum +{mom}%, price above EMA20")
        if -0.20 <= vdist <= 0.05:
            return "BUY", f"VWAP bounce: uptrend pullback to session VWAP (dist {vdist}%)"
        return None, (f"uptrend, no burst yet (slope {slope}%/2h, 3h mom {mom}%; "
                      f"need ≥{slope_min}%/{mom_min}%, price≥EMA20) and no VWAP touch (dist {vdist}%)")
    if trend == "DOWN":
        if pos is not None and float(pos) < 5 and dch == "INSIDE":
            return None, f"downtrend but price at {pos}% of session range without a breakout — no chase"
        if slope <= -slope_min and mom <= -mom_min and last <= ema20:
            return "SELL", (f"momentum burst: EMA20 slope {slope}%/2h, "
                            f"3h momentum {mom}%, price below EMA20")
        if -0.05 <= vdist <= 0.20:
            return "SELL", f"VWAP bounce: downtrend rally to session VWAP (dist {vdist}%)"
        return None, (f"downtrend, no burst yet (slope {slope}%/2h, 3h mom {mom}%; "
                      f"need ≤-{slope_min}%/-{mom_min}%, price≤EMA20) and no VWAP touch (dist {vdist}%)")
    return None, "M15 trend FLAT — momentum engine needs a directional EMA stack"


def range_fade_signal(feats: dict) -> tuple:
    """Mean reversion: in a confirmed M15 range, fade extremes toward VWAP."""
    edge_pct = 20            # extreme = within this % of the session range edge
    min_day_pct = 0.5        # session must have moved at least this much
    min_atr_mult = 3.0       # range width must be ≥ this × ATR15
    if feats.get("trend") != "FLAT" or feats.get("donchian20") != "INSIDE":
        return None, "not rangebound (trend or breakout active)"
    atr15 = float(feats.get("atr15") or 0)
    hi, lo = feats.get("session_high"), feats.get("session_low")
    pos = feats.get("range_pos_pct")
    day_rng = float(feats.get("day_range_pct") or 0)
    if not atr15 or hi is None or lo is None or pos is None:
        return None, "missing range data"
    if day_rng < min_day_pct:
        return None, f"day range {day_rng}% too small to fade"
    if (float(hi) - float(lo)) < min_atr_mult * atr15:
        return None, "range too narrow vs ATR — target not reachable"
    pos = float(pos)
    if pos <= edge_pct:
        return "BUY", (f"range fade: price at {pos}% of session range "
                       f"({lo}–{hi}), fading the low toward VWAP {feats.get('session_vwap')}")
    if pos >= 100 - edge_pct:
        return "SELL", (f"range fade: price at {pos}% of session range "
                        f"({lo}–{hi}), fading the high toward VWAP {feats.get('session_vwap')}")
    return None, f"mid-range ({pos}%) — waiting for an extreme"


def breakout_signal(feats: dict) -> tuple:
    """Donchian-20 M15 channel escape in the direction of the break."""
    if float(feats.get("atr15") or 0) <= 0:
        return None, "no ATR15 yet"
    dch = feats.get("donchian20")
    mom = float(feats.get("momentum_3h_pct") or 0)
    day_rng = float(feats.get("day_range_pct") or 0)
    if day_rng < 0.3:
        return None, f"day range {day_rng}% too quiet for a breakout play"
    if dch == "BREAK_UP":
        if mom < 0:
            return None, f"Donchian BREAK_UP but 3h momentum negative ({mom}%) — likely fake-out"
        return "BUY", f"Donchian-20 breakout UP with 3h momentum +{mom}%"
    if dch == "BREAK_DOWN":
        if mom > 0:
            return None, f"Donchian BREAK_DOWN but 3h momentum positive (+{mom}%) — likely fake-out"
        return "SELL", f"Donchian-20 breakout DOWN with 3h momentum {mom}%"
    return None, "price inside the Donchian-20 channel — waiting for an escape"


def run_engine(engine: str, feats: dict) -> tuple:
    if engine == "hf_scalp":
        return hf_scalp_signal(feats, fast=False)
    if engine == "hf_scalp_fast":
        return hf_scalp_signal(feats, fast=True)
    if engine == "range_fade":
        return range_fade_signal(feats)
    if engine == "breakout_m15":
        return breakout_signal(feats)
    return None, f"unknown engine '{engine}'"
