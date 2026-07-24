"""Autopilot #10 — change governance.

Principle: THE BOT MAY AUTOMATICALLY BECOME MORE CONSERVATIVE.
BECOMING MORE AGGRESSIVE REQUIRES EVIDENCE + USER APPROVAL.

Every bot-proposed config change flows through `propose_change`:
  · conservative (risk-reducing) → auto-applied, recorded in the ledger
  · aggressive (risk-increasing) → queued PENDING for user approval
  · forbidden fields             → rejected outright
Unknown fields default to aggressive (approval required).
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("change-governance")

SECOND_APPROVAL_COOLING_MINUTES = 15
MIN_REASON_LEN = 10

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


def material_change(field: str, old, new) -> bool:
    """Material risk-increases need dual approval (second sign-off after a
    cooling period): hard drawdown/leverage widenings, Kelly activation,
    full-autonomy promotion, or any ≥1.5× numeric increase."""
    if field in {"daily_drawdown_pct", "weekly_drawdown_pct",
                 "monthly_drawdown_pct", "max_leverage"}:
        return True
    if field == "kelly_enabled" and bool(new):
        return True
    if field == "operational_mode" and str(new) == "autonomous_live":
        return True
    try:
        old_v, new_v = float(old), float(new)
        if old_v > 0 and new_v >= 1.5 * old_v:
            return True
    except (TypeError, ValueError):
        pass
    return False


async def snapshot_config(db, user_id: str, account_id: str | None,
                          label: str) -> str:
    """Immutable configuration version — full config document + content hash,
    written BEFORE any governed mutation touches the active config."""
    q = {"user_id": user_id, "active": True}
    if account_id:
        q["account_id"] = account_id
    cfg = await db.bot_configs.find_one(q) or {}
    body = {k: v for k, v in cfg.items() if k != "_id"}
    canonical = json.dumps(body, sort_keys=True, default=str,
                           separators=(",", ":"))
    doc = {"user_id": user_id, "account_id": account_id, "label": label,
           "config": body,
           "config_hash": hashlib.sha256(canonical.encode()).hexdigest(),
           "created_at": datetime.now(timezone.utc)}
    res = await db.config_versions.insert_one(doc)
    return str(res.inserted_id)


def _risk_impact(field: str, old, new) -> dict:
    impact = {"field": field, "before": old, "after": new}
    cls = classify_change(field, old, new)
    impact["direction"] = ("risk_reducing" if cls == "conservative"
                           else "risk_increasing")
    try:
        old_v, new_v = float(old), float(new)
        if old_v:
            impact["relative_change_pct"] = round(
                (new_v - old_v) / abs(old_v) * 100, 1)
    except (TypeError, ValueError):
        pass
    impact["material"] = material_change(field, old, new)
    return impact


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
    now = datetime.now(timezone.utc)
    doc = {"user_id": user_id, "account_id": account_id,
           "field": field, "old_value": old, "new_value": new,
           "classification": cls, "source": source, "evidence": evidence,
           "risk_impact": _risk_impact(field, old, new),
           "proposed_at": now}
    if cls == "forbidden":
        doc["status"] = "rejected"
        doc["detail"] = "field is never bot-changeable"
    elif cls == "conservative":
        doc["config_version_before"] = await snapshot_config(
            db, user_id, account_id, f"pre-auto-apply:{field}")
        doc["status"] = "auto_applied"
        doc["updated_configs"] = await _apply(db, user_id, field, new,
                                              account_id)
        doc["applied_at"] = now
    else:
        doc["status"] = "pending"
        doc["detail"] = ("risk-increasing change — requires step-up MFA, a "
                         "reason and user approval"
                         + (" (MATERIAL: dual approval with "
                            f"{SECOND_APPROVAL_COOLING_MINUTES}min cooling)"
                            if doc["risk_impact"]["material"] else ""))
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
    now = datetime.now(timezone.utc)
    await db.governed_changes.insert_one(
        {"user_id": user_id, "account_id": None, "field": field,
         "old_value": old, "new_value": new,
         "classification": "conservative", "status": "auto_applied",
         "source": source, "evidence": evidence, "detail": detail,
         "proposed_at": now, "applied_at": now})


async def resolve_change(db, user_id: str, change_id, approve: bool,
                         reason: str | None = None) -> dict:
    """Approve/reject a pending risk-increase. Approvals require a reason;
    MATERIAL changes require a second approval ≥15 minutes after the first
    (both with fresh step-up MFA at the route layer). Idempotent — resolved
    changes refuse re-resolution."""
    from bson import ObjectId
    doc = await db.governed_changes.find_one(
        {"_id": ObjectId(str(change_id)), "user_id": user_id})
    if not doc:
        return {"ok": False, "error": "change not found"}
    status = doc.get("status")
    if status not in ("pending", "pending_second_approval"):
        return {"ok": False, "error": f"change is {status}, not pending"}
    now = datetime.now(timezone.utc)
    if not approve:
        await db.governed_changes.update_one(
            {"_id": doc["_id"], "status": status},
            {"$set": {"status": "rejected", "resolved_at": now,
                      "reject_reason": reason}})
        return {"ok": True, "status": "rejected"}

    if not reason or len(reason.strip()) < MIN_REASON_LEN:
        return {"ok": False, "error": f"approval requires a reason "
                                      f"(≥{MIN_REASON_LEN} characters)"}
    material = (doc.get("risk_impact")
                or _risk_impact(doc["field"], doc["old_value"],
                                doc["new_value"]))["material"]
    if material and status == "pending":
        # first of two sign-offs — start the cooling period
        r = await db.governed_changes.update_one(
            {"_id": doc["_id"], "status": "pending"},
            {"$set": {"status": "pending_second_approval",
                      "first_approval": {"at": now, "reason": reason.strip()}}})
        if not r.modified_count:  # concurrent resolution — idempotency
            return {"ok": False, "error": "change was resolved concurrently"}
        eligible = now + timedelta(minutes=SECOND_APPROVAL_COOLING_MINUTES)
        return {"ok": True, "status": "pending_second_approval",
                "detail": f"material change — second approval required after "
                          f"a {SECOND_APPROVAL_COOLING_MINUTES}min cooling period",
                "second_approval_eligible_at": eligible.isoformat()}
    if status == "pending_second_approval":
        first_at = (doc.get("first_approval") or {}).get("at")
        if first_at is not None and not isinstance(first_at, datetime):
            first_at = datetime.fromisoformat(str(first_at))
        if first_at and first_at.tzinfo is None:
            first_at = first_at.replace(tzinfo=timezone.utc)
        waited = (now - first_at).total_seconds() if first_at else 0
        if waited < SECOND_APPROVAL_COOLING_MINUTES * 60:
            remaining = int(SECOND_APPROVAL_COOLING_MINUTES * 60 - waited)
            return {"ok": False,
                    "error": f"cooling period active — second approval "
                             f"possible in {remaining}s"}

    version_before = await snapshot_config(
        db, user_id, doc.get("account_id"), f"pre-approval:{doc['field']}")
    n = await _apply(db, user_id, doc["field"], doc["new_value"],
                     doc.get("account_id"))
    version_after = await snapshot_config(
        db, user_id, doc.get("account_id"), f"post-approval:{doc['field']}")
    r = await db.governed_changes.update_one(
        {"_id": doc["_id"], "status": status},
        {"$set": {"status": "approved", "applied_at": now,
                  "updated_configs": n,
                  "approval_reason": reason.strip(),
                  "config_version_before": version_before,
                  "config_version_after": version_after}})
    if not r.modified_count:
        return {"ok": False, "error": "change was resolved concurrently"}
    return {"ok": True, "status": "approved", "updated_configs": n,
            "config_version_before": version_before,
            "config_version_after": version_after}
