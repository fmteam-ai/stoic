"""PAMM Risk Profiles (v62.1) — Strategy and Risk Profile are SEPARATE
axes: "Nitro + Conservative" differs from "Nitro + Growth" without two
Nitro algorithms existing.

Risk hierarchy invariant: nothing below the PAMM risk envelope can
INCREASE risk — conflicting limits always resolve to the STRICTEST."""
from datetime import datetime, timezone

DEFAULT_PROFILES = [
    {"risk_profile_id": "conservative", "display_name": "Conservative",
     "max_risk_per_trade": 0.25, "max_daily_loss": 1.0,
     "max_weekly_loss": 2.5, "max_drawdown": 5.0,
     "max_open_positions": 2, "max_symbol_exposure": 0.5,
     "max_factor_exposure": 1.0, "max_consecutive_losses": 3,
     "allowed_symbols": None,
     "spread_limits": {"max_spread_pips": 1.5},
     "slippage_limits": {"max_slippage_pips": 0.5},
     "emergency_stop_rules": {"daily_loss_pct": 1.0}},
    {"risk_profile_id": "controlled", "display_name": "Controlled",
     "max_risk_per_trade": 0.5, "max_daily_loss": 2.0,
     "max_weekly_loss": 5.0, "max_drawdown": 10.0,
     "max_open_positions": 4, "max_symbol_exposure": 1.0,
     "max_factor_exposure": 2.0, "max_consecutive_losses": 4,
     "allowed_symbols": None,
     "spread_limits": {"max_spread_pips": 2.0},
     "slippage_limits": {"max_slippage_pips": 1.0},
     "emergency_stop_rules": {"daily_loss_pct": 2.0}},
    {"risk_profile_id": "growth", "display_name": "Growth",
     "max_risk_per_trade": 1.0, "max_daily_loss": 3.0,
     "max_weekly_loss": 7.5, "max_drawdown": 15.0,
     "max_open_positions": 6, "max_symbol_exposure": 2.0,
     "max_factor_exposure": 3.0, "max_consecutive_losses": 5,
     "allowed_symbols": None,
     "spread_limits": {"max_spread_pips": 2.5},
     "slippage_limits": {"max_slippage_pips": 1.5},
     "emergency_stop_rules": {"daily_loss_pct": 3.0}},
]


def strictest_limit(*limits) -> float | None:
    """The risk hierarchy NEVER averages — the strictest wins.
    e.g. Nitro 0.40 / Portfolio 0.30 / PAMM 0.20 → 0.20."""
    vals = [float(v) for v in limits if v is not None]
    return min(vals) if vals else None


def effective_envelope(profile: dict | None,
                       program_limits: dict | None) -> dict:
    """v62.4 — ONE Effective Risk Envelope: PAMM program limits and the
    strategy risk profile merged with strictest_limit (never averaged)."""
    p = profile or {}
    pl = program_limits or {}

    def _thr(key):
        lim = pl.get(key) or {}
        return lim.get("threshold") if lim.get("enabled") else None

    return {
        "max_risk_per_trade": strictest_limit(p.get("max_risk_per_trade")),
        "max_daily_loss_pct": strictest_limit(p.get("max_daily_loss"),
                                              _thr("daily_loss_pct")),
        "max_weekly_loss_pct": strictest_limit(p.get("max_weekly_loss"),
                                               _thr("weekly_loss_pct")),
        "max_drawdown_pct": strictest_limit(p.get("max_drawdown"),
                                            _thr("max_drawdown_pct")),
        "max_open_positions": strictest_limit(p.get("max_open_positions")),
        "max_symbol_exposure_lots": strictest_limit(
            p.get("max_symbol_exposure")),
        "max_factor_exposure_lots": strictest_limit(
            p.get("max_factor_exposure")),
        "max_consecutive_losses": strictest_limit(
            p.get("max_consecutive_losses")),
        "allowed_symbols": p.get("allowed_symbols"),
        "max_spread_pips": (p.get("spread_limits")
                            or {}).get("max_spread_pips"),
        "max_slippage_pips": (p.get("slippage_limits")
                              or {}).get("max_slippage_pips"),
    }


async def ensure_profiles(db) -> None:
    for p in DEFAULT_PROFILES:
        await db.pamm_risk_profiles.update_one(
            {"risk_profile_id": p["risk_profile_id"]},
            {"$setOnInsert": {**p, "created_at":
                              datetime.now(timezone.utc).isoformat()}},
            upsert=True)


async def list_profiles(db) -> list:
    await ensure_profiles(db)
    return [p async for p in db.pamm_risk_profiles.find(
        {}, {"_id": 0}).sort("max_risk_per_trade", 1)]


async def get_profile(db, risk_profile_id: str) -> dict | None:
    await ensure_profiles(db)
    return await db.pamm_risk_profiles.find_one(
        {"risk_profile_id": risk_profile_id}, {"_id": 0})
