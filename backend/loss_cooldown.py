"""iter-58 · Loss Cooldown guardrail (iter-122b: per-account).

After a BOT trade closes at a loss, block NEW trades in the same
symbol+direction for `loss_cooldown_minutes` (default 30) on THAT account.
Each account has its own equity and settings, so a loss on one broker must
not pause the others."""
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol

DEFAULT_LOSS_COOLDOWN_MIN = 30


async def loss_cooldown_block(db, user_id: str, symbol: str, action: str,
                              cfg: dict) -> str | None:
    if not cfg.get("loss_cooldown_enabled", True):
        return None
    try:
        raw = cfg.get("loss_cooldown_minutes")
        minutes = DEFAULT_LOSS_COOLDOWN_MIN if raw is None else float(raw)
    except (TypeError, ValueError):
        minutes = DEFAULT_LOSS_COOLDOWN_MIN
    if minutes <= 0:
        return None
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=minutes)).isoformat()
    q = {
        "user_id": user_id, "status": "closed", "action": action,
        "origin": "auto",  # bot losses only — manual trades must not freeze the bot
        "pnl": {"$lt": 0}, "closed_at": {"$gte": cutoff},
        "stats_excluded": {"$ne": True},   # late fills of expired/PANIC orders are not strategy outcomes
    }
    account_id = cfg.get("account_id")
    if account_id:
        q["account_id"] = account_id
    rows = await db.trades.find(q).sort("closed_at", -1).limit(25).to_list(length=25)
    base = base_symbol(symbol)
    for r in rows:
        if base_symbol(r.get("symbol")) != base:
            continue
        try:
            closed = datetime.fromisoformat(str(r.get("closed_at")))
            left = minutes - (now - closed).total_seconds() / 60.0
        except (ValueError, TypeError):
            left = minutes
        return (f"Loss cooldown: a bot {action} on {base} closed at a loss "
                f"(${float(r.get('pnl') or 0):.2f}) {minutes - left:.0f}min ago — "
                f"same-direction re-entries paused on this account for "
                f"{max(left, 0):.0f} more min.")
    return None
