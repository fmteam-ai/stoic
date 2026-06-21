"""Risk management profiles.

Defines lot sizing, stop-loss / take-profit distances per risk tier, and
confidence thresholds required to act on a signal.
"""
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "extreme"]

# Default profiles tuned for MT5 microcent accounts.
# `risk_pct` = percentage of account equity risked per trade.
# `sl_atr_mult` / `tp_atr_mult` = stop / target distance multiplier (vs. recent volatility).
# `min_confidence` = AI confidence threshold below which trades are skipped.
PROFILES = {
    "low": {
        "label": "Low",
        "risk_pct": 0.5,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 2.0,
        "min_confidence": 75,
        "max_concurrent": 1,
        "leverage_cap": 10,
    },
    "medium": {
        "label": "Medium",
        "risk_pct": 1.0,
        "sl_atr_mult": 1.5,
        "tp_atr_mult": 2.5,
        "min_confidence": 65,
        "max_concurrent": 2,
        "leverage_cap": 30,
    },
    "high": {
        "label": "High",
        "risk_pct": 2.5,
        "sl_atr_mult": 1.2,
        "tp_atr_mult": 3.0,
        "min_confidence": 55,
        "max_concurrent": 4,
        "leverage_cap": 100,
    },
    "extreme": {
        "label": "Extreme",
        "risk_pct": 5.0,
        "sl_atr_mult": 1.0,
        "tp_atr_mult": 4.0,
        "min_confidence": 45,
        "max_concurrent": 6,
        "leverage_cap": 500,
    },
}


def get_profile(level: str) -> dict:
    return PROFILES.get(level, PROFILES["medium"])


def compute_position_size(equity: float, risk_pct: float, sl_pips: float,
                          pip_value: float = 1.0) -> float:
    """Compute lot size from equity and stop distance.

    Microcent accounts: pip value is tiny so lot size scales up.
    Returns lots rounded to 2 decimals (minimum 0.01).
    """
    if sl_pips <= 0 or pip_value <= 0:
        return 0.01
    risk_amount = equity * (risk_pct / 100.0)
    lots = risk_amount / (sl_pips * pip_value)
    return max(round(lots, 2), 0.01)


def derive_sl_tp(action: str, entry: float, atr: float, profile: dict) -> tuple:
    """Compute SL/TP prices from entry, ATR proxy, and profile multipliers."""
    sl_dist = atr * profile["sl_atr_mult"]
    tp_dist = atr * profile["tp_atr_mult"]
    if action == "BUY":
        return round(entry - sl_dist, 5), round(entry + tp_dist, 5)
    elif action == "SELL":
        return round(entry + sl_dist, 5), round(entry - tp_dist, 5)
    return entry, entry
