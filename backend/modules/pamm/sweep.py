"""PAMM automatic sweeps — risk engine + broker heartbeat every ~60s.
Enforcement is delegated to run_risk_check (halt/flatten + alerts)."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pamm.sweep")

HEALTH_RETENTION_DAYS = 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def sweep_once(db) -> dict:
    from modules.pamm.risk import run_risk_check
    from services.broker_gateway.health import heartbeat_all

    started = _now()
    heartbeats = await heartbeat_all(db)
    checked, breaches = 0, []
    async for p in db.pamm_programs.find({"status": "active"}, {"_id": 0}):
        try:
            res = await run_risk_check(db, p, actor="auto-sweep")
            checked += 1
            if res.get("action_taken"):
                breaches.append(
                    {"program_id": p["program_id"],
                     "action": res["action_taken"],
                     "limits": [c["limit"] for c in res["breached"]]})
        except Exception as e:
            logger.warning("sweep risk-check failed on %s: %s",
                           p.get("program_id"), e)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=HEALTH_RETENTION_DAYS)).isoformat()
    await db.pamm_health.delete_many({"at": {"$lt": cutoff}})
    doc = {"_id": "last", "at": started, "finished_at": _now(),
           "programs_checked": checked, "breaches": breaches,
           "heartbeats": [{k: h.get(k) for k in
                           ("partner_id", "ok", "score", "status",
                            "latency_ms")} for h in heartbeats]}
    await db.pamm_sweeps.replace_one({"_id": "last"}, dict(doc), upsert=True)
    if breaches:
        logger.warning("PAMM sweep enforced breaches: %s", breaches)
    return doc


async def sweep_status(db) -> dict:
    return await db.pamm_sweeps.find_one({"_id": "last"}) or {"at": None}
