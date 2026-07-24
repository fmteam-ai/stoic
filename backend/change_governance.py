"""Autopilot #10 — change governance.

Principle: THE BOT MAY AUTOMATICALLY BECOME MORE CONSERVATIVE.
BECOMING MORE AGGRESSIVE REQUIRES EVIDENCE + USER APPROVAL.

Every bot-proposed config change flows through `propose_change`:
  · conservative (risk-reducing) → auto-applied, recorded in the ledger
  · aggressive (risk-increasing) → queued PENDING for user approval
  · forbidden fields             → rejected outright
Unknown fields default to aggressive (approval required).
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("change-governance")

# field → direction of the SAFER change ("down" = smaller is safer)
SAFER_DIRECTION = {
    "risk_pct": "down",
    "crypto_risk_pct_per_trade": "down",
    "adaptive_risk_cap_pct": "down",
    "adaptive_risk_floor_pct": "down",
    "max_leverage": "down",
    "cvar_budget_pct": "down",
    "event_exposure_cap_pct": "down",
    "daily_drawdown_pct": "down",
    "weekly_drawdown_pct": "down",
    "monthly_drawdown_pct": "down",
    "trade_of_day_cap": "down",
    "payoff_guard_max_sl_tp1": "down",
    "min_confidence_override": "up",
    "min_calibrated_confidence": "up",
    "min_final_rr": "up",
    "loss_cooldown_minutes": "up",
    "signal_cooldown_minutes": "up",
    "consensus_threshold": "up",
    "friday_flat_minutes_before": "up",
}

# boolean field → the SAFE value
SAFER_BOOL = {
    "kelly_enabled": False,
    "friday_flat_enabled": True,
    "loss_cooldown_enabled": True,
    "payoff_guard_enabled": True,
    "risk_engine_enabled": True,
    "regime_gating_enabled": True,
    "session_trend_gate_enabled": True,
    "exhaustion_gate_enabled": True,
    "daily_drawdown_enabled": True,
    "weekly_drawdown_enabled": True,
    "monthly_drawdown_enabled": True,
    "adaptive_sizing_enabled": True,
}

# never bot-changeable, even with approval queued
FORBIDDEN_FIELDS = {"bridge_token", "broker", "mode", "account_type",
                    "user_id", "account_id"}

# operational mode ranks — lower = safer (autopilot #15)
MODE_RANK = {"panic": 0, "defensive": 1, "observe": 2, "shadow": 3,
             "demo_autopilot": 4, "supervised_live": 5,
             "autonomous_live": 6}


def classify_change(field: str, old, new) -> str:
    """→ 'conservative' | 'aggressive' | 'forbidden'."""
    if field in FORBIDDEN_FIELDS:
        return "forbidden"
    if field == "operational_mode":
        old_r = MODE_RANK.get(str(old or "autonomous_live"))
        new_r = MODE_RANK.get(str(new))
        if new_r is None or old_r is None:
            return "aggressive"
        return "conservative" if new_r <= old_r else "aggressive"
    if field in SAFER_BOOL:
        return "conservative" if bool(new) == SAFER_BOOL[field] else "aggressive"
    direction = SAFER_DIRECTION.get(field)
    if direction is None:
        return "aggressive"  # unknown knob = approval required by principle
    try:
        old_v, new_v = float(old), float(new)
    except (TypeError, ValueError):
        return "aggressive"
    if new_v == old_v:
        return "conservative"  # no-op is harmless
    moved_down = new_v < old_v
    return ("conservative"
            if (direction == "down") == moved_down else "aggressive")


async def _apply(db, user_id: str, field: str, value,
                 account_id: str | None) -> int:
    q = {"user_id": user_id, "active": True}
    if account_id:
        q["account_id"] = account_id
    res = await db.bot_configs.update_many(q, {"$set": {field: value}})
    return res.modified_count


async def propose_change(db, user_id: str, field: str, old, new,
                         source: str, evidence=None,
                         account_id: str | None = None) -> dict:
    cls = classify_change(field, old, new)
    doc = {"user_id": user_id, "account_id": account_id,
           "field": field, "old_value": old, "new_value": new,
           "classification": cls, "source": source, "evidence": evidence,
           "proposed_at": datetime.now(timezone.utc).isoformat()}
    if cls == "forbidden":
        doc["status"] = "rejected"
        doc["detail"] = "field is never bot-changeable"
    elif cls == "conservative":
        doc["status"] = "auto_applied"
        doc["updated_configs"] = await _apply(db, user_id, field, new,
                                              account_id)
        doc["applied_at"] = doc["proposed_at"]
    else:
        doc["status"] = "pending"
        doc["detail"] = ("risk-increasing change — requires user approval "
                         "with evidence")
    res = await db.governed_changes.insert_one(doc)
    doc["_id"] = res.inserted_id
    logger.info("governance %s %s: %s → %s (%s)", doc["status"], field,
                old, new, source)
    return doc


async def record_auto_applied(db, user_id: str, field: str, old, new,
                              source: str, evidence=None,
                              detail: str | None = None) -> None:
    """Ledger entry for a conservative change applied by an existing
    auto-learning path (guards, friday_flat, auto-heal)."""
    now = datetime.now(timezone.utc).isoformat()
    await db.governed_changes.insert_one(
        {"user_id": user_id, "account_id": None, "field": field,
         "old_value": old, "new_value": new,
         "classification": "conservative", "status": "auto_applied",
         "source": source, "evidence": evidence, "detail": detail,
         "proposed_at": now, "applied_at": now})


async def resolve_change(db, user_id: str, change_id, approve: bool) -> dict:
    from bson import ObjectId
    doc = await db.governed_changes.find_one(
        {"_id": ObjectId(str(change_id)), "user_id": user_id})
    if not doc:
        return {"ok": False, "error": "change not found"}
    if doc.get("status") != "pending":
        return {"ok": False, "error": f"change is {doc.get('status')}, not pending"}
    now = datetime.now(timezone.utc).isoformat()
    if approve:
        n = await _apply(db, user_id, doc["field"], doc["new_value"],
                         doc.get("account_id"))
        await db.governed_changes.update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "approved", "applied_at": now,
                      "updated_configs": n}})
        return {"ok": True, "status": "approved", "updated_configs": n}
    await db.governed_changes.update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "rejected",
                                       "resolved_at": now}})
    return {"ok": True, "status": "rejected"}
