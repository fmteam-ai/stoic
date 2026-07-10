"""Circuit breakers — hard programmatic guardrails the AI cannot override.

Implemented:
  - Daily drawdown:  realised P&L today (UTC)        vs `daily_drawdown_pct`
  - Weekly drawdown: realised P&L past 7 days (UTC)  vs `weekly_drawdown_pct`

If either breaches the limit, the bot is force-disabled and a
`circuit_breaker_tripped` event is broadcast.
"""
from datetime import datetime, timezone, timedelta

# Conservative defaults — used if the user's bot_config doesn't override
DEFAULT_DAILY_DRAWDOWN_PCT = {
    "low": 2.0,
    "medium": 4.0,
    "high": 7.0,
    "extreme": 12.0,
}
DEFAULT_WEEKLY_DRAWDOWN_PCT = {
    "low": 5.0,
    "medium": 8.0,
    "high": 14.0,
    "extreme": 25.0,
}


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def week_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()


async def realised_pnl_since(db, user_id: str, since_iso: str,
                             account_id: str | None = None) -> float:
    """Sum of P&L for trades closed on or after `since_iso` (UTC date).

    If `account_id` is provided, scope the sum to trades from that account
    only (multi-account isolation — losses on broker A must not trip the
    breaker on broker B).
    """
    q: dict = {
        "user_id": user_id,
        "status": "closed",
        "origin": "auto",  # bot losses only — manual trades must not trip the breaker
        "closed_at": {"$gte": since_iso},
    }
    if account_id:
        q["account_id"] = account_id
    cursor = db.trades.find(q)
    closed = await cursor.to_list(length=2000)
    return sum((t.get("pnl") or 0) for t in closed)


def _daily_limit(cfg: dict) -> float:
    """Resolve daily-drawdown threshold: per-user config wins, else risk-level default."""
    explicit = cfg.get("daily_drawdown_pct")
    if explicit is not None:
        return float(explicit)
    return DEFAULT_DAILY_DRAWDOWN_PCT.get(cfg.get("risk_level", "medium"), 4.0)


def _weekly_limit(cfg: dict) -> float:
    explicit = cfg.get("weekly_drawdown_pct")
    if explicit is not None:
        return float(explicit)
    return DEFAULT_WEEKLY_DRAWDOWN_PCT.get(cfg.get("risk_level", "medium"), 8.0)


async def check_and_trip(db, user_id: str, cfg: dict, accounts: list) -> dict:
    """Inspect today's and the rolling 7-day loss vs the user's limits.

    Multi-account isolation: when `cfg.account_id` is set, the check is
    scoped to that single account's equity AND that account's P&L. Losses
    on a different broker will not trip the breaker for this cfg.

    Returns:
      {
        tripped: bool, reason: str, kind: "daily"|"weekly"|"",
        pnl_today, drawdown_pct, limit_pct,
        pnl_week,  drawdown_week_pct, weekly_limit_pct,
        equity, daily_enabled, weekly_enabled,
      }
    If tripped, the bot config is force-disabled in the DB.
    """
    cfg_account_id = cfg.get("account_id")
    total_equity = sum((a.get("equity") or 0) for a in accounts) or sum((a.get("balance") or 0) for a in accounts)
    pnl_today = await realised_pnl_since(db, user_id, today_iso(), account_id=cfg_account_id)
    pnl_week = await realised_pnl_since(db, user_id, week_ago_iso(), account_id=cfg_account_id)
    daily_limit_pct = _daily_limit(cfg)
    weekly_limit_pct = _weekly_limit(cfg)
    daily_enabled = bool(cfg.get("daily_drawdown_enabled", True))
    weekly_enabled = bool(cfg.get("weekly_drawdown_enabled", True))

    base = {
        "pnl_today": round(pnl_today, 2),
        "pnl_week": round(pnl_week, 2),
        "limit_pct": daily_limit_pct,
        "weekly_limit_pct": weekly_limit_pct,
        "equity": round(total_equity, 2),
        "daily_enabled": daily_enabled,
        "weekly_enabled": weekly_enabled,
    }

    # Can't compute a percentage without equity reported by the EA — skip.
    if total_equity <= 0:
        return {**base, "tripped": False, "reason": "", "kind": "",
                "drawdown_pct": 0.0, "drawdown_week_pct": 0.0}

    drawdown_pct = (pnl_today / total_equity) * 100
    drawdown_week_pct = (pnl_week / total_equity) * 100

    tripped, reason, kind = False, "", ""
    if daily_enabled and drawdown_pct <= -daily_limit_pct:
        tripped, kind = True, "daily"
        reason = f"Daily drawdown {drawdown_pct:.2f}% breached -{daily_limit_pct}% limit"
    elif weekly_enabled and drawdown_week_pct <= -weekly_limit_pct:
        tripped, kind = True, "weekly"
        reason = f"Weekly drawdown {drawdown_week_pct:.2f}% breached -{weekly_limit_pct}% limit (7-day window)"

    if tripped:
        # Disable only the cfg that tripped — per-account override OR default profile.
        cfg_filter: dict = {"user_id": user_id}
        if cfg.get("account_id"):
            cfg_filter["account_id"] = cfg["account_id"]
        else:
            cfg_filter["$or"] = [{"account_id": None}, {"account_id": {"$exists": False}}]
        await db.bot_configs.update_one(
            cfg_filter,
            {"$set": {
                "active": False,
                "tripped_at": datetime.now(timezone.utc).isoformat(),
                "tripped_reason": reason,
                "tripped_kind": kind,
            }},
        )

    return {
        **base,
        "tripped": tripped,
        "reason": reason,
        "kind": kind,
        "drawdown_pct": round(drawdown_pct, 2),
        "drawdown_week_pct": round(drawdown_week_pct, 2),
    }


# Legacy helper kept for any callers/tests that imported it directly.
async def realised_pnl_today(db, user_id: str) -> float:
    return await realised_pnl_since(db, user_id, today_iso())


def drawdown_limit_for(risk_level: str) -> float:
    return DEFAULT_DAILY_DRAWDOWN_PCT.get(risk_level, 4.0)
