"""Broker Health Monitor (Phase 10) — heartbeat + latency → 0-100 score.
Score = success-rate (70pts) + latency (30pts) over the last 20 checks."""
import logging
import time
from datetime import datetime, timezone

from services.broker_gateway.broker_adapter import adapter_for

logger = logging.getLogger("pamm.health")

HISTORY_WINDOW = 20
PARTNER_FIELDS = {"_id": 0, "partner_id": 1, "name": 1, "adapter": 1,
                  "status": 1, "health": 1, "created_at": 1}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_score(history: list) -> float:
    """history = recent checks, each {ok, latency_ms}."""
    if not history:
        return 0.0
    success = sum(1 for h in history if h.get("ok")) / len(history)
    lats = [h["latency_ms"] for h in history if h.get("ok")]
    avg = sum(lats) / len(lats) if lats else 2000.0
    lat_score = max(0.0, min(1.0, (2000.0 - avg) / 1800.0))
    return round(success * 70 + lat_score * 30, 1)


def status_for(score: float) -> str:
    return "healthy" if score >= 80 else (
        "degraded" if score >= 50 else "down")


async def heartbeat_partner(db, partner: dict) -> dict:
    """One heartbeat: time an adapter round-trip, record, rescore."""
    t0 = time.perf_counter()
    ok, error = True, None
    try:
        adapter = adapter_for(db, partner)
        await adapter.get_pamm_programs()
    except Exception as e:
        ok, error = False, str(e)[:200]
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    check = {"partner_id": partner["partner_id"], "ok": ok,
             "latency_ms": latency_ms, "error": error, "at": _now()}
    await db.pamm_health.insert_one(dict(check))

    history = [h async for h in db.pamm_health.find(
        {"partner_id": partner["partner_id"]},
        {"_id": 0, "ok": 1, "latency_ms": 1})
        .sort("at", -1).limit(HISTORY_WINDOW)]
    score = compute_score(history)
    status = status_for(score)
    prev_status = (partner.get("health") or {}).get("status")
    await db.broker_partners.update_one(
        {"partner_id": partner["partner_id"]},
        {"$set": {"health": {"score": score, "status": status, "ok": ok,
                             "latency_ms": latency_ms,
                             "last_check": check["at"]}}})

    if not ok:
        from modules.pamm.events import emit_event
        await emit_event(db, "HeartbeatLost",
                         {"partner_id": partner["partner_id"],
                          "error": error}, source="health-monitor")
        await db.pamm_notifications.insert_one(
            {"type": "HeartbeatLost", "partner_id": partner["partner_id"],
             "at": _now(), "seen": False,
             "summary": f"Broker heartbeat FAILED: {partner.get('name')} "
                        f"— {error}"})
    elif prev_status in ("healthy", None) and status != "healthy":
        from modules.pamm.events import emit_event
        await emit_event(db, "BrokerHealthDegraded",
                         {"partner_id": partner["partner_id"],
                          "score": score, "status": status},
                         source="health-monitor")
    check.pop("_id", None)
    return {**check, "score": score, "status": status}


async def heartbeat_all(db) -> list:
    results = []
    async for p in db.broker_partners.find({}, {"_id": 0}):
        results.append(await heartbeat_partner(db, p))
    return results


async def health_overview(db) -> list:
    out = []
    async for p in db.broker_partners.find({}, dict(PARTNER_FIELDS)):
        history = [h async for h in db.pamm_health.find(
            {"partner_id": p["partner_id"]},
            {"_id": 0, "ok": 1, "latency_ms": 1, "at": 1, "error": 1})
            .sort("at", -1).limit(HISTORY_WINDOW)]
        out.append({**p, "history": history})
    return out
