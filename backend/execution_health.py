"""A7d — execution-health brake (main90–91 review, approved by the operator).

Execution anomalies — late fills, broker rejects, slippage vetoes, duplicate
tickets — are recorded per account. When ≥ EXEC_BRAKE_THRESHOLD of them land
inside the rolling EXEC_BRAKE_WINDOW_MIN window the account's NEW entries are
paused (managed exits keep running), an ops alert is raised and the Dashboard /
Bot Pulse show the EXECUTION BRAKE pill. The brake releases itself after
EXEC_BRAKE_RELEASE_MIN clean minutes, or by hand (step-up) via the API.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("execution_health")

EVENT_KINDS = ("late_fill", "reject", "slippage_veto", "duplicate_ticket")


def _i(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


THRESHOLD = _i("EXEC_BRAKE_THRESHOLD", 3)
WINDOW_MIN = _i("EXEC_BRAKE_WINDOW_MIN", 120)
RELEASE_MIN = _i("EXEC_BRAKE_RELEASE_MIN", 60)


def _now():
    return datetime.now(timezone.utc)


def is_braked(acc: dict | None) -> bool:
    return bool(((acc or {}).get("execution_brake") or {}).get("active"))


async def record_event(db, account_id: str, user_id: str | None, kind: str, *,
                       trade_id=None, detail: str = "") -> dict | None:
    """Persist one anomaly and re-evaluate the brake. Returns the brake state when it engaged."""
    if kind not in EVENT_KINDS or not account_id:
        return None
    now = _now()
    await db.execution_health_events.insert_one({
        "account_id": str(account_id), "user_id": user_id, "kind": kind,
        "trade_id": str(trade_id) if trade_id else None, "detail": str(detail or "")[:200],
        "at": now.isoformat()})
    return await evaluate(db, str(account_id))


async def window_events(db, account_id: str) -> list[dict]:
    since = (_now() - timedelta(minutes=WINDOW_MIN)).isoformat()
    return await db.execution_health_events.find(
        {"account_id": str(account_id), "at": {"$gte": since}},
        {"_id": 0, "kind": 1, "trade_id": 1, "detail": 1, "at": 1}).sort("at", -1).to_list(length=200)


async def evaluate(db, account_id: str) -> dict | None:
    """Engage the brake when the window holds ≥ THRESHOLD anomalies (idempotent)."""
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1, "user_id": 1, "label": 1})
    if not acc or is_braked(acc):
        return None
    events = await window_events(db, account_id)
    if len(events) < THRESHOLD:
        return None
    now = _now()
    kinds = sorted({e["kind"] for e in events})
    state = {"active": True, "since": now.isoformat(), "engaged_by": "auto",
             "reason": f"{len(events)} execution anomalies in {WINDOW_MIN} min ({', '.join(kinds)})",
             "event_count": len(events), "kinds": kinds,
             "release_after": (now + timedelta(minutes=RELEASE_MIN)).isoformat()}
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"execution_brake": state}})
    logger.warning("EXECUTION BRAKE engaged account=%s: %s", account_id, state["reason"])
    try:
        from alerting import raise_alert
        await raise_alert(db, "execution_brake", "critical",
                          f"EXECUTION BRAKE on account {acc.get('label') or account_id}: {state['reason']}. "
                          f"New entries paused; exits continue. Auto-release after {RELEASE_MIN} clean minutes.",
                          dedup_key=f"execution_brake:{account_id}",
                          meta={"account_id": account_id, "user_id": acc.get("user_id"), **state})
    except Exception as e:  # noqa: BLE001
        logger.warning("execution brake alert failed: %s", type(e).__name__)
    try:
        await db.audit_log.insert_one({
            "user_id": acc.get("user_id"), "action": "execution_brake_engaged",
            "detail": {"account_id": account_id, **state}, "step_up_verified": False, "at": now.isoformat()})
    except Exception:  # noqa: BLE001
        pass
    return state


async def maybe_auto_release(db, acc: dict) -> bool:
    """Release when the clean period has elapsed with no new anomaly (called from the trading loop)."""
    brake = (acc or {}).get("execution_brake") or {}
    if not brake.get("active"):
        return False
    try:
        release_after = datetime.fromisoformat(str(brake.get("release_after")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    now = _now()
    if now < release_after:
        return False
    since = (now - timedelta(minutes=RELEASE_MIN)).isoformat()
    recent = await db.execution_health_events.count_documents({"account_id": str(acc["_id"]), "at": {"$gte": since}})
    if recent:
        # anomalies kept coming: push the clean window out
        await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {
            "execution_brake.release_after": (now + timedelta(minutes=RELEASE_MIN)).isoformat()}})
        return False
    await release(db, str(acc["_id"]), actor="auto", reason=f"{RELEASE_MIN} clean minutes")
    return True


async def release(db, account_id: str, *, actor: str, reason: str = "") -> dict:
    now = _now().isoformat()
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1, "user_id": 1}) or {}
    prev = acc.get("execution_brake") or {}
    state = {"active": False, "released_at": now, "released_by": actor, "release_reason": reason,
             "last_engaged": prev.get("since"), "last_reason": prev.get("reason")}
    await db.accounts.update_one({"_id": _oid(account_id)}, {"$set": {"execution_brake": state}})
    try:
        await db.ops_alerts.update_many({"dedup_key": f"execution_brake:{account_id}", "acked_at": None},
                                        {"$set": {"acked_by": actor, "acked_at": now,
                                                  "auto_resolved": actor == "auto"}})
    except Exception:  # noqa: BLE001
        pass
    try:
        await db.audit_log.insert_one({
            "user_id": acc.get("user_id"), "action": "execution_brake_released",
            "detail": {"account_id": account_id, "actor": actor, "reason": reason},
            "step_up_verified": actor != "auto", "at": now})
    except Exception:  # noqa: BLE001
        pass
    logger.info("EXECUTION BRAKE released account=%s by %s (%s)", account_id, actor, reason)
    return state


async def summary(db, account_id: str) -> dict:
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1}) or {}
    events = await window_events(db, account_id)
    return {"account_id": str(account_id), "brake": acc.get("execution_brake") or {"active": False},
            "window_min": WINDOW_MIN, "threshold": THRESHOLD, "release_min": RELEASE_MIN,
            "events_in_window": len(events), "events": events[:20]}


def _oid(v):
    from bson import ObjectId
    try:
        return ObjectId(str(v))
    except Exception:  # noqa: BLE001 — fake ids in unit tests
        return v
