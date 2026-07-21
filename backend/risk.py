"""Risk management profiles + Kelly-modified dynamic position sizing."""
from typing import Literal

from pip_utils import price_to_pips, pip_value_usd_per_lot

RiskLevel = Literal["low", "medium", "high", "extreme"]

# C5: broker-minimum rounding may only overshoot the approved risk budget by
# this factor before the trade is rejected outright (fail-closed sizing).
RISK_OVERSHOOT_TOLERANCE = 1.5

# `kelly_cap` is the maximum fraction of profile risk to deploy on a 100%-confidence
# signal. Lower-risk profiles cap Kelly tighter to avoid over-betting.
PROFILES = {
    "low":     {"label": "Low",     "risk_pct": 0.5, "sl_atr_mult": 1.5, "tp_atr_mult": 2.0,
                "min_confidence": 75, "max_concurrent": 1, "leverage_cap": 10,  "kelly_cap": 0.25},
    "medium":  {"label": "Medium",  "risk_pct": 1.0, "sl_atr_mult": 1.5, "tp_atr_mult": 2.5,
                "min_confidence": 65, "max_concurrent": 2, "leverage_cap": 30,  "kelly_cap": 0.50},
    "high":    {"label": "High",    "risk_pct": 2.5, "sl_atr_mult": 1.2, "tp_atr_mult": 3.0,
                "min_confidence": 55, "max_concurrent": 4, "leverage_cap": 100, "kelly_cap": 0.75},
    "extreme": {"label": "Extreme", "risk_pct": 2.0, "sl_atr_mult": 1.0, "tp_atr_mult": 4.0,
                "min_confidence": 45, "max_concurrent": 6, "leverage_cap": 500, "kelly_cap": 0.50},
}
# quant review H4: full Kelly (1.00) on uncalibrated confidence is pathological
# over-betting — extreme profile capped at half-Kelly.


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


def compute_lot_for_account(account: dict, symbol: str, entry_price: float,
                            stop_loss: float, confidence_pct: float,
                            profile: dict, locked_profit: float = 0.0,
                            kelly_enabled: bool = False) -> dict:
    """Account-aware position sizing — the ONE authoritative stage, run at
    execute time (quant review C1/C2).

    kelly_enabled=False (default): fixed fractional risk = profile.risk_pct.
    Kelly stays disabled until confidence values are genuinely calibrated
    probabilities — the engine confidence is a synthetic setup score, not a
    p_win, so treating it as one distorts sizing.

    Uses the broker's real equity (not a hardcoded $1000), converts the SL
    price distance to pips via the symbol's pip size, and applies the proper
    USD-per-lot-per-pip table scaled by account_type (standard / cent /
    microcent). Returns the lot size you should pass to the broker, *before*
    the user's `max_lot_size` cap is applied.

    Why this matters: the ai_signals.py signal-time `lot_size` was computed
    with a fake $1000 equity and pip_value=1.0, which always over-shoots on
    larger live accounts — making the user's max_lot_size cap permanently
    binding (which feels like "the bot uses my max every trade").

    `locked_profit` (iter-65): subtracted from equity BEFORE sizing so that
    the daily-profit-target lock prevents that $ from being risked on later
    trades today. Defaults to 0 — no behaviour change when feature is off.
    """
    equity = float(
        account.get("equity")
        or account.get("balance")
        or account.get("initial_balance")
        or 0
    )
    # Iter-65: shave off the locked daily profit so subsequent trades can
    # never lose it. Floor at $0 — never go negative even if locked > equity
    # (defensive only; shouldn't happen in practice).
    if locked_profit > 0:
        equity = max(0.0, equity - float(locked_profit))
    if equity <= 0:
        return {"lot_size": 0.0, "sizing_valid": False,
                "method": "rejected_no_equity",
                "reject_reason": "equity unavailable — cannot compute risk"}

    sl_distance_price = abs(float(entry_price) - float(stop_loss))
    sl_pips = price_to_pips(symbol, sl_distance_price)
    if sl_pips <= 0:
        return {"lot_size": 0.0, "sizing_valid": False,
                "method": "rejected_zero_sl",
                "reject_reason": "stop distance is zero — risk undefined"}

    pip_usd = pip_value_usd_per_lot(symbol, account.get("account_type"))
    if pip_usd <= 0:
        return {"lot_size": 0.0, "sizing_valid": False,
                "method": "rejected_zero_pip_value",
                "reject_reason": f"no pip value for {symbol} — risk undefined"}

    payoff_ratio = profile["tp_atr_mult"] / max(profile["sl_atr_mult"], 0.1)
    f = kelly_fraction(
        confidence_pct=confidence_pct,
        min_conf=profile["min_confidence"],
        profile_kelly_cap=profile["kelly_cap"],
        payoff_ratio=payoff_ratio,
    )
    if kelly_enabled and profile["kelly_cap"] > 0:
        effective_risk_pct = profile["risk_pct"] * (f / profile["kelly_cap"])
        method = "kelly"
    else:
        # Fixed fractional risk — Kelly disabled until calibration exists.
        effective_risk_pct = float(profile["risk_pct"])
        method = "fixed_fraction"
    risk_amount_usd = equity * (effective_risk_pct / 100.0)
    lots = risk_amount_usd / (sl_pips * pip_usd)
    lot_size = max(round(lots, 2), 0.01)
    # iter-144 C5 · verify the ACTUAL risk after broker-step rounding and the
    # 0.01 minimum. If the broker minimum forces materially more risk than
    # the budget (e.g. tiny equity, wide stop), REJECT instead of trading a
    # position whose risk was never approved.
    actual_risk_usd = lot_size * sl_pips * pip_usd
    if actual_risk_usd > risk_amount_usd * RISK_OVERSHOOT_TOLERANCE:
        return {
            "lot_size": 0.0, "sizing_valid": False,
            "method": "rejected_min_lot_risk",
            "reject_reason": (
                f"broker-minimum 0.01 lot risks ${actual_risk_usd:.2f} vs the "
                f"${risk_amount_usd:.2f} budget ({effective_risk_pct:.2f}% of "
                f"equity) — stop too wide for this account"),
            "risk_amount_usd": round(risk_amount_usd, 2),
            "actual_risk_usd": round(actual_risk_usd, 2),
            "sl_pips": round(sl_pips, 1), "equity": round(equity, 2),
        }
    return {
        "lot_size": lot_size,
        "sizing_valid": True,
        "risk_amount_usd": round(risk_amount_usd, 2),
        "actual_risk_usd": round(actual_risk_usd, 2),
        "kelly_f": round(f, 4),
        "method": method,
        "effective_risk_pct": round(effective_risk_pct, 3),
        "sl_pips": round(sl_pips, 1),
        "pip_usd_per_lot": round(pip_usd, 4),
        "equity": round(equity, 2),
    }

