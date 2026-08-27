"""PAMM dual authorization (review §12) — two-person approval for critical
changes: loosening risk limits, unlocking LOCKED, master/broker changes.
Requester and approver MUST be different admins; requests expire in 48h."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pamm.dualauth")

CRITICAL_KINDS = {"risk_limits_increase", "unlock",
                  "master_account_change", "broker_change",
                  "strategy_replacement", "strategy_version_promotion",
                  "leverage_cap_increase", "max_aum_increase",
                  "fee_change", "withdrawal_rules_change",
                  "allocation_method_change", "drift_tolerance_increase"}
EXPIRY_HOURS = 48


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_change_request(db, program: dict, kind: str, payload: dict,
                                requester_id: str,
                                reason: str = "") -> dict:
    if kind not in CRITICAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(CRITICAL_KINDS)}")
    dup = await db.pamm_change_requests.find_one(
        {"program_id": program["program_id"], "kind": kind,
         "status": "pending"}, {"_id": 1})
    if dup:
        raise ValueError("a pending request of this kind already exists "
                         "for this program")
    doc = {"change_id": f"chg_{uuid.uuid4().hex[:10]}",
           "program_id": program["program_id"],
           "program_name": program.get("name"), "kind": kind,
           "payload": payload or {}, "reason": str(reason or "")[:300],
           "requested_by": requester_id, "requested_at": _now(),
           "expires_at": (datetime.now(timezone.utc)
                          + timedelta(hours=EXPIRY_HOURS)).isoformat(),
           "status": "pending"}
    await db.pamm_change_requests.insert_one(dict(doc))
    from modules.pamm.events import emit_event
    await emit_event(db, "ChangeRequested",
                     {"program_id": program["program_id"],
                      "change_id": doc["change_id"], "kind": kind})
    await db.pamm_notifications.insert_one(
        {"type": "ChangeRequested", "program_id": program["program_id"],
         "at": _now(), "seen": False,
         "summary": f"Dual-auth pending on {program.get('name')}: {kind} "
                    f"(needs a SECOND admin approval)"})
    doc.pop("_id", None)
    return doc


async def list_change_requests(db, program_id: str | None = None) -> list:
    q = {"program_id": program_id} if program_id else {}
    return [r async for r in db.pamm_change_requests.find(
        q, {"_id": 0}).sort("requested_at", -1).limit(100)]


async def _apply(db, req: dict) -> None:
    program = await db.pamm_programs.find_one(
        {"program_id": req["program_id"]}, {"_id": 0})
    if not program:
        raise ValueError("program no longer exists")
    kind, payload = req["kind"], req.get("payload") or {}
    if kind == "risk_limits_increase":
        from modules.pamm.risk import get_limits
        merged = get_limits(program)
        for k, v in (payload.get("risk_limits_patch") or {}).items():
            if k in merged and isinstance(v, dict):
                merged[k].update(v)
        await db.pamm_programs.update_one(
            {"program_id": program["program_id"]},
            {"$set": {"risk_limits": merged}})
    elif kind == "drift_tolerance_increase":
        await db.pamm_programs.update_one(
            {"program_id": program["program_id"]},
            {"$set": {"drift_tolerance":
                      float(payload.get("tolerance") or 0.0)}})
    elif kind == "unlock":
        from modules.pamm.risk.states import set_op_state
        await set_op_state(db, program,
                           str(payload.get("target") or "new_trades_paused"),
                           actor=req["requested_by"], reason="dual-auth unlock",
                           source="dual_auth", allow_deescalate=True)
    else:  # governance parameter changes — record the approved value
        field = {"master_account_change": "master_login_override",
                 "broker_change": "partner_id"}.get(kind)
        if field and payload.get("value"):
            await db.pamm_programs.update_one(
                {"program_id": program["program_id"]},
                {"$set": {field: str(payload["value"])[:120]}})
        else:
            await db.pamm_programs.update_one(
                {"program_id": program["program_id"]},
                {"$set": {f"governance.{kind}": {
                    "payload": payload, "applied_at": _now(),
                    "requested_by": req["requested_by"]}}})


async def decide_change_request(db, change_id: str, approve: bool,
                                approver_id: str) -> dict:
    req = await db.pamm_change_requests.find_one_and_update(
        {"change_id": change_id, "status": "pending"},
        {"$set": {"status": "processing"}})
    if not req:
        raise ValueError("request not found or already decided")
    req.pop("_id", None)

    async def _revert():
        await db.pamm_change_requests.update_one(
            {"change_id": change_id}, {"$set": {"status": "pending"}})

    if approve and req["requested_by"] == approver_id:
        await _revert()
        raise PermissionError(
            "second approver must be a DIFFERENT admin than the requester")
    if req["expires_at"] < _now():
        await db.pamm_change_requests.update_one(
            {"change_id": change_id}, {"$set": {"status": "expired"}})
        raise ValueError("request has expired — create a new one")

    if approve:
        try:
            await _apply(db, req)
        except Exception:
            await _revert()
            raise
    status = "approved" if approve else "rejected"
    await db.pamm_change_requests.update_one(
        {"change_id": change_id},
        {"$set": {"status": status, "decided_by": approver_id,
                  "decided_at": _now()}})
    from modules.pamm.events import emit_event
    await emit_event(db, "ChangeApproved" if approve else "ChangeRejected",
                     {"program_id": req["program_id"],
                      "change_id": change_id, "kind": req["kind"],
                      "requested_by": req["requested_by"],
                      "decided_by": approver_id})
    logger.warning("PAMM dual-auth %s: %s (%s) by %s (requested by %s)",
                   status, change_id, req["kind"], approver_id,
                   req["requested_by"])
    return {"change_id": change_id, "status": status, "kind": req["kind"]}


def is_loosening(current: dict, patch: dict) -> list:
    """Which parts of a validated risk-limits patch WEAKEN protection?"""
    _RANK = {"low": 1, "medium": 2, "high": 3}
    loosened = []
    for k, v in patch.items():
        cur = current.get(k) or {}
        if v.get("enabled") is False and cur.get("enabled", True):
            loosened.append(k)
        elif "threshold" in v and float(v["threshold"]) > float(
                cur.get("threshold", v["threshold"])):
            loosened.append(k)
        elif any(f in v and int(v[f]) < int(cur.get(f, 0))
                 for f in ("blackout_before_min", "blackout_after_min")):
            loosened.append(k)
        elif "min_impact" in v and _RANK.get(v["min_impact"], 3) > _RANK.get(
                cur.get("min_impact", "high"), 3):
            loosened.append(k)
    return loosened
