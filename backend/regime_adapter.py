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
