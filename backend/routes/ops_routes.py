"""Release-readiness probe — verifies the complete trading topology before a
deployment is accepted (used by deploy/update.sh). METRICS_TOKEN gated."""
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from routes.metrics_routes import _authorized

router = APIRouter(tags=["ops"])

EXPECTED_WORKERS = ("trading", "protection", "reconciliation",
                    "analytics", "model", "tuning")


@router.get("/ops/release-readiness")
async def release_readiness(request: Request):
    if not _authorized(request):
        return JSONResponse(status_code=403,
                            content={"detail": "bad metrics token"})
    db = get_db()
    now = datetime.now(timezone.utc)
    checks: dict = {}

    # 1 · MongoDB full round trip (write + read + delete, not just ping)
    try:
        t0 = time.perf_counter()
        probe_id = f"readiness-{now.timestamp()}"
        await db.ops_probes.insert_one({"_id": probe_id, "at": now.isoformat()})
        assert await db.ops_probes.find_one({"_id": probe_id})
        await db.ops_probes.delete_one({"_id": probe_id})
        checks["mongo_roundtrip"] = {
            "ok": True, "ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception:
        checks["mongo_roundtrip"] = {"ok": False}

    # 2 · all six worker leases present and unexpired
    leases = {str(w["_id"]): w async for w in db.worker_leases.find({})}
    now_iso = now.isoformat()
    workers = {}
    for name in EXPECTED_WORKERS:
        lease = leases.get(name)
        alive = bool(lease and str(lease.get("expires_at") or "") >= now_iso)
        workers[name] = {
            "alive": alive,
            "renewed_at": (lease or {}).get("renewed_at"),
        }
    checks["workers"] = {"ok": all(w["alive"] for w in workers.values()),
                         "detail": workers}

    # 3 · reconciliation lag — lease freshly renewed AND no broker-accepted
    #     order stuck unresolved for more than 5 minutes
    recon = leases.get("reconciliation") or {}
    recon_fresh = str(recon.get("renewed_at") or "") >= (
        now - timedelta(seconds=120)).isoformat()
    stale_cutoff = (now - timedelta(minutes=5)).isoformat()
    stuck = await db.trades.count_documents(
        {"status": "pending",
         "submission_state": "broker_accepted_unresolved",
         "updated_at": {"$lt": stale_cutoff}})
    checks["reconciliation"] = {"ok": recon_fresh and stuck == 0,
                                "lease_fresh": recon_fresh,
                                "stuck_unresolved_gt_5m": stuck}

    # 4 · outbox backlog — nothing pending older than 2 minutes
    backlog_cutoff = (now - timedelta(minutes=2)).isoformat()
    pending = await db.outbox.count_documents({"state": "pending"})
    aged = await db.outbox.count_documents(
        {"state": "pending", "created_at": {"$lt": backlog_cutoff}})
    checks["outbox"] = {"ok": aged == 0, "pending": pending,
                        "pending_older_than_2m": aged}

    # 5 · schema compatibility — DB must not hold decisions written by a
    #     NEWER feature schema than this build understands
    from scalp.feature_schema import FEATURE_SCHEMA_VERSION
    newest = await db.trade_decisions.find_one(
        {"feature_schema_version": {"$exists": True}},
        sort=[("feature_schema_version", -1)],
        projection={"feature_schema_version": 1})
    db_version = (newest or {}).get("feature_schema_version", 0)
    checks["schema"] = {"ok": db_version <= FEATURE_SCHEMA_VERSION,
                        "code_version": FEATURE_SCHEMA_VERSION,
                        "db_max_version": db_version}

    ready = all(c["ok"] for c in checks.values())
    return JSONResponse(status_code=200 if ready else 503,
                        content={"ready": ready,
                                 "checked_at": now.isoformat(),
                                 "checks": checks})
