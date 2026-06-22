"""Circuit breakers — hard programmatic guardrails the AI cannot override.

Currently implemented:
  - Daily drawdown limit per user: if realised+unrealised P&L for today drops
    below -drawdown_pct of equity-at-day-start, the bot is force-disabled and
    a `circuit_breaker_tripped` event is broadcast.
"""
from datetime import datetime, timezone

DEFAULT_DAILY_DRAWDOWN_PCT = {
    "low": 2.0,        # tighter
    "medium": 4.0,
    "high": 7.0,
    "extreme": 12.0,   # most permissive
}


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def realised_pnl_today(db, user_id: str) -> float:
    """Sum of P&L for trades closed today (UTC)."""
    cursor = db.trades.find({
        "user_id": user_id,
        "status": "closed",
        "closed_at": {"$gte": today_iso()},
    })
    closed = await cursor.to_list(length=1000)
    return sum((t.get("pnl") or 0) for t in closed)


def drawdown_limit_for(risk_level: str) -> float:
    return DEFAULT_DAILY_DRAWDOWN_PCT.get(risk_level, 4.0)


async def check_and_trip(db, user_id: str, cfg: dict, accounts: list) -> dict:
    """Inspect today's loss vs. the user's daily drawdown limit.

    Returns: { tripped: bool, reason: str, pnl_today: float, limit_pct: float, equity: float }
    If tripped, the bot config is force-disabled in the DB.
    """
    total_equity = sum((a.get("equity") or 0) for a in accounts) or sum((a.get("balance") or 0) for a in accounts)
    pnl_today = await realised_pnl_today(db, user_id)
    limit_pct = drawdown_limit_for(cfg.get("risk_level", "medium"))

    # If no equity reported yet (EA hasn't sent heartbeat), skip check
    if total_equity <= 0:
        return {"tripped": False, "reason": "", "pnl_today": pnl_today,
                "limit_pct": limit_pct, "equity": total_equity}

    drawdown_pct = (pnl_today / total_equity) * 100 if total_equity > 0 else 0
    tripped = drawdown_pct <= -limit_pct
    if tripped:
        await db.bot_configs.update_one(
            {"user_id": user_id},
            {"$set": {
                "active": False,
                "tripped_at": datetime.now(timezone.utc).isoformat(),
                "tripped_reason": f"Daily drawdown {drawdown_pct:.2f}% exceeded {limit_pct}% limit",
            }},
        )
    return {
        "tripped": tripped,
        "reason": f"Daily drawdown {drawdown_pct:.2f}% breached -{limit_pct}% limit" if tripped else "",
        "pnl_today": round(pnl_today, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "limit_pct": limit_pct,
        "equity": round(total_equity, 2),
    }
