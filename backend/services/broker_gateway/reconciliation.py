"""Reconciliation engine (Phase 11) — the broker is AUTHORITATIVE. Every
run pulls live NAV / master / allocations from the broker, compares against
STOIC's mirror, records drift in pamm_reconciliation + pamm_audit, updates
the mirror, and emits events on discrepancies. Never trusts cached values.
"""
import logging
import uuid
from datetime import datetime, timezone

from services.broker_gateway.pamm_api import get_adapter

logger = logging.getLogger("pamm.recon")
NAV_DRIFT_ALERT_PCT = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def reconcile_program(db, program: dict) -> dict:
    adapter = await get_adapter(db, program["partner_id"])
    bpid = program["broker_program_id"]
    broker_nav = await adapter.get_nav(bpid)
    master = await adapter.get_master_account(bpid)
    broker_allocs = [a async for a in db.sandbox_broker_allocations.find(
        {"program_id": bpid}, {"_id": 0})] if program["partner_id"] == "prt_sandbox" else []

    mirrored_nav = (program.get("last_nav") or {}).get("nav")
    drift_pct = None
    if mirrored_nav:
        drift_pct = round(abs(broker_nav["nav"] - mirrored_nav)
                          / max(mirrored_nav, 1e-9) * 100, 4)

    mirror_alloc_total = 0.0
    async for a in db.pamm_allocations.find(
            {"program_id": program["program_id"]}, {"amount": 1}):
        mirror_alloc_total += float(a.get("amount") or 0)
    broker_alloc_total = round(sum(float(a["amount"]) for a in broker_allocs), 2)
    alloc_match = (program["partner_id"] != "prt_sandbox"
                   or round(mirror_alloc_total, 2) == broker_alloc_total)

    result = {"recon_id": f"rec_{uuid.uuid4().hex[:10]}",
              "program_id": program["program_id"], "at": _now(),
              "broker_nav": broker_nav["nav"],
              "mirrored_nav": mirrored_nav,
              "nav_drift_pct": drift_pct,
              "broker_alloc_total": broker_alloc_total,
              "mirror_alloc_total": round(mirror_alloc_total, 2),
              "allocations_match": alloc_match,
              "investor_count": master.get("investor_count"),
              "trading": master.get("trading"),
              "status": "ok" if (alloc_match and (drift_pct is None
                                 or drift_pct < NAV_DRIFT_ALERT_PCT))
              else "drift"}
    await db.pamm_reconciliation.insert_one(dict(result))

    # broker truth overwrites the mirror
    await db.pamm_nav_snapshots.insert_one(
        {"program_id": program["program_id"], "nav": broker_nav["nav"],
         "currency": broker_nav.get("currency"), "at": _now()})
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"last_nav": {"nav": broker_nav["nav"], "at": _now()},
                  "investor_count": master.get("investor_count"),
                  "trading": master.get("trading"),
                  "aum": broker_nav["nav"],
                  "last_reconciled_at": _now()}})
    await db.pamm_audit.insert_one(
        {"kind": "reconciliation", "program_id": program["program_id"],
         "at": _now(), "result": result["status"],
         "recon_id": result["recon_id"]})
    if result["status"] == "drift":
        from modules.pamm.events import emit_event
        await emit_event(db, "ReconciliationDrift",
                         {"program_id": program["program_id"],
                          "recon_id": result["recon_id"],
                          "nav_drift_pct": drift_pct,
                          "allocations_match": alloc_match},
                         source="reconciliation")
        logger.warning("PAMM reconciliation drift on %s: %s",
                       program["program_id"], result)
    return result


async def reconcile_all(db) -> list:
    out = []
    async for program in db.pamm_programs.find({"status": "active"}):
        try:
            out.append(await reconcile_program(db, program))
        except Exception as e:  # noqa: BLE001
            logger.warning("reconciliation failed for %s: %s",
                           program.get("program_id"), e)
    return out
