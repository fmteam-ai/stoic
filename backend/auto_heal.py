"""Auto-heal — non-destructive self-healing pass that keeps Bot Health ≥ 90.

The scheduler in `server.py` calls `sweep_user(user_id)` on a 5-minute cadence
(when the user has opted in via `users.auto_heal_settings.enabled=true`).
Every action is idempotent + reversible + logged to `auto_heal_actions`.

Safe set (never touches money flow):
  • Disable `aggressive_mode` after a loss spike (daily PnL ≤ -2% of equity)
  • Raise `min_confidence_override` +5 (clamp 95) when a loss pattern recurs ≥3×
  • Trade reconcile when DB ↔ broker drift detected
  • Clear stale Bot Pulse (>1 h)

NEVER does:
  • Enable / disable the bot
  • Close or open trades
  • Change risk profile / position sizing
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from bson import ObjectId

from database import get_db

logger = logging.getLogger("auto-heal")


async def _user_doc(db, user_id: str) -> Optional[dict]:
    try:
        d = await db.users.find_one({"_id": ObjectId(user_id)})
        if d:
            return d
    except Exception:
        pass
    return (await db.users.find_one({"_id": user_id})
            or await db.users.find_one({"id": user_id}))


async def _log_action(db, user_id: str, kind: str, detail: dict):
    await db.auto_heal_actions.insert_one({
        "user_id": user_id,
        "kind": kind,
        "detail": detail,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def _check_aggressive_mode(db, user_id: str) -> list[dict]:
    """If any cfg has aggressive_mode=true AND today's PnL is deeply red,
    flip it off as a protective measure.

    The threshold is conservative — daily PnL ≤ -2% of equity. This catches
    the iter-48 bias-trap pattern without flapping on day-to-day noise.
    """
    actions = []
    cfgs = await db.bot_configs.find({"user_id": user_id, "aggressive_mode": True}).to_list(length=20)
    if not cfgs:
        return actions

    # Daily PnL — sum today's closed trades
    today_iso = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    daily_pnl = 0.0
    async for t in db.trades.find({
        "user_id": user_id, "status": "closed",
        "closed_at": {"$gte": today_iso},
    }):
        daily_pnl += float(t.get("pnl") or 0)

    # Reference equity — use first account's equity (best-effort)
    acct = await db.accounts.find_one({"user_id": user_id})
    equity = float((acct or {}).get("equity") or 0)
    pnl_pct = (daily_pnl / equity * 100) if equity > 0 else 0.0

    if pnl_pct > -2.0:
        return actions  # not severe enough — leave aggressive_mode alone

    for cfg in cfgs:
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {"aggressive_mode": False,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        actions.append({
            "kind": "disable_aggressive_mode",
            "config_id": str(cfg["_id"]),
            "trigger": f"daily PnL {pnl_pct:.2f}% ≤ -2.0%",
        })
    if actions:
        await _log_action(db, user_id, "disable_aggressive_mode", {
            "configs": [a["config_id"] for a in actions],
            "daily_pnl": round(daily_pnl, 2),
            "daily_pnl_pct": round(pnl_pct, 2),
        })
    return actions


async def _check_recurring_loss_pattern(db, user_id: str) -> list[dict]:
    """When a loss pattern recurs ≥3× in 30 days, raise min_confidence_override
    by +5 (clamped at 95). Same effect as the existing Loss-Lab auto-tighten,
    but auto-heal runs it regardless of the `auto_tighten_enabled` flag — this
    is the safety net of last resort.
    """
    actions = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    pipeline = [
        {"$match": {"user_id": user_id, "created_at": {"$gte": cutoff}}},
        {"$group": {"_id": "$pattern_key", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gte": 3}}},
    ]
    pattern_rows = []
    async for row in db.loss_postmortems.aggregate(pipeline):
        pattern_rows.append(row)
    if not pattern_rows:
        return actions

    # 7-day per-pattern cooldown to avoid ratcheting
    cooldown = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    for row in pattern_rows:
        pattern = row["_id"]
        cnt = row["count"]
        recent_adj = await db.guardrail_adjustments.find_one({
            "user_id": user_id, "pattern_key": pattern,
            "created_at": {"$gte": cooldown},
        })
        if recent_adj:
            continue

        # Default config (account_id=None) is the conservative target.
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": None})
        if not cfg:
            continue
        cur = int(cfg.get("min_confidence_override") or 60)
        new = min(95, cur + 5)
        if new <= cur:
            continue

        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {"min_confidence_override": new,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        adj_doc = {
            "user_id": user_id, "config_id": str(cfg["_id"]),
            "pattern_key": pattern, "direction": "tighten",
            "field": "min_confidence_override",
            "from": cur, "to": new,
            "trigger_count": cnt, "trigger_kind": "auto_heal_pattern",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.guardrail_adjustments.insert_one(adj_doc)
        actions.append({
            "kind": "tighten_min_confidence",
            "pattern": pattern, "count": cnt, "from": cur, "to": new,
        })
        await _log_action(db, user_id, "tighten_min_confidence", {
            "pattern": pattern, "count": cnt, "from": cur, "to": new,
        })
    return actions


async def _check_trade_sync_drift(db, user_id: str) -> list[dict]:
    """Run reconciler if the local DB shows open trades that the broker no
    longer reports. Cheap call — the reconciler short-circuits if nothing
    needs fixing.
    """
    open_count = await db.trades.count_documents({
        "user_id": user_id, "status": "open",
    })
    if open_count == 0:
        return []
    try:
        from trade_reconciler import reconcile_user
        result = await reconcile_user(user_id)
        total_closed = (result or {}).get("total_closed", 0)
        if total_closed > 0:
            await _log_action(db, user_id, "reconcile_drift", {"closed": total_closed})
            return [{"kind": "reconcile_drift", "closed": total_closed}]
    except Exception as e:  # noqa: BLE001
        logger.warning("auto-heal reconcile failed for user=%s: %s", user_id, e)
    return []


async def _clear_stale_pulses(db, user_id: str) -> list[dict]:
    """A pulse older than 1 hour means the cfg's runner hasn't ticked — clear
    it so the UI shows the actual "Waiting for first cycle…" state instead of
    a stale verdict. Pure UX cleanup, no risk surface.
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res = await db.bot_configs.update_many(
        {"user_id": user_id, "_last_pulse.ts": {"$lt": cutoff_iso}},
        {"$unset": {"_last_pulse": ""}},
    )
    if res.modified_count > 0:
        await _log_action(db, user_id, "clear_stale_pulses", {"count": res.modified_count})
        return [{"kind": "clear_stale_pulses", "count": res.modified_count}]
    return []


async def sweep_user(user_id: str) -> dict:
    """Run all safe self-healing checks for one user. Returns a summary."""
    db = get_db()
    user = await _user_doc(db, user_id)
    if not user:
        return {"ok": False, "reason": "user not found"}
    settings = (user.get("auto_heal_settings") or {})
    if not settings.get("enabled"):
        return {"ok": True, "skipped": "auto_heal disabled"}

    all_actions = []
    for fn in (
        _check_aggressive_mode,
        _check_recurring_loss_pattern,
        _check_trade_sync_drift,
        _clear_stale_pulses,
    ):
        try:
            all_actions.extend(await fn(db, user_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-heal check %s failed for user=%s: %s", fn.__name__, user_id, e)

    return {
        "ok": True,
        "actions_taken": len(all_actions),
        "actions": all_actions,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


async def sweep_all_users() -> dict:
    """Background scheduler hook — iterate every user with auto_heal=enabled."""
    db = get_db()
    cursor = db.users.find({"auto_heal_settings.enabled": True})
    users = await cursor.to_list(length=500)
    summary = []
    for u in users:
        try:
            uid = str(u.get("_id"))
            result = await sweep_user(uid)
            if result.get("actions_taken", 0) > 0:
                summary.append({"user_id": uid, **result})
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-heal sweep failed user=%s: %s", u.get("_id"), e)
    return {"users_swept": len(users), "users_with_actions": len(summary), "details": summary}
