"""A7d — execution-health brake (operator spec, main93 decision 2).

Only LATE FILLS count. Per account:
  · 1 late fill            → alert the owner (Telegram + in-app) and the admins (ops alert)
  · 2 late fills in 24 h   → NEW entries paused (managed exits keep running) until an ADMIN
                             presses Resume after checking the EA / VPS — no auto-release,
                             the owner cannot release it
  · every pause / resume is audited; only late fills AFTER the last release count again.
Other anomalies (rejects, slippage vetoes, duplicate tickets) are still recorded for the
panel but never engage the brake. The brake is enforced at the dispatch fence
(/bridge/poll-trades), so queued, scalp-fast and manual entries cannot bypass it.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("execution_health")

EVENT_KINDS = ("late_fill", "reject", "slippage_veto", "duplicate_ticket")
COUNTED_KINDS = ("late_fill",)


def _i(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


THRESHOLD = _i("EXEC_BRAKE_THRESHOLD", 2)
WINDOW_MIN = _i("EXEC_BRAKE_WINDOW_MIN", 24 * 60)
RELEASE_MIN = None          # no auto-release — admin Resume only


def _now():
    return datetime.now(timezone.utc)


def is_braked(acc: dict | None) -> bool:
    return bool(((acc or {}).get("execution_brake") or {}).get("active"))


async def record_event(db, account_id: str, user_id: str | None, kind: str, *,
                       trade_id=None, detail: str = "") -> dict | None:
    """Persist one anomaly; late fills alert the owner and re-evaluate the brake.
    Returns the brake state when it engaged."""
    if kind not in EVENT_KINDS or not account_id:
        return None
    now = _now()
    await db.execution_health_events.insert_one({
        "account_id": str(account_id), "user_id": user_id, "kind": kind,
        "trade_id": str(trade_id) if trade_id else None, "detail": str(detail or "")[:200],
        "at": now.isoformat()})
    if kind not in COUNTED_KINDS:
        return None
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1, "user_id": 1, "label": 1})
    if not acc:
        return None
    events = await window_events(db, str(account_id), acc)
    await _alert_late_fill(db, acc, str(account_id), len(events), detail)
    return await evaluate(db, str(account_id), acc=acc, events=events)


def _window_floor(acc: dict | None) -> str:
    """Events older than the window OR before the last release never count again (M3)."""
    since = (_now() - timedelta(minutes=WINDOW_MIN)).isoformat()
    released = str(((acc or {}).get("execution_brake") or {}).get("released_at") or "")
    return max(since, released)


async def window_events(db, account_id: str, acc: dict | None = None) -> list[dict]:
    if acc is None:
        acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1}) or {}
    return await db.execution_health_events.find(
        {"account_id": str(account_id), "kind": {"$in": list(COUNTED_KINDS)}, "at": {"$gt": _window_floor(acc)}},
        {"_id": 0, "kind": 1, "trade_id": 1, "detail": 1, "at": 1}).sort("at", -1).to_list(length=200)


async def recent_events(db, account_id: str, minutes: int = 24 * 60) -> list[dict]:
    """Every anomaly kind in the window — informational, for the panel."""
    since = (_now() - timedelta(minutes=minutes)).isoformat()
    return await db.execution_health_events.find(
        {"account_id": str(account_id), "at": {"$gte": since}},
        {"_id": 0, "kind": 1, "trade_id": 1, "detail": 1, "at": 1}).sort("at", -1).to_list(length=200)


async def _alert_late_fill(db, acc: dict, account_id: str, count: int, detail: str) -> None:
    """1 late fill → owner (Telegram + in-app) and admins (ops alert)."""
    label = acc.get("label") or account_id
    user_id = acc.get("user_id")
    msg = (f"Late fill on account {label}: an order STOIC had already cancelled filled at the broker "
           f"({detail or 'see execution health'}). {count} in the last 24 h — "
           f"{'new entries are now PAUSED' if count >= THRESHOLD else f'at {THRESHOLD} new entries pause'} "
           "until an admin checks the EA / VPS.")
    try:
        from alerting import raise_alert
        await raise_alert(db, "late_fill", "warning" if count < THRESHOLD else "critical", msg,
                          dedup_key=f"late_fill:{account_id}:{_now().strftime('%Y%m%d%H%M')}",
                          meta={"account_id": account_id, "user_id": user_id, "count_24h": count})
    except Exception as e:  # noqa: BLE001
        logger.warning("late fill ops alert failed: %s", type(e).__name__)
    if not user_id:
        return
    try:
        await db.notifications.insert_one({
            "user_id": str(user_id), "kind": "execution_late_fill", "title": "Late fill detected",
            "message": msg, "severity": "warning" if count < THRESHOLD else "critical", "read": False,
            "created_at": _now().isoformat()})
    except Exception as e:  # noqa: BLE001
        logger.warning("late fill in-app notification failed: %s", type(e).__name__)
    try:
        from notifier import send_telegram
        await send_telegram(str(user_id), "execution_brake", "⚠️ LATE FILL",
                            [f"Account: {label}", f"Detail: {detail or '-'}", f"Late fills in 24 h: {count}",
                             "New entries pause at 2 until an admin resumes after checking the EA / VPS."
                             if count < THRESHOLD else "New entries are PAUSED — an admin must resume."])
    except Exception as e:  # noqa: BLE001
        logger.warning("late fill telegram failed: %s", type(e).__name__)


async def evaluate(db, account_id: str, *, acc: dict | None = None, events: list | None = None) -> dict | None:
    """Engage the brake when ≥ THRESHOLD late fills landed since max(window, last release)."""
    if acc is None:
        acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1, "user_id": 1, "label": 1})
    if not acc or is_braked(acc):
        return None
    if events is None:
        events = await window_events(db, account_id, acc)
    if len(events) < THRESHOLD:
        return None
    now = _now()
    state = {"active": True, "since": now.isoformat(), "engaged_by": "auto",
             "reason": f"{len(events)} late fills in {WINDOW_MIN // 60} h",
             "event_count": len(events), "kinds": ["late_fill"],
             "release_after": None, "release_requires": "admin"}
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"execution_brake": state}})
    logger.warning("EXECUTION BRAKE engaged account=%s: %s", account_id, state["reason"])
    try:
        from alerting import raise_alert
        await raise_alert(db, "execution_brake", "critical",
                          f"EXECUTION BRAKE on account {acc.get('label') or account_id}: {state['reason']}. "
                          "New entries paused; exits continue. An ADMIN must check the EA / VPS and press Resume.",
                          dedup_key=f"execution_brake:{account_id}",
                          meta={"account_id": account_id, "user_id": acc.get("user_id"), **state})
    except Exception as e:  # noqa: BLE001
        logger.warning("execution brake alert failed: %s", type(e).__name__)
    if acc.get("user_id"):
        try:
            await db.notifications.insert_one({
                "user_id": str(acc["user_id"]), "kind": "execution_brake", "title": "New entries paused",
                "message": f"Account {acc.get('label') or account_id}: {state['reason']}. New entries are paused "
                           "until an admin checks the EA / VPS and resumes. Open positions stay fully managed.",
                "severity": "critical", "read": False, "created_at": now.isoformat()})
        except Exception as e:  # noqa: BLE001
            logger.warning("execution brake in-app notification failed: %s", type(e).__name__)
    try:
        await db.audit_log.insert_one({
            "user_id": acc.get("user_id"), "action": "execution_brake_engaged",
            "detail": {"account_id": account_id, **state}, "step_up_verified": False, "at": now.isoformat()})
    except Exception:  # noqa: BLE001
        pass
    return state


async def maybe_auto_release(db, acc: dict) -> bool:
    """Operator spec: there is NO automatic release. Kept for callers; always False."""
    return False


async def release(db, account_id: str, *, actor: str, reason: str = "") -> dict:
    now = _now().isoformat()
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1, "user_id": 1}) or {}
    prev = acc.get("execution_brake") or {}
    state = {"active": False, "released_at": now, "released_by": actor, "release_reason": reason,
             "last_engaged": prev.get("since"), "last_reason": prev.get("reason")}
    await db.accounts.update_one({"_id": _oid(account_id)}, {"$set": {"execution_brake": state}})
    try:
        await db.ops_alerts.update_many({"dedup_key": f"execution_brake:{account_id}", "acked_at": None},
                                        {"$set": {"acked_by": actor, "acked_at": now, "auto_resolved": False}})
    except Exception:  # noqa: BLE001
        pass
    try:
        await db.audit_log.insert_one({
            "user_id": acc.get("user_id"), "action": "execution_brake_released",
            "detail": {"account_id": account_id, "actor": actor, "reason": reason},
            "step_up_verified": True, "at": now})
    except Exception:  # noqa: BLE001
        pass
    logger.info("EXECUTION BRAKE released account=%s by %s (%s)", account_id, actor, reason)
    return state


async def summary(db, account_id: str) -> dict:
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"execution_brake": 1}) or {}
    counted = await window_events(db, account_id, acc)
    events = await recent_events(db, account_id)
    return {"account_id": str(account_id), "brake": acc.get("execution_brake") or {"active": False},
            "window_min": WINDOW_MIN, "threshold": THRESHOLD, "release_min": None, "counted_kinds": list(COUNTED_KINDS),
            "release_requires": "admin", "events_in_window": len(counted), "events": events[:20]}


def _oid(v):
    from bson import ObjectId
    try:
        return ObjectId(str(v))
    except Exception:  # noqa: BLE001 — fake ids in unit tests
        return v
