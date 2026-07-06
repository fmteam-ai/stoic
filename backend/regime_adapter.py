"""Regime-Adaptive Risk Modifier — swaps execution behaviour by live market regime.

A static config gets shredded in chaotic 2026 markets. This module mutates the
user's chosen profile into a per-trade *effective* profile based on real-time
regime classification.

Regime A — Defensive / Mean-Reversion (LOW_VOL_TREND, RANGE, weak signals):
    * Tighter SL multiplier (scalp ranges)
    * Lower TP multiplier (take quick profit)
    * Trim Kelly cap (smaller bets)
    * Bumped min_confidence

Regime B — Dynamic Volatility Execution (HIGH_VOL_TREND, news shocks):
    * Wider SL (avoid noise stop-outs)
    * Larger TP (ride momentum waves)
    * Larger Kelly cap (let confident signals breathe)
    * Slightly relaxed min_confidence

CHOP / TRANSITIONAL: forces HOLD via the existing regime veto upstream.
"""
from copy import deepcopy
from typing import Tuple


REGIME_MODIFIERS = {
    # mode-A (defensive scalp) — compressed vol, mean-revert behavior
    "LOW_VOL_TREND": {
        "mode": "DEFENSIVE_SCALP",
        "sl_atr_mult": 0.85,   # 15% tighter stops
        "tp_atr_mult": 0.75,   # 25% tighter targets (scalp)
        "kelly_cap": 0.90,     # 10% trim
        "min_confidence_delta": 0,
        # iter-53 · Loss-Lab guardrail: counter-momentum entries in this
        # regime clustered in the losing set (e.g. GOLD SELL while Kalman
        # velocity was > +3). Overridable per-user via cfg.regime_overrides.
        "velocity_counter_max": 3.0,     # veto SELL when kv > +3 / BUY when kv < -3
        "velocity_veto_threshold": 5.0,  # veto when |kv| > 5 AND contradicts MTF bias
    },
    "RANGE": {
        "mode": "DEFENSIVE_SCALP",
        "sl_atr_mult": 0.80,
        "tp_atr_mult": 0.70,
        "kelly_cap": 0.75,     # smaller bets — range = lower expectancy
        "min_confidence_delta": +5,
    },
    # mode-B (dynamic volatility) — let momentum run
    "HIGH_VOL_TREND": {
        "mode": "DYNAMIC_MOMENTUM",
        "sl_atr_mult": 1.40,   # 40% wider stops (avoid noise)
        "tp_atr_mult": 1.50,   # 50% larger targets (catch impulse)
        "kelly_cap": 1.10,     # 10% lift on conviction
        "min_confidence_delta": -3,
    },
    # transition states — neutral / hold
    "TRANSITIONAL": {
        "mode": "CAUTIOUS_WAIT",
        "sl_atr_mult": 1.0,
        "tp_atr_mult": 1.0,
        "kelly_cap": 0.50,    # half-size while regime resolves
        "min_confidence_delta": +10,
    },
    # CHOP is fully blocked upstream — defaults here are conservative anyway
    "CHOP": {
        "mode": "HALT",
        "sl_atr_mult": 0.5,
        "tp_atr_mult": 0.5,
        "kelly_cap": 0.0,
        "min_confidence_delta": +99,  # never trade
    },
}


def velocity_veto(signal: dict, cfg: dict | None = None) -> str | None:
    """iter-53 · Loss-Lab guardrail — velocity veto.

    Blocks entries whose Kalman price velocity fights the trade:
      1. Counter-momentum: SELL while kv > +`velocity_counter_max`
         (or BUY while kv < -max) in regimes that define the knob.
      2. Momentum/structure conflict: |kv| > `velocity_veto_threshold`
         while the velocity direction contradicts the MTF HTF bias.
    Thresholds live on REGIME_MODIFIERS and can be overridden per-user via
    cfg["regime_overrides"][regime]. Returns a human-readable veto reason
    or None to allow the trade.
    """
    action = signal.get("action")
    if action not in ("BUY", "SELL"):
        return None
    regime_name = ((signal.get("regime") or {}).get("regime") or "").upper()
    mods = REGIME_MODIFIERS.get(regime_name) or {}
    ov = ((cfg or {}).get("regime_overrides") or {}).get(regime_name) or {}
    counter_max = ov.get("velocity_counter_max", mods.get("velocity_counter_max"))
    veto_thresh = ov.get("velocity_veto_threshold", mods.get("velocity_veto_threshold"))
    if counter_max is None and veto_thresh is None:
        return None  # regime defines no velocity rules
    kf = ((signal.get("indicators") or {}).get("kalman_filter")
          or signal.get("kalman_filter") or {})
    kv = kf.get("k_velocity")
    if kv is None:
        return None
    kv = float(kv)
    if counter_max is not None:
        if action == "SELL" and kv > float(counter_max):
            return (f"Velocity veto ({regime_name}): SELL blocked — Kalman velocity "
                    f"+{kv:.1f} > +{float(counter_max):.1f}, price momentum is against the short.")
        if action == "BUY" and kv < -float(counter_max):
            return (f"Velocity veto ({regime_name}): BUY blocked — Kalman velocity "
                    f"{kv:.1f} < -{float(counter_max):.1f}, price momentum is against the long.")
    if veto_thresh is not None and abs(kv) > float(veto_thresh):
        htf = ((signal.get("mtf_gate") or {}).get("htf_trend") or "").upper()
        vel_dir = "UP" if kv > 0 else "DOWN"
        if htf in ("UP", "DOWN") and htf != vel_dir:
            return (f"Velocity veto ({regime_name}): |velocity| {abs(kv):.1f} > "
                    f"{float(veto_thresh):.1f} and contradicts the MTF bias "
                    f"(velocity {vel_dir} vs HTF {htf}) — momentum/structure conflict.")
    return None


def adapt_profile_for_regime(profile: dict, regime: dict) -> Tuple[dict, dict]:
    """Return (modified_profile, regime_meta).

    Pure function — does not mutate input. Multiplicative modifiers preserve
    the user's chosen risk tier as the baseline.
    """
    label = (regime or {}).get("regime", "TRANSITIONAL")
    mods = REGIME_MODIFIERS.get(label, REGIME_MODIFIERS["TRANSITIONAL"])

    new_profile = deepcopy(profile)
    new_profile["sl_atr_mult"] = round(profile["sl_atr_mult"] * mods["sl_atr_mult"], 3)
    new_profile["tp_atr_mult"] = round(profile["tp_atr_mult"] * mods["tp_atr_mult"], 3)
    new_profile["kelly_cap"] = max(0.0, min(1.0, profile["kelly_cap"] * mods["kelly_cap"]))
    new_profile["min_confidence"] = min(99, profile["min_confidence"] + mods["min_confidence_delta"])

    meta = {
        "execution_mode": mods["mode"],
        "regime_detected": label,
        "sl_multiplier_applied": mods["sl_atr_mult"],
        "tp_multiplier_applied": mods["tp_atr_mult"],
        "kelly_multiplier_applied": mods["kelly_cap"],
        "min_conf_shift": mods["min_confidence_delta"],
    }
    return new_profile, meta
