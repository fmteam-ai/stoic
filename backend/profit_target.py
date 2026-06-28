"""Daily profit target with lock-profit semantics — iter-65.

Upside mirror of the daily-drawdown circuit breaker. Once today's realised
P&L crosses the user's target (expressed in R-multiples of their base
risk-per-trade), the bot enters one of two modes:

  • "lock"  — DEFAULT. Profit is locked: subsequent position sizing uses
              `equity - locked_amount` so the locked $ literally cannot be
              lost on subsequent trades. Bot keeps trading.
  • "stop"  — Auto-execute is disabled for the rest of the UTC day (resets
              at 00:00 UTC like the existing drawdown breaker).

Per-account isolated: a 5R target on RoboForex doesn't affect VT Markets.
Resets at 00:00 UTC daily.
"""
from datetime import datetime, timezone
from typing import Optional

from circuit_breakers import realised_pnl_since, today_iso
from risk import get_profile


def _r_dollar_value(equity: float, profile: dict) -> float:
    """1R = `risk_per_trade_pct × equity`. Matches the R used everywhere else
    in STOIC (Kelly sizing, breakeven trigger, partial-close trigger)."""
    if equity <= 0:
        return 0.0
    return equity * (float(profile.get("risk_pct", 1.0)) / 100.0)


async def evaluate_profit_target(
    db, user_id: str, cfg: dict, accounts: list,
) -> dict:
    """Check today's realised P&L against the daily profit target.

    Returns a dict the bot_runner / UI can act on:
        {
          "enabled": bool,
          "mode": "lock" | "stop",
          "target_r": float, "target_amount": float,
          "current_pnl": float, "r_dollar_value": float,
          "hit": bool,
          "locked_amount": float,        # = current_pnl when hit AND mode="lock"
          "should_stop": bool,           # True when hit AND mode="stop"
        }
    """
    target_r = float(cfg.get("daily_profit_target_r") or 0.0)
    mode = (cfg.get("daily_profit_target_action") or "lock").lower()
    if mode not in ("lock", "stop"):
        mode = "lock"

    base = {
        "enabled": target_r > 0,
        "mode": mode,
        "target_r": target_r,
        "target_amount": 0.0,
        "current_pnl": 0.0,
        "r_dollar_value": 0.0,
        "hit": False,
        "locked_amount": 0.0,
        "should_stop": False,
    }
    if target_r <= 0:
        return base

    # Per-account scoping — matches the drawdown breaker.
    cfg_account_id = cfg.get("account_id")
    if cfg_account_id:
        equity = next(
            (float(a.get("equity") or a.get("balance") or 0)
             for a in accounts if str(a.get("_id")) == cfg_account_id),
            0.0,
        )
    else:
        equity = sum(
            float(a.get("equity") or a.get("balance") or 0) for a in accounts
        )

    profile = get_profile(cfg.get("risk_level", "medium"))
    r_dollar = _r_dollar_value(equity, profile)
    target_amount = target_r * r_dollar
    pnl_today = await realised_pnl_since(
        db, user_id, today_iso(), account_id=cfg_account_id
    )
    hit = target_amount > 0 and pnl_today >= target_amount

    return {
        **base,
        "target_amount": round(target_amount, 2),
        "current_pnl": round(pnl_today, 2),
        "r_dollar_value": round(r_dollar, 2),
        "hit": hit,
        "locked_amount": round(pnl_today, 2) if (hit and mode == "lock") else 0.0,
        "should_stop": hit and mode == "stop",
    }


def _today_lock_field(cfg: dict) -> dict:
    """Read the per-day lock-state stored on the cfg doc."""
    pl = cfg.get("_profit_lock") or {}
    if pl.get("date") != today_iso():
        return {"date": today_iso(), "amount": 0.0, "triggered_at": None}
    return pl


async def apply_profit_target(
    db, user_id: str, cfg: dict, accounts: list,
) -> Optional[dict]:
    """Persist the target-hit state to the cfg doc.

    Called once per bot_runner cycle (after check_and_trip). Returns the
    evaluation result so the caller can broadcast a websocket event.

    Idempotent: re-running on the same day doesn't change anything if the
    target was already triggered.
    """
    result = await evaluate_profit_target(db, user_id, cfg, accounts)
    if not result["enabled"] or not result["hit"]:
        return result

    cfg_filter: dict = {"user_id": user_id}
    if cfg.get("account_id"):
        cfg_filter["account_id"] = cfg["account_id"]
    else:
        cfg_filter["$or"] = [
            {"account_id": None}, {"account_id": {"$exists": False}}
        ]

    lock_state = _today_lock_field(cfg)
    update: dict = {}

    if result["mode"] == "lock":
        # Always update the locked amount to the latest realised P&L — this
        # way the locked $ tracks UP if more wins come in (you never UNLOCK).
        new_amount = max(lock_state.get("amount", 0.0), result["locked_amount"])
        update["_profit_lock"] = {
            "date": today_iso(),
            "amount": round(new_amount, 2),
            "triggered_at": lock_state.get("triggered_at") or datetime.now(timezone.utc).isoformat(),
            "mode": "lock",
        }
    elif result["mode"] == "stop" and cfg.get("active"):
        # One-shot stop: disable auto-execute for the rest of the UTC day.
        update.update({
            "active": False,
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "stopped_reason": (
                f"Daily profit target hit: +${result['current_pnl']:.2f} "
                f"≥ {result['target_r']}R (${result['target_amount']:.2f})"
            ),
            "stopped_kind": "profit_target",
            "_profit_lock": {
                "date": today_iso(),
                "amount": round(result["current_pnl"], 2),
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "mode": "stop",
            },
        })

    if update:
        await db.bot_configs.update_one(cfg_filter, {"$set": update})

    return result


def locked_profit_amount(cfg: dict) -> float:
    """Return today's locked-profit amount in $ (0 if none / different day).

    Position sizing reads this to compute `equity - locked` before Kelly.
    """
    pl = cfg.get("_profit_lock") or {}
    if pl.get("date") != today_iso():
        return 0.0
    return float(pl.get("amount") or 0.0)
