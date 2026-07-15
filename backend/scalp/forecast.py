"""Scalp subsystem · Step 5/6 — three-quantity forecast, deterministic baseline.

The logistic model (scalp/model.py) replaces the deterministic probability
ONLY after it beats the baseline on walk-forward OOS AUC with enough samples.
"""
from dataclasses import dataclass, asdict

from scalp.costs import expected_costs


@dataclass(frozen=True)
class ScalpForecast:
    p_target_before_stop: float
    expected_favorable_move_pips: float
    expected_adverse_move_pips: float
    expected_spread_cost_pips: float
    expected_slippage_pips: float
    expected_commission_pips: float
    uncertainty_pips: float
    target_pips: float
    stop_pips: float
    p_source: str = "deterministic"

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def make(feats: dict, candidate: dict, state, cfg,
         model_p: float | None = None,
         commission_pips: float = 0.0) -> ScalpForecast:
    spread = float(feats["spread_pips"])
    vol_short = max(float(feats["vol_short"]), 0.3)

    # geometry — stop wide enough to breathe past spread noise
    stop_pips = _clamp(max(2.0 * spread, 1.5 * vol_short), 1.0, 5.0)
    target_pips = round(1.3 * stop_pips, 2)

    # deterministic probability: conservative base + setup-quality bonuses
    p = 0.48
    if candidate["impulse_pips"] >= 2.5 * vol_short:
        p += 0.03
    if 0.25 <= candidate["pullback_frac"] <= 0.45:
        p += 0.03
    if candidate["accel_pips"] > 0.2:
        p += 0.02
    if feats["spread_pctl"] <= 0.5:
        p += 0.01
    p = _clamp(p, 0.40, 0.60)
    p_source = "deterministic"
    if model_p is not None:
        # trained + calibrated model wins, clamped to a sane scalp band
        p = _clamp(model_p, 0.30, 0.70)
        p_source = "logistic_calibrated"

    costs = expected_costs(feats, state, cfg, commission_pips)
    uncertainty = round(max(0.15, 0.25 * vol_short), 2)

    return ScalpForecast(
        p_target_before_stop=round(p, 3),
        expected_favorable_move_pips=round(target_pips * p, 2),
        expected_adverse_move_pips=round(stop_pips * (1 - p), 2),
        expected_spread_cost_pips=costs["expected_spread_cost_pips"],
        expected_slippage_pips=costs["expected_slippage_pips"],
        expected_commission_pips=costs["expected_commission_pips"],
        uncertainty_pips=uncertainty,
        target_pips=target_pips,
        stop_pips=round(stop_pips, 2),
        p_source=p_source,
    )
