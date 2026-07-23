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


async def _ops_actor(request: Request):
    """(allowed, actor) — METRICS_TOKEN scraper or an admin session."""
    try:
        if _authorized(request):
            return True, "metrics-token"
    except Exception:
        pass
    try:
        from auth import get_current_user
        u = await get_current_user(request)
        if u.get("role") == "admin":
            return True, u.get("email") or "admin"
    except Exception:
        pass
    return False, None


def _as_dt(v):
    """BSON datetime or legacy ISO string → aware datetime (None on junk)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(v))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso(v):
    dt = _as_dt(v)
    return dt.isoformat() if dt else None


@router.get("/ops/release-readiness")
async def release_readiness(request: Request):
    # Two auth paths: metrics token (deploy scripts / Prometheus) OR an
    # authenticated admin session (Bot Health dashboard card).
    allowed = False
    try:
        allowed = _authorized(request)
    except Exception:
        pass
    if not allowed:
        try:
            from auth import get_current_user
            u = await get_current_user(request)
            allowed = u.get("role") == "admin"
        except Exception:
            pass
    if not allowed:
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
    workers = {}
    for name in EXPECTED_WORKERS:
        lease = leases.get(name)
        exp = _as_dt((lease or {}).get("expires_at"))
        alive = bool(exp and exp >= now)
        # loop-execution truth: the process may hold its lease while an
        # individual loop coroutine has crashed — require every loop running.
        loops_total = (lease or {}).get("loops_total")
        loops_running = (lease or {}).get("loops_running")
        loops_ok = (loops_total is None) or (loops_running == loops_total)
        # crash-loop detection: a supervised loop that keeps failing without
        # 5 min of healthy running marks the worker unhealthy
        crashlooping = [ln for ln, st in ((lease or {}).get("loops") or {}).items()
                        if (st or {}).get("consecutive_failures", 0) >= 3]
        # stall detection: a loop whose coroutine is alive but has made no
        # per-iteration progress for 3× its declared interval is unhealthy
        stalled = []
        for ln, st in ((lease or {}).get("loops") or {}).items():
            ivl = (st or {}).get("expected_interval_sec")
            done = _as_dt((st or {}).get("last_iteration_completed_at"))
            if ivl and done and alive:
                if (now - done).total_seconds() > max(3 * int(ivl), 120):
                    stalled.append(ln)
        workers[name] = {
            "alive": alive and loops_ok and not crashlooping and not stalled,
            "loops": (f"{loops_running}/{loops_total}"
                      if loops_total is not None else None),
            "crashlooping": crashlooping or None,
            "stalled": stalled or None,
            "renewed_at": _iso((lease or {}).get("renewed_at")),
        }
    checks["workers"] = {"ok": all(w["alive"] for w in workers.values()),
                         "detail": workers}

    # 3 · reconciliation lag — lease freshly renewed AND no broker-accepted
    #     order stuck unresolved for more than 5 minutes
    recon_renewed = _as_dt((leases.get("reconciliation") or {}).get("renewed_at"))
    recon_fresh = bool(recon_renewed
                       and recon_renewed >= now - timedelta(seconds=120))
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


@router.get("/ops/alerts")
async def list_alerts(request: Request, include_acked: bool = False,
                      limit: int = 100):
    allowed, _ = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    q = {} if include_acked else {"acked_at": None}
    out = []
    async for a in db.ops_alerts.find(q).sort("created_at", -1).limit(
            max(1, min(limit, 500))):
        a["id"] = str(a.pop("_id"))
        out.append(a)
    return {"alerts": out,
            "unacked": await db.ops_alerts.count_documents({"acked_at": None})}


@router.post("/ops/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, request: Request):
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from bson import ObjectId
    try:
        oid = ObjectId(alert_id)
    except Exception:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    db = get_db()
    res = await db.ops_alerts.update_one(
        {"_id": oid, "acked_at": None},
        {"$set": {"acked_at": datetime.now(timezone.utc),
                  "acked_by": actor}})
    if res.matched_count == 0:
        return JSONResponse(status_code=404,
                            content={"detail": "not found or already acked"})
    return {"ok": True, "acked_by": actor}


@router.post("/ops/alerts/ack-all")
async def ack_all_alerts(request: Request):
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    res = await db.ops_alerts.update_many(
        {"acked_at": None},
        {"$set": {"acked_at": datetime.now(timezone.utc),
                  "acked_by": actor}})
    return {"ok": True, "acked": res.modified_count}
