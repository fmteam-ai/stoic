"""iter-58 · Loss Cooldown guardrail.

After ANY trade closes at a loss, block NEW trades in the same
symbol+direction for `loss_cooldown_minutes` (default 30) across ALL of the
user's accounts. The existing anti-tilt freeze is per-account and needs 2
losses per account before it bites — with 5 correlated accounts one wrong
directional read became 13 clustered losses in under an hour (2026-07-07).

Shadow-tested on the user's real trades: last 14 days -$174 → +$7,429
(blocks 159 clustered re-entries carrying net -$7,603)."""
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
    rows = await db.trades.find({
        "user_id": user_id, "status": "closed", "action": action,
        "pnl": {"$lt": 0}, "closed_at": {"$gte": cutoff},
    }).sort("closed_at", -1).limit(25).to_list(length=25)
    base = base_symbol(symbol)
    for r in rows:
        if base_symbol(r.get("symbol")) != base:
            continue
        try:
            closed = datetime.fromisoformat(str(r.get("closed_at")))
            left = minutes - (now - closed).total_seconds() / 60.0
        except (ValueError, TypeError):
            left = minutes
        return (f"Loss cooldown: a {action} on {base} closed at a loss "
                f"(${float(r.get('pnl') or 0):.2f}) {minutes - left:.0f}min ago — "
                f"same-direction re-entries paused across all accounts for "
                f"{max(left, 0):.0f} more min.")
    return None
