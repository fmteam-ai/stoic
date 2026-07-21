"""Phase 1 · Value-driven trading — $ Expected Value + 0-100 Trade Quality.

Shifts both trading paths (main MTF bot + scalp fast path) from
confidence-driven to value-driven decisions:

  · compute_ev()      — expected move, costs, net edge (pips) and EV in $
                        from a calibrated win probability and trade geometry.
  · quality_score()   — additive, explainable 0-100 score (EV, confidence,
                        trend alignment, liquidity, spread, volatility,
                        time-of-day). OBSERVE-ONLY until calibrated on real
                        data (QUALITY_GATE_ENABLED=False by design).
  · EV gate           — autonomous trades with EV ≤ MIN_EV_USD are skipped
                        (ACTIVE for the main bot; scalp already gates on
                        net-edge pips).
  · sizing            — main bot adaptive risk clamped to
                        [RISK_FLOOR_PCT, RISK_CAP_PCT] (user: 0.25% - 1.3%);
                        scalp lots downscale on poor quality (never upscale —
                        the scalp risk budget stays the hard ceiling).
"""

MIN_EV_USD = 0.0                 # autonomous trades must clear this EV in $
QUALITY_GATE_ENABLED = False     # observe-first (user decision) — score, never gate
QUALITY_MIN_SCORE = 80           # future gate threshold once calibrated
RISK_FLOOR_PCT = 0.25            # dynamic sizing bounds (user decision)
RISK_CAP_PCT = 1.30

# additive score weights — must sum to 100
WEIGHTS = {
    "ev": 22, "confidence": 18, "trend_alignment": 18,
    "liquidity": 14, "spread": 12, "volatility": 10, "time_of_day": 6,
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_ev(p_win: float, target_pips: float, stop_pips: float,
               cost_pips: float, pip_value_usd_per_lot: float | None = None,
               lot: float | None = None) -> dict:
    """EV in pips and $ for one trade: p·target − (1−p)·stop − costs."""
    p = _clamp(float(p_win or 0.5), 0.01, 0.99)
    target = max(0.0, float(target_pips or 0))
    stop = max(0.0, float(stop_pips or 0))
    cost = max(0.0, float(cost_pips or 0))
    expected_move = p * target - (1 - p) * stop
    ev_pips = expected_move - cost
    ev_usd = None
    if pip_value_usd_per_lot and lot:
        ev_usd = round(ev_pips * float(pip_value_usd_per_lot) * float(lot), 2)
    return {
        "p_win": round(p, 4),
        "target_pips": round(target, 2),
        "stop_pips": round(stop, 2),
        "expected_move_pips": round(expected_move, 3),
        "cost_pips": round(cost, 3),
        "ev_pips": round(ev_pips, 3),
        "ev_usd": ev_usd,
        "version": 1,
    }


def quality_score(*, ev_pips: float, cost_pips: float, p_win: float,
                  trend_alignment: float | None = None,
                  liquidity: float | None = None,
                  spread_ratio: float | None = None,
                  volatility_ratio: float | None = None,
                  hour_utc: int | None = None) -> dict:
    """Additive 0-100 Trade Quality Score with an explainable breakdown.

    Normalized inputs (None → neutral half-credit, never a penalty for
    missing data):
      trend_alignment  0..1 (1 = fully aligned with larger structure)
      liquidity        0..1 (1 = deep/active market)
      spread_ratio     current spread / allowed cap (0 best, ≥1 worst)
      volatility_ratio short-term vol / long-term vol (~1 is calm/normal)
    """
    b: dict[str, float] = {}

    # EV: full credit when net edge ≥ round-trip cost (edge ≥ 1× cost)
    cost = max(0.25, float(cost_pips or 0.25))
    b["ev"] = _clamp(float(ev_pips or 0) / cost, 0.0, 1.0) * WEIGHTS["ev"]

    # confidence: p 0.50 → 0, p ≥ 0.75 → full
    b["confidence"] = _clamp((float(p_win or 0.5) - 0.50) / 0.25, 0.0, 1.0) \
        * WEIGHTS["confidence"]

    b["trend_alignment"] = (WEIGHTS["trend_alignment"] * 0.5
                            if trend_alignment is None else
                            _clamp(float(trend_alignment), 0, 1)
                            * WEIGHTS["trend_alignment"])
    b["liquidity"] = (WEIGHTS["liquidity"] * 0.5 if liquidity is None else
                      _clamp(float(liquidity), 0, 1) * WEIGHTS["liquidity"])
    b["spread"] = (WEIGHTS["spread"] * 0.5 if spread_ratio is None else
                   (1.0 - _clamp(float(spread_ratio), 0.0, 1.0))
                   * WEIGHTS["spread"])
    if volatility_ratio is None:
        b["volatility"] = WEIGHTS["volatility"] * 0.5
    else:
        # calm/normal (≤1.2×) full credit, 3×+ expansion → zero
        vr = max(0.0, float(volatility_ratio))
        b["volatility"] = (1.0 - _clamp((vr - 1.2) / 1.8, 0.0, 1.0)) \
            * WEIGHTS["volatility"]
    if hour_utc is None:
        b["time_of_day"] = WEIGHTS["time_of_day"] * 0.5
    else:
        h = int(hour_utc) % 24
        b["time_of_day"] = (WEIGHTS["time_of_day"] if 7 <= h < 17
                            else WEIGHTS["time_of_day"] * 0.6
                            if 17 <= h < 20 else WEIGHTS["time_of_day"] * 0.2)

    breakdown = {k: round(v, 1) for k, v in b.items()}
    score = int(round(sum(b.values())))
    return {"score": _clamp(score, 0, 100), "breakdown": breakdown,
            "gate_enabled": QUALITY_GATE_ENABLED,
            "gate_min": QUALITY_MIN_SCORE, "version": 1}


def scalp_size_multiplier(score: float) -> float:
    """Scalp lots DOWNSCALE on poor quality (0.5×-1.0×). Never upscale —
    the scalp per-trade risk budget stays the hard ceiling and the
    pre-submit re-size takes min(decision lot, fresh lot) regardless."""
    return round(_clamp(0.5 + float(score or 0) / 200.0, 0.5, 1.0), 2)
