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

# iter-138 · Tunable engine parameters. Defaults are EXACTLY the values that
# were previously hard-coded — passing params=None never changes behavior.
DEFAULT_PARAMS = {
    "hf_scalp": {"slope_min": 0.08, "mom_min": 0.10,
                 "exhaustion_vdist": 0.35, "flat_fade_min": 0.25},
    "hf_scalp_fast": {"slope_min": 0.05, "mom_min": 0.06,
                      "exhaustion_vdist": 0.30, "flat_fade_min": 0.20},
    "range_fade": {"edge_pct": 20.0, "min_day_pct": 0.5, "min_atr_mult": 3.0},
    "breakout_m15": {"min_day_rng": 0.3},
}

PARAM_BOUNDS = {
    "hf_scalp": {"slope_min": (0.03, 0.20), "mom_min": (0.04, 0.25),
                 "exhaustion_vdist": (0.20, 0.60), "flat_fade_min": (0.12, 0.45)},
    "hf_scalp_fast": {"slope_min": (0.02, 0.15), "mom_min": (0.03, 0.20),
                      "exhaustion_vdist": (0.18, 0.55), "flat_fade_min": (0.10, 0.40)},
    "range_fade": {"edge_pct": (10.0, 30.0), "min_day_pct": (0.3, 1.0),
                   "min_atr_mult": (2.0, 5.0)},
    "breakout_m15": {"min_day_rng": (0.15, 0.80)},
}

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


def hf_scalp_signal(feats: dict, fast: bool = False,
                    params: dict | None = None) -> tuple:
    """High-frequency momentum scalper: EMA-stack bursts + VWAP bounces."""
    prm = {**DEFAULT_PARAMS["hf_scalp_fast" if fast else "hf_scalp"], **(params or {})}
    slope_min = float(prm["slope_min"])   # EMA20 slope %/2h
    mom_min = float(prm["mom_min"])       # 3h momentum %
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
    rbreak = feats.get("recent_break")
    day_rng = float(feats.get("day_range_pct") or 0)

    def _knife(direction: str) -> str | None:
        """Falling-knife filter for fade entries (iter-128, tightened iter-129:
        was day_rng≥1.0 / 20-80 bands — too loose to catch the 2026-07-13
        0.57% grind): never fade INTO a fresh breakout or the working third
        of a directional day."""
        if direction == "BUY":
            if rbreak == "DOWN" or dch == "BREAK_DOWN":
                return "fresh breakdown in last 2h — no knife catching"
            if day_rng >= 0.6 and pos is not None and float(pos) <= 30:
                return f"price at {pos}% of a {day_rng}% down-day — no knife catching"
        else:
            if rbreak == "UP" or dch == "BREAK_UP":
                return "fresh breakout up in last 2h — no fading strength"
            if day_rng >= 0.6 and pos is not None and float(pos) >= 70:
                return f"price at {pos}% of a {day_rng}% up-day — no fading strength"
        return None

    if trend == "UP":
        if pos is not None and float(pos) > 95 and dch == "INSIDE":
            return None, f"uptrend but price at {pos}% of session range without a breakout — no chase"
        if slope >= slope_min and mom >= mom_min and last >= ema20:
            return "BUY", (f"momentum burst: EMA20 slope +{slope}%/2h, "
                           f"3h momentum +{mom}%, price above EMA20")
        if -0.20 <= vdist <= 0.05:
            return "BUY", f"VWAP bounce: uptrend pullback to session VWAP (dist {vdist}%)"
        # iter-132 · Trend-day continuation (2026-07-14: 600-pip CPI rally,
        # zero long entries all day — price gapped away from VWAP and never
        # returned; 3h momentum diluted to ~0 by the consolidation). Buy
        # shallow EMA20 pullbacks instead. Exhaustion gate caps the extreme.
        if (day_rng >= 1.0 and pos is not None and 55 <= float(pos) <= 85
                and ema20 > 0 and abs(last - ema20) / ema20 * 100 <= 0.15
                and slope >= 0 and dch != "BREAK_DOWN"):
            return "BUY", (f"trend-day continuation: {day_rng}% up-day, shallow "
                           f"pullback to EMA20 (dist "
                           f"{abs(last - ema20) / ema20 * 100:.2f}%) — riding the trend")
        if vdist >= float(prm["exhaustion_vdist"]) and mom <= 0 and dch != "BREAK_UP":
            k = _knife("SELL")
            if k:
                return None, f"exhaustion fade blocked: {k}"
            return "SELL", (f"exhaustion fade: price {vdist}% above VWAP with 3h "
                            f"momentum stalled ({mom}%) — fading back toward VWAP")
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
        if (day_rng >= 1.0 and pos is not None and 15 <= float(pos) <= 45
                and ema20 > 0 and abs(last - ema20) / ema20 * 100 <= 0.15
                and slope <= 0 and dch != "BREAK_UP"):
            return "SELL", (f"trend-day continuation: {day_rng}% down-day, shallow "
                            f"rally to EMA20 (dist "
                            f"{abs(last - ema20) / ema20 * 100:.2f}%) — riding the trend")
        if vdist <= -float(prm["exhaustion_vdist"]) and mom >= 0 and dch != "BREAK_DOWN":
            k = _knife("BUY")
            if k:
                return None, f"exhaustion fade blocked: {k}"
            return "BUY", (f"exhaustion fade: price {abs(vdist)}% below VWAP with 3h "
                           f"momentum stalled (+{mom}%) — fading back toward VWAP")
        return None, (f"downtrend, no burst yet (slope {slope}%/2h, 3h mom {mom}%; "
                      f"need ≤-{slope_min}%/-{mom_min}%, price≤EMA20) and no VWAP touch (dist {vdist}%)")
    # FLAT market — micro mean-reversion around session VWAP (the classic
    # high-frequency "small doses" behavior; tight stop is the protection)
    # iter-132 · Trend-day flag: after a big directional day the M15 EMA
    # stack converges during consolidation and reads FLAT — but holding the
    # upper third of a 1.2%+ up-day is a continuation flag, not a fade zone.
    if day_rng >= 1.2 and pos is not None:
        p = float(pos)
        if 62 <= p <= 85 and slope >= -0.02 and mom >= -0.05 and dch != "BREAK_DOWN":
            return "BUY", (f"trend-day flag: consolidating at {p}% of a "
                           f"{day_rng}% up-day (slope {slope:+}%/2h) — continuation long")
        if 15 <= p <= 38 and slope <= 0.02 and mom <= 0.05 and dch != "BREAK_UP":
            return "SELL", (f"trend-day flag: consolidating at {p}% of a "
                            f"{day_rng}% down-day (slope {slope:+}%/2h) — continuation short")
    fade_min = float(prm["flat_fade_min"])   # % distance from VWAP required to fade
    if vdist >= fade_min and dch != "BREAK_UP":
        # iter-129: a persistent grind keeps price on one side of VWAP for
        # hours (2026-07-13: 109 BTC "fade back to VWAP" BUYs in a down
        # grind). Never fade against a sloping session.
        if slope >= 0.04:
            return None, (f"VWAP fade blocked: session grinding UP (EMA20 slope "
                          f"+{slope}%/2h) — price above VWAP is trend, not stretch")
        k = _knife("SELL")
        if k:
            return None, f"VWAP fade blocked: {k}"
        return "SELL", (f"VWAP fade: FLAT trend, price {vdist}% above session "
                        f"VWAP — fading back toward {feats.get('session_vwap')}")
    if vdist <= -fade_min and dch != "BREAK_DOWN":
        if slope <= -0.04:
            return None, (f"VWAP fade blocked: session grinding DOWN (EMA20 slope "
                          f"{slope}%/2h) — price below VWAP is trend, not stretch")
        k = _knife("BUY")
        if k:
            return None, f"VWAP fade blocked: {k}"
        return "BUY", (f"VWAP fade: FLAT trend, price {abs(vdist)}% below session "
                       f"VWAP — fading back toward {feats.get('session_vwap')}")
    return None, (f"FLAT trend, price within ±{fade_min}% of VWAP "
                  f"(dist {vdist}%) — no scalp edge")


def range_fade_signal(feats: dict, params: dict | None = None) -> tuple:
    """Mean reversion: in a confirmed M15 range, fade extremes toward VWAP."""
    p = {**DEFAULT_PARAMS["range_fade"], **(params or {})}
    edge_pct = float(p["edge_pct"])          # extreme = within this % of the range edge
    min_day_pct = float(p["min_day_pct"])    # session must have moved at least this much
    min_atr_mult = float(p["min_atr_mult"])  # range width must be ≥ this × ATR15
    if feats.get("trend") != "FLAT" or feats.get("donchian20") != "INSIDE":
        return None, "not rangebound (trend or breakout active)"
    atr15 = float(feats.get("atr15") or 0)
    hi, lo = feats.get("session_high"), feats.get("session_low")
    pos = feats.get("range_pos_pct")
    day_rng = float(feats.get("day_range_pct") or 0)
    if not atr15 or hi is None or lo is None or pos is None:
        return None, "missing range data"
    rbreak = feats.get("recent_break")
    if rbreak == "DOWN" and float(pos) <= edge_pct:
        return None, "fresh breakdown in last 2h — not fading the low"
    if rbreak == "UP" and float(pos) >= 100 - edge_pct:
        return None, "fresh breakout up in last 2h — not fading the high"
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


def breakout_signal(feats: dict, params: dict | None = None) -> tuple:
    """Donchian-20 M15 channel escape in the direction of the break."""
    p = {**DEFAULT_PARAMS["breakout_m15"], **(params or {})}
    if float(feats.get("atr15") or 0) <= 0:
        return None, "no ATR15 yet"
    dch = feats.get("donchian20")
    mom = float(feats.get("momentum_3h_pct") or 0)
    day_rng = float(feats.get("day_range_pct") or 0)
    if day_rng < float(p["min_day_rng"]):
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


def run_engine(engine: str, feats: dict, params: dict | None = None) -> tuple:
    if engine == "hf_scalp":
        return hf_scalp_signal(feats, fast=False, params=params)
    if engine == "hf_scalp_fast":
        return hf_scalp_signal(feats, fast=True, params=params)
    if engine == "range_fade":
        return range_fade_signal(feats, params=params)
    if engine == "breakout_m15":
        return breakout_signal(feats, params=params)
    return None, f"unknown engine '{engine}'"
