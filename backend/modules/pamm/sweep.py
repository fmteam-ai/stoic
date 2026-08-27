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
    # retry unresolved FLATTEN_FAILED incidents first (critical invariant)
    flatten_retries = 0
    async for p in db.pamm_programs.find(
            {"flatten_failed": {"$exists": True}}, {"_id": 0}):
        from modules.pamm.risk.states import _flatten_and_verify
        attempt = int((p.get("flatten_failed") or {}).get("attempts", 1)) + 1
        try:
            await _flatten_and_verify(db, p, "auto-sweep", attempt=attempt)
        except Exception as e:
            logger.error("sweep flatten retry failed on %s: %s",
                         p.get("program_id"), e)
        flatten_retries += 1
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
    # position truth (v55 §3) — expected vs broker book; drift freezes
    # new exposure via the op-state machine (escalation only)
    truth_checked, drifts = 0, 0
    from modules.pamm.reconciliation.position_truth import \
        check_position_truth
    async for p in db.pamm_programs.find({"status": "active"}, {"_id": 0}):
        try:
            r = await check_position_truth(db, p, actor="auto-sweep")
            truth_checked += 1
            if r.get("status") == "drift":
                drifts += 1
        except Exception as e:
            logger.warning("sweep position-truth failed on %s: %s",
                           p.get("program_id"), e)
            # BROKER_UNCERTAIN (v56 §8): 3 consecutive failures to
            # establish position truth → new trades NO, close risk YES
            fails = int(p.get("position_truth_failures") or 0) + 1
            await db.pamm_programs.update_one(
                {"program_id": p["program_id"]},
                {"$set": {"position_truth_failures": fails}})
            if fails >= 3:
                from modules.pamm.risk.states import (op_state_of,
                                                      set_op_state)
                from modules.pamm.risk.states import severity as _sev
                if _sev(op_state_of(p)) < _sev("broker_uncertain"):
                    try:
                        await set_op_state(
                            db, p, "broker_uncertain", "auto-sweep",
                            reason=f"position truth unavailable x{fails} "
                                   f"— broker state cannot be verified",
                            source="automation")
                    except Exception as e2:
                        logger.error("broker_uncertain escalation failed "
                                     "on %s: %s", p.get("program_id"), e2)
    from execution_intents import (expire_stale, mark_unknown_stale,
                                   reconcile_unknown_intents)
    intents_expired = await expire_stale(db)
    intents_unknown = await mark_unknown_stale(db)
    unknown_recon = await reconcile_unknown_intents(db)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=HEALTH_RETENTION_DAYS)).isoformat()
    await db.pamm_health.delete_many({"at": {"$lt": cutoff}})
    doc = {"_id": "last", "at": started, "finished_at": _now(),
           "programs_checked": checked, "breaches": breaches,
           "flatten_retries": flatten_retries,
           "position_truth": {"checked": truth_checked, "drift": drifts},
           "intents_expired": intents_expired,
           "intents_unknown": intents_unknown,
           "unknown_reconciled": unknown_recon,
           "heartbeats": [{k: h.get(k) for k in
                           ("partner_id", "ok", "score", "status",
                            "latency_ms")} for h in heartbeats]}
    await db.pamm_sweeps.replace_one({"_id": "last"}, dict(doc), upsert=True)
    if breaches:
        logger.warning("PAMM sweep enforced breaches: %s", breaches)
    return doc


async def sweep_status(db) -> dict:
    return await db.pamm_sweeps.find_one({"_id": "last"}) or {"at": None}
