"""Risk management profiles + Kelly-modified dynamic position sizing."""
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "extreme"]

# `kelly_cap` is the maximum fraction of profile risk to deploy on a 100%-confidence
# signal. Lower-risk profiles cap Kelly tighter to avoid over-betting.
PROFILES = {
    "low":     {"label": "Low",     "risk_pct": 0.5, "sl_atr_mult": 1.5, "tp_atr_mult": 2.0,
                "min_confidence": 75, "max_concurrent": 1, "leverage_cap": 10,  "kelly_cap": 0.25},
    "medium":  {"label": "Medium",  "risk_pct": 1.0, "sl_atr_mult": 1.5, "tp_atr_mult": 2.5,
                "min_confidence": 65, "max_concurrent": 2, "leverage_cap": 30,  "kelly_cap": 0.50},
    "high":    {"label": "High",    "risk_pct": 2.5, "sl_atr_mult": 1.2, "tp_atr_mult": 3.0,
                "min_confidence": 55, "max_concurrent": 4, "leverage_cap": 100, "kelly_cap": 0.75},
    "extreme": {"label": "Extreme", "risk_pct": 5.0, "sl_atr_mult": 1.0, "tp_atr_mult": 4.0,
                "min_confidence": 45, "max_concurrent": 6, "leverage_cap": 500, "kelly_cap": 1.00},
}


def get_profile(level: str) -> dict:
    return PROFILES.get(level, PROFILES["medium"])


def kelly_fraction(confidence_pct: float, min_conf: float, profile_kelly_cap: float,
                   payoff_ratio: float = 2.0) -> float:
    """Modified Kelly Criterion.

    Standard Kelly: f* = (p*b - (1-p)) / b
        where p = win probability, b = payoff ratio (TP/SL = R:R).
    We clamp negative values to 0 (no bet) and cap at profile_kelly_cap to avoid
    pathological over-leveraging on overconfident model output.

    confidence_pct: Claude's confidence (0-100). Treated as win probability.
    min_conf: profile's gate; signals below this aren't tradeable so we ignore them here.
    profile_kelly_cap: per-profile ceiling (0-1).
    """
    if confidence_pct < min_conf:
        return 0.0
    p = confidence_pct / 100.0
    b = max(payoff_ratio, 0.1)
    f_star = (p * b - (1 - p)) / b
    if f_star <= 0:
        return 0.0
    return min(f_star, profile_kelly_cap)


def compute_position_size(equity: float, risk_pct: float, sl_pips: float,
                          pip_value: float = 1.0) -> float:
    """Classic risk-percent sizing (fallback for legacy callers)."""
    if sl_pips <= 0 or pip_value <= 0:
        return 0.01
    risk_amount = equity * (risk_pct / 100.0)
    lots = risk_amount / (sl_pips * pip_value)
    return max(round(lots, 2), 0.01)


def compute_kelly_position_size(equity: float, confidence_pct: float, sl_pips: float,
                                profile: dict, pip_value: float = 1.0) -> dict:
    """Dynamic position sizing using modified Kelly Criterion.

    Returns: { lot_size, risk_amount, kelly_f, effective_risk_pct }
    """
    if sl_pips <= 0 or pip_value <= 0:
        return {"lot_size": 0.01, "risk_amount": 0.0, "kelly_f": 0.0, "effective_risk_pct": 0.0}

    payoff_ratio = profile["tp_atr_mult"] / max(profile["sl_atr_mult"], 0.1)
    f = kelly_fraction(
        confidence_pct=confidence_pct,
        min_conf=profile["min_confidence"],
        profile_kelly_cap=profile["kelly_cap"],
        payoff_ratio=payoff_ratio,
    )
    # Effective risk = profile.risk_pct × Kelly fraction (relative to max).
    # Kelly is already a fraction-of-bankroll; we scale it by profile's risk_pct
    # to keep the user's chosen risk tier as the hard ceiling.
    effective_risk_pct = profile["risk_pct"] * (f / profile["kelly_cap"]) if profile["kelly_cap"] > 0 else 0
    risk_amount = equity * (effective_risk_pct / 100.0)
    lots = risk_amount / (sl_pips * pip_value)
    return {
        "lot_size": max(round(lots, 2), 0.01),
        "risk_amount": round(risk_amount, 2),
        "kelly_f": round(f, 4),
        "effective_risk_pct": round(effective_risk_pct, 3),
    }


def derive_sl_tp(action: str, entry: float, atr: float, profile: dict) -> tuple:
    sl_dist = atr * profile["sl_atr_mult"]
    tp_dist = atr * profile["tp_atr_mult"]
    if action == "BUY":
        return round(entry - sl_dist, 5), round(entry + tp_dist, 5)
    elif action == "SELL":
        return round(entry + sl_dist, 5), round(entry - tp_dist, 5)
    return entry, entry

