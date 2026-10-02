"""Release-readiness probe — verifies the complete trading topology before a
deployment is accepted (used by deploy/update.sh).

Auth split (code review O8): METRICS_TOKEN (Prometheus scrape credential)
authorises READ-ONLY ops endpoints only; mutating machine calls require
OPS_DEPLOY_TOKEN. Humans need a verified admin session (role + TOTP MFA)."""
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from routes.metrics_routes import _authorized

router = APIRouter(tags=["ops"])

EXPECTED_WORKERS = ("trading", "protection", "reconciliation",
                    "analytics", "model", "tuning")


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_log = logging.getLogger("ops_auth")


def _metrics_token_ok(request: Request) -> bool:
    """METRICS_TOKEN — the Prometheus scrape credential (read-only)."""
    try:
        return bool(_authorized(request))
    except Exception:
        return False


def _deploy_token_ok(request: Request) -> bool:
    """OPS_DEPLOY_TOKEN — the machine credential for MUTATING ops paths
    (deploy scripts / CI). Sent as `X-Ops-Deploy-Token: <t>` or
    `Authorization: Bearer <t>`. Disabled when unset or when it equals
    METRICS_TOKEN (key separation: the scraper credential must never
    double as deploy authority)."""
    expected = os.environ.get("OPS_DEPLOY_TOKEN") or ""
    if not expected:
        return False
    if hmac.compare_digest(expected, os.environ.get("METRICS_TOKEN") or ""):
        _log.error("OPS_DEPLOY_TOKEN == METRICS_TOKEN — deploy token path disabled")
        return False
    got = request.headers.get("X-Ops-Deploy-Token") or ""
    auth = request.headers.get("Authorization", "")
    if not got and auth.startswith("Bearer "):
        got = auth[7:]
    return bool(got) and hmac.compare_digest(str(got), expected)


def _metrics_token_may_deploy() -> bool:
    """Transitional escape hatch (default OFF): lets METRICS_TOKEN keep its
    pre-split mutating rights while deploy tooling migrates to
    OPS_DEPLOY_TOKEN. Remove once every caller sends the deploy token."""
    return (os.environ.get("OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY") or "").strip().lower() == "true"


def _machine_actor(request: Request, mutating: bool):
    """Machine credential → actor label, or None."""
    if _deploy_token_ok(request):
        return "ops-deploy-token"
    if _metrics_token_ok(request):
        if not mutating:
            return "metrics-token"
        if _metrics_token_may_deploy():
            _log.warning("METRICS_TOKEN used for mutating ops %s %s "
                         "(OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY=true — migrate "
                         "the caller to OPS_DEPLOY_TOKEN)",
                         request.method, request.url.path)
            return "metrics-token"
    return None


async def _admin_user(request: Request):
    """Verified admin session (role + mandatory TOTP MFA via
    auth.require_admin) or None. A role-admin WITHOUT MFA gets the explicit
    403 admin_mfa_required (so the UI can prompt enrollment)."""
    try:
        from auth import get_current_user
        u = await get_current_user(request)
    except Exception:
        return None
    if (u or {}).get("role") != "admin":
        return None
    from auth import require_admin
    require_admin(u)          # raises 403 admin_mfa_required
    return u


async def _ops_actor(request: Request, *, mutating: bool | None = None):
    """(allowed, actor) — machine token or a verified admin session.
    Read-only requests (GET/HEAD) accept METRICS_TOKEN or OPS_DEPLOY_TOKEN;
    mutating requests (POST/PUT/PATCH/DELETE — inferred from the method
    unless `mutating` is given) require OPS_DEPLOY_TOKEN."""
    if mutating is None:
        mutating = request.method.upper() not in _READ_METHODS
    actor = _machine_actor(request, mutating)
    if actor:
        return True, actor
    u = await _admin_user(request)
    if u is None:
        return False, None
    return True, u.get("email") or "admin"


async def _ops_admin_step_up(request: Request, action: str):
    """iter-163 — like _ops_actor but human admin sessions must also carry a
    fresh step-up (TOTP) token for `action`. Machine callers must present
    OPS_DEPLOY_TOKEN (METRICS_TOKEN only when the transitional
    OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY=true). Raises 403 step_up_required /
    mfa_enrollment_required for admins without a fresh token."""
    actor = _machine_actor(request, mutating=True)
    if actor:
        return True, actor
    u = await _admin_user(request)
    if u is None:
        return False, None
    from step_up import audit_event, require_step_up
    db = get_db()
    await require_step_up(db, u, request, action)
    await audit_event(db, u["id"], action, {"path": str(request.url.path)},
                      request, step_up=True)
    return True, u.get("email") or "admin"


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
    # Two auth paths: metrics/deploy token (deploy scripts / Prometheus) OR a
    # verified admin session (Bot Health dashboard card). Read-only probe.
    allowed, _actor = await _ops_actor(request, mutating=False)
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

    # 2b · REAL loop-progress telemetry (safety review) — the trading scan
    # loop and trade manager must be COMPLETING iterations, not merely
    # holding a lease. Works in both worker and in-process modes.
    loop_rows = {}
    async for lp in db.loop_progress.find({}):
        done = _as_dt(lp.get("last_iteration_completed_at"))
        ivl = int(lp.get("expected_interval_sec") or 60)
        fresh = bool(done and (now - done).total_seconds()
                     <= max(3 * ivl, 120))
        loop_rows[str(lp["_id"])] = {
            "ok": fresh,
            "last_iteration_completed_at": _iso(done),
            "last_duration_ms": lp.get("last_duration_ms"),
            "processed_count": lp.get("processed_count"),
            "expected_interval_sec": ivl}
    checks["loop_progress"] = {
        "ok": bool(loop_rows) and all(r["ok"] for r in loop_rows.values()),
        "detail": loop_rows or {"note": "no loop progress recorded yet"}}

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
    # r25 P2-02 · PANIC notification outbox — failed/unknown rows are incidents
    from routes.panic_routes import ops_outbox_health
    checks["panic_outbox"] = await ops_outbox_health(db)
    # r26 P2-02 · close protocol — a PANIC that had to run non-transactionally is an incident
    # until an operator resolves it (replica-set MongoDB) and acknowledges the row
    open_incidents = await db.close_protocol_incidents.count_documents({"resolved_at": {"$exists": False}})
    checks["close_protocol"] = {"ok": open_incidents == 0, "open_incidents": open_incidents,
                                "note": "PANIC executed without transactions" if open_incidents else None}

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

    # 6 · EA release verification chain (audit item 45) — the exact RC MQ5
    #     must have a 0-error Windows MetaEditor compile, EX5 SHA-256 and an
    #     Ed25519 signature recorded in docs/RELEASE_HASHES.json. Always
    #     reported; counts toward `ready` when REQUIRE_EA_RELEASE_PROOF=true
    #     (set on the Demo-Production-Proof / soak host) or in production.
    import os as _os
    ea_fails = []
    try:
        import json as _json
        _root = _os.path.dirname(_os.path.dirname(
            _os.path.dirname(_os.path.abspath(__file__))))
        _hashes = _os.path.join(_root, "docs", "RELEASE_HASHES.json")
        _ea = (_json.load(open(_hashes)) if _os.path.exists(_hashes)
               else {}).get("ea") or {}
        sys_path_added = _os.path.join(_root, "scripts")
        import sys as _sys
        if sys_path_added not in _sys.path:
            _sys.path.insert(0, sys_path_added)
        from verify_ea_release import check_entry
        ea_fails = check_entry(_ea)
    except Exception as e:  # noqa: BLE001
        ea_fails = [f"verifier unavailable: {e}"]
    ea_required = (
        _os.environ.get("REQUIRE_EA_RELEASE_PROOF", "").strip().lower()
        == "true")
    from app_env import is_production
    ea_required = ea_required or is_production()
    checks["ea_release"] = {
        "ok": (not ea_fails) if ea_required else True,
        "verified": not ea_fails,
        "enforced": ea_required,
        "failures": ea_fails or None,
        "note": None if not ea_fails else
        "MQL5 externally unverified — mandatory before Demo Production "
        "Proof: compile the exact RC MQ5 in Windows MetaEditor (0 errors), "
        "then scripts/verify_ea_release.py --sign"}

    # audit P1-5 — runtime release truth: the signed CI attestation that
    # deploy/lib.sh verified for THIS checkout (release/attestation.current.json)
    # is exposed here and compared with the running build SHA / image digest.
    from release_truth import release_attestation_check, rc_lock_check
    checks["release_attestation"] = release_attestation_check(is_production())
    checks["rc_lock"] = rc_lock_check(is_production())
    # P2-1 — repair-ledger anchor: latest signed anchor must still be reachable (no tail deletion)
    try:
        from health_repairs import verify_anchor
        checks["repair_ledger_anchor"] = await verify_anchor(db)
    except Exception as e:  # noqa: BLE001
        checks["repair_ledger_anchor"] = {"ok": False, "detail": f"anchor verification failed: {e}"}
    # round-5 P1 — broker truth: unresolved executions past threshold / position mismatch → fail closed
    try:
        from execution_truth import execution_truth_check
        checks["execution_truth"] = await execution_truth_check(db)
    except Exception as e:  # noqa: BLE001
        checks["execution_truth"] = {"ok": False, "detail": f"execution truth unavailable: {e}"}
    # round-9 P0-01/P0-02 — the canonical decision + inventory projection gate promotion
    try:
        from canonical_decision import decide_platform
        from inventory_projection import projection as inv_projection
        dec = await decide_platform(db)
        checks["canonical_decision"] = {"ok": dec["new_exposure_allowed"] if is_production() else True,
                                        "state": dec["state"], "reason_codes": dec["reason_codes"],
                                        "decision_id": dec["decision_id"], "enforced": is_production()}
        exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
        inv = await inv_projection(db, exp.get("scope_user_id"))
        checks["inventory"] = {"ok": not inv["blocking"], "counts": inv["counts"], "violations": inv["violations"],
                               "inventory_hash": inv["inventory_hash"], "approved_hash": inv["approved_hash"],
                               "approval_mode": inv["approval_mode"], "approved_mode": inv.get("approved_mode")}
        if inv["approval_mode"] == "single_admin":
            checks["inventory"]["note"] = "INVENTORY_APPROVAL_MODE=single_admin — operator chose single-operator approvals (no 4-eyes); every approval is stamped in the audit chain"
    except Exception as e:  # noqa: BLE001
        checks["canonical_decision"] = {"ok": False, "detail": f"decision unavailable: {e}"}
    # round-9 P1-04/P1-05 — Turnstile: no break-glass active/unreviewed; policy-enabled ⇒ keys complete
    try:
        from turnstile_break_glass import readiness_check as bg_readiness
        from turnstile_gate import is_enabled as ts_enabled, configuration_state
        checks["turnstile_break_glass"] = await bg_readiness(db)
        ts_on = await ts_enabled(db)
        checks["turnstile_config"] = {"ok": (not ts_on) or configuration_state() == "ready",
                                      "policy_enabled": ts_on, "state": configuration_state()}
    except Exception as e:  # noqa: BLE001
        checks["turnstile_break_glass"] = {"ok": False, "detail": f"turnstile state unavailable: {e}"}
    # audit r28 P2-02 — sealed provider secrets must be openable; production needs a dedicated master key
    try:
        from integrations_settings import readiness_check as vault_readiness, rewrap_readiness
        checks["secrets_vault"] = vault_readiness()
        checks["secrets_rewrap"] = await rewrap_readiness(db)
    except Exception as e:  # noqa: BLE001
        checks["secrets_vault"] = {"ok": False, "detail": f"vault state unavailable: {e}"}
    # audit v2 P2-02 — BLOCKER while any account still stores a plaintext bridge token
    try:
        from auth import bridge_plaintext_readiness
        checks["bridge_token_plaintext"] = await bridge_plaintext_readiness(db)
    except Exception as e:  # noqa: BLE001
        checks["bridge_token_plaintext"] = {"ok": False, "severity": "blocker",
                                            "detail": f"bridge token state unavailable: {e}"}
    # AT-15 rollback drill hook — can ONLY force a failure (fail-closed), never a pass.
    if os.environ.get("STOIC_DRILL_FORCE_READINESS_FAIL") == "1":
        checks["drill_forced_failure"] = {"ok": False,
                                          "detail": "STOIC_DRILL_FORCE_READINESS_FAIL=1 — rollback drill in progress"}

    ready = all(c["ok"] for c in checks.values())
    return JSONResponse(status_code=200 if ready else 503,
                        content={"ready": ready,
                                 "checked_at": now.isoformat(),
                                 "checks": checks})


@router.get("/ops/release-safety")
async def release_safety(request: Request):
    """Tier 16 — composite Release Safety Score (0-100) with promotion
    verdict: readiness checks, test manifest, validation campaigns,
    confidence calibration."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    comps = {}
    ready = await release_readiness(request)
    if hasattr(ready, "body"):
        import json as _json
        ready = _json.loads(ready.body)
    checks = (ready if isinstance(ready, dict) else {}).get("checks") or {}
    ok_n = sum(1 for c in checks.values() if c.get("ok"))
    comps["readiness"] = {"score": round(ok_n / max(1, len(checks)) * 100),
                          "detail": f"{ok_n}/{len(checks)} checks green"}
    try:
        import re
        from pathlib import Path
        manifest = Path(__file__).resolve().parents[2] / "docs" / "TEST_MANIFEST.md"
        with open(manifest) as f:
            head = f.read(2000)
        m = re.search(r"Total:\s*([\d,]+)\s*tests", head)
        n_tests = int(m.group(1).replace(",", "")) if m else 0
        comps["test_suite"] = {"score": 100 if n_tests >= 2000 else 60,
                               "detail": f"{n_tests} manifest-locked tests"}
    except Exception:  # noqa: BLE001
        comps["test_suite"] = {"score": 0, "detail": "manifest unreadable"}
    runs = await db.validation_runs.find(
        {}, sort=[("at", -1)]).limit(10).to_list(10)
    if runs:
        passed = sum(1 for r in runs if r.get("passed"))
        comps["validation_campaigns"] = {
            "score": round(passed / len(runs) * 100),
            "detail": f"{passed}/{len(runs)} recent campaigns passed"}
    else:
        comps["validation_campaigns"] = {
            "score": None,
            "detail": "no campaigns recorded — run the MT5 validation harness"}
    try:
        from calibration import compute_calibration
        admin = await db.users.find_one({"role": "admin"}, {"_id": 1})
        table = await compute_calibration(
            db, str(admin["_id"])) if admin else {}
        errs, ns = [], 0
        for ent in (table.values() if isinstance(table, dict) else []):
            for b in (ent.get("buckets") or []):
                n = b.get("n") or 0
                if n >= 30 and b.get("gap") is not None:
                    errs.append(abs(float(b["gap"])) * n)
                    ns += n
        if ns:
            err = sum(errs) / ns
            comps["calibration"] = {
                "score": round(max(0, 100 - err * 2)),
                "detail": f"avg calibration error {err:.1f}pts over {ns} trades"}
        else:
            comps["calibration"] = {
                "score": None,
                "detail": "not enough closed trades per confidence bucket"}
    except Exception as e:  # noqa: BLE001
        comps["calibration"] = {"score": None, "detail": f"unavailable: {e}"}

    scored = [c["score"] for c in comps.values() if c["score"] is not None]
    chaos = await db.chaos_drills.find_one({}, sort=[("at", -1)])
    if chaos:
        _partial = chaos.get("partial") or 0
        comps["chaos_drills"] = {
            "score": round(chaos["passed"] / max(1, chaos["total"]) * 100),
            "detail": f"{chaos['passed']}/{chaos['total']} failure drills "
                      f"passed"
                      + (f" · {_partial} PARTIAL (skipped assertions "
                         "are not passes)" if _partial else "")}
        scored.append(comps["chaos_drills"]["score"])
    else:
        comps["chaos_drills"] = {
            "score": None,
            "detail": "no chaos drills recorded — POST /api/ops/chaos/run"}
    score = round(sum(scored) / len(scored), 1) if scored else 0.0
    verdict = ("PROMOTE" if score >= 90 else
               "CANARY_ONLY" if score >= 75 else "BLOCK")
    return {"release_safety_score": score, "verdict": verdict,
            "threshold": {"promote": 90, "canary": 75},
            "components": comps,
            "principle": "only releases above the threshold can be promoted"}


@router.get("/ops/soak")
async def soak_report(request: Request, days: int = 14):
    """Phase 2.2 — long-soak report: memory growth, worker restarts,
    missed heartbeats, reconciliation backlog, suppressed failures."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    from datetime import timedelta
    days = min(max(int(days), 1), 45)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    samples = await db.ops_soak_samples.find(
        {"at": {"$gte": since}}).sort("at", 1).to_list(10000)
    rss = [s["rss_mb"] for s in samples if s.get("rss_mb") is not None]
    missed_hb = sum(1 for s in samples
                    if (s.get("hb_age_max_s") or 0) > 600)
    restarts = 0
    prev_alive = None
    for s in samples:
        a = s.get("workers_alive")
        if prev_alive is not None and a is not None and a < prev_alive:
            restarts += 1
        prev_alive = a if a is not None else prev_alive
    recon_backlog = await db.broker_deals.count_documents(
        {"financial_reconciliation_status": {"$nin": [None, "ok",
                                                      "reconciled"]}})
    from silent_failures import swallow_counters
    from soak_memory_watch import memory_trend
    chaos = await db.chaos_drills.find_one({}, sort=[("at", -1)]) or {}
    return {"days": days, "samples": len(samples),
            "memory": {"first_mb": rss[0] if rss else None,
                       "last_mb": rss[-1] if rss else None,
                       "max_mb": max(rss) if rss else None,
                       "growth_pct": (round((rss[-1] - rss[0]) / rss[0]
                                            * 100, 1)
                                      if len(rss) > 1 and rss[0] else None),
                       "watch": memory_trend(samples)},
            "worker_restart_events": restarts,
            "missed_heartbeat_samples": missed_hb,
            "reconciliation_backlog": recon_backlog,
            "suppressed_failures": swallow_counters(),
            "last_chaos": {"passed": chaos.get("passed"),
                           "partial": chaos.get("partial") or 0,
                           "total": chaos.get("total")},
            "note": ("Samples every ~10min (SOAK_SAMPLE_INTERVAL_SEC); "
                     "TTL 45 days. Run the soak for 14-30 days and watch "
                     "growth_pct, restarts and missed heartbeats.")}


@router.get("/ops/swallowed")
async def swallowed_exceptions(request: Request):
    """iter-103 — in-process counters of suppressed failures per component."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from silent_failures import swallow_counters
    return {"counters": swallow_counters()}


@router.get("/ops/chaos")
async def chaos_latest(request: Request):
    """Tier 14 — latest chaos-drill campaign results."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    doc = await get_db().chaos_drills.find_one({}, sort=[("at", -1)])
    if not doc:
        return {"results": [], "passed": 0, "total": 0, "at": None}
    at = doc.get("at")
    return {"results": doc.get("results") or [],
            "passed": doc.get("passed"),
            "partial": doc.get("partial") or 0,
            "total": doc.get("total"),
            "at": at.isoformat() if hasattr(at, "isoformat") else at}


@router.post("/ops/chaos/run")
async def chaos_run(request: Request):
    """Tier 14 — run the chaos-drill campaign (synthetic, self-cleaning)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from chaos_drills import run_drills
    return await run_drills(get_db())


@router.post("/ops/stress-test/run")
async def stress_test_run(request: Request, severity: str = "moderate"):
    """iter-158 — flash-crash simulator: replays a sudden market drop through
    the real defense layers and scores whether the bot stays calm."""
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from stress_test import run_stress_test, SEVERITIES
    if severity not in SEVERITIES:
        return JSONResponse(status_code=400,
                            content={"detail": f"severity must be one of "
                                               f"{sorted(SEVERITIES)}"})
    out = await run_stress_test(get_db(), severity=severity,
                                actor=actor or "admin")
    out["at"] = out["at"].isoformat()
    return out


@router.get("/ops/stress-test/runs")
async def stress_test_runs(request: Request):
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from stress_test import SEVERITIES
    db = get_db()
    runs = await db.stress_tests.find({}, {"_id": 0},
                                      sort=[("at", -1)]).limit(10).to_list(10)
    for r in runs:
        if hasattr(r.get("at"), "isoformat"):
            r["at"] = r["at"].isoformat()
    return {"severities": sorted(SEVERITIES), "runs": runs}


@router.get("/ops/slo")
async def slo_status(request: Request):
    """iter-158 — SLO compliance + error-budget consumption."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from slo import compute_slos
    return {"slos": await compute_slos(get_db())}


@router.get("/ops/releases")
async def releases_state(request: Request):
    """iter-160 — canary/staged rollout state + deployment history."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from release_channels import sync_channels
    db = get_db()
    st = await sync_channels(db)
    st.pop("_id", None)
    history = await db.release_history.find(
        {}, {"_id": 0}, sort=[("at", -1)]).limit(10).to_list(10)
    return {"state": st, "history": history}


@router.post("/ops/releases/canary")
async def releases_set_canary(payload: dict, request: Request):
    allowed, actor = await _ops_admin_step_up(request, "canary_set")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from release_channels import set_canary_agents
    ids = payload.get("agent_ids") or []
    if not isinstance(ids, list):
        return JSONResponse(status_code=400,
                            content={"detail": "agent_ids must be a list"})
    st = await set_canary_agents(get_db(), ids, actor=actor or "admin")
    st.pop("_id", None)
    return st


@router.post("/ops/releases/promote")
async def releases_promote(request: Request):
    allowed, actor = await _ops_admin_step_up(request, "release_promote")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from release_channels import promote
    try:
        st = await promote(get_db(), actor=actor or "admin")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    st.pop("_id", None)
    return st


@router.post("/ops/releases/rollback")
async def releases_rollback(request: Request):
    allowed, actor = await _ops_admin_step_up(request, "release_rollback")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from release_channels import rollback
    try:
        st = await rollback(get_db(), actor=actor or "admin")
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    st.pop("_id", None)
    return st


@router.get("/ops/deployment-health")
async def deployment_health_status(request: Request):
    """iter-161 — fleet health score, active bake watch, last auto-rollback
    and the auto-rollback policy."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    import deployment_health as dh
    db = get_db()
    st = await db.platform_state.find_one({"_id": "release_state"}) or {}
    last_auto = await db.release_history.find_one(
        {"event": "auto_rollback"}, {"_id": 0}, sort=[("at", -1)])
    trusted_uids = await dh._trusted_user_ids(db)
    trusted_health = await dh.score_fleet(
        db, dh._trusted_agent_filter(trusted_uids))
    return {"health": await dh.score_fleet(db),
            "trusted_health": trusted_health,
            "release_trust": {
                "configured": bool(trusted_uids),
                "trusted_tenants": len(trusted_uids),
                "auto_rollback_enabled": bool(trusted_uids)
                                         or trusted_health["fleet_size"] > 0},
            "watch": st.get("deploy_watch"),
            "last_auto_rollback": last_auto,
            "policy": {"bake_hours": dh.BAKE_HOURS,
                       "min_score": dh.MIN_SCORE,
                       "max_drop": dh.MAX_DROP,
                       "min_fail_tenants": dh.MIN_FAIL_TENANTS}}


@router.get("/ops/audit-anchor")
async def audit_anchor_status(request: Request):
    """iter-171 (#8) — verify the latest signed audit-chain anchor."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from audit_anchor import verify_latest
    return await verify_latest(get_db())


@router.post("/ops/audit-anchor")
async def audit_anchor_create(request: Request):
    """Create a signed anchor of the current audit-chain head on demand."""
    allowed, actor = await _ops_admin_step_up(request, "audit_anchor")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from audit_anchor import create_anchor
    doc = await create_anchor(get_db())
    if not doc:
        return {"anchored": False, "detail": "no audit-chain entries yet"}
    return {"anchored": True, **doc, "by": actor}


@router.get("/ops/runtime-stats")
async def runtime_stats(request: Request):
    """iter-183 — live process forensics: RSS, loop lag, restart history."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from runtime_watchdog import full_stats
    out = await full_stats(get_db())
    from workers.registry import REGISTRY
    out["background_tasks"] = REGISTRY.stats()
    return out


@router.get("/ops/deploy-preflight")
async def deploy_preflight(request: Request, signer_health: bool = False):
    """iter-181 — production guardrail preflight (deploys never bounce).
    ?signer_health=true adds the non-signing external-signer identity check (round 9 P1-01)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from deploy_preflight import run_preflight
    import asyncio
    return await asyncio.to_thread(run_preflight, bool(signer_health))


@router.get("/ops/deploy-watch")
async def deploy_watch_status(request: Request):
    """Deploy Watchdog — latest watch (watching|live|stalled|cancelled)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from deploy_watch import allowed_hosts, latest
    return {"watch": await latest(get_db()),
            "allowed_hosts": sorted(allowed_hosts()),
            "interval_sec": int(os.environ.get("DEPLOY_WATCH_INTERVAL_SEC", "30")),
            "workers_in_process": (os.environ.get("BACKGROUND_WORKERS_IN_PROCESS") or "").lower() == "true"}


@router.post("/ops/deploy-watch/arm")
async def deploy_watch_arm(request: Request):
    """Arm a watch: admin session (email verified) → polls target/api/health
    until expected_sha is live, e-mails the arming admin."""
    from auth import get_current_user, require_admin
    try:
        user = await get_current_user(request)
    except Exception:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    require_admin(user)
    db = get_db()
    from bson import ObjectId
    full = await db.users.find_one({"_id": ObjectId(user["id"])}, {"email": 1, "email_verified": 1})
    if (full or {}).get("email_verified") is not True:
        return JSONResponse(status_code=403, content={"detail": {
            "code": "email_not_verified",
            "message": "Verify your e-mail before arming the deploy watchdog."}})
    from email_sender import is_configured
    if not is_configured():
        return JSONResponse(status_code=503, content={"detail": {
            "code": "email_not_configured", "message": "RESEND_API_KEY is not configured."}})
    body = await request.json()
    from deploy_watch import arm
    try:
        doc = await arm(db, target_url=body.get("target_url"),
                        expected_sha=body.get("expected_sha"),
                        notify_email=full["email"], armed_by=full["email"],
                        timeout_h=int(body.get("timeout_h") or 24))
    except (ValueError, TypeError) as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    return {"watch": doc}


@router.post("/ops/deploy-watch/cancel")
async def deploy_watch_cancel(request: Request):
    from auth import get_current_user, require_admin
    try:
        user = await get_current_user(request)
    except Exception:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    require_admin(user)
    from deploy_watch import cancel
    doc = await cancel(get_db(), user.get("email") or "admin")
    if not doc:
        return JSONResponse(status_code=404, content={"detail": "no active watch"})
    return {"watch": doc}


@router.get("/ops/agent-certs")
async def agent_cert_posture(request: Request):
    """iter-176 — mTLS cert-expiry posture (rotation policy visibility)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from agent_mtls import certs_expiring
    return await certs_expiring(get_db())


@router.get("/ops/turnstile-diag")
async def turnstile_diag(request: Request):
    """Turnstile health: secret-key validity probe + recent rejection codes."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": (
            "Admin session required — sign in as an admin and use the "
            "'Run diagnostics' button on Admin → Users (Turnstile card), "
            "or send a valid METRICS_TOKEN / OPS_DEPLOY_TOKEN bearer.")})
    import turnstile_gate
    return await turnstile_gate.diagnose(get_db())


@router.get("/ops/query-perf")
async def query_perf_status(request: Request):
    """iter-171 (#9) — slow-query monitoring counters for this API process."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from query_perf import query_perf_stats
    return query_perf_stats()



@router.post("/ops/agents/{agent_id}/release-trust")
async def set_agent_release_trust(agent_id: str, payload: dict,
                                  request: Request):
    """4th-audit hardening — designate/undesignate an agent as release-trusted.
    ONLY release-trusted agents' telemetry can trigger an automatic rollback,
    so tenant-controlled agents can never move fleet release state."""
    allowed, actor = await _ops_admin_step_up(request, "release_trust")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    trusted = bool(payload.get("trusted", True))
    r = await get_db().vps_agents.update_one(
        {"agent_id": agent_id}, {"$set": {"release_trusted": trusted}})
    if r.matched_count == 0:
        return JSONResponse(status_code=404, content={"detail": "agent not found"})
    return {"agent_id": agent_id, "release_trusted": trusted, "by": actor}


@router.post("/ops/deployment-health/check")
async def deployment_health_check(request: Request):
    """Run the bake-window evaluation immediately (same code the scheduler
    runs every 30 min)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from deployment_health import watch_deployment
    return {"result": await watch_deployment(get_db())}


@router.get("/ops/model-version")
async def model_version_status(request: Request):
    """iter-161 — current AI model version + registry of versions seen."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from model_lineage import model_version
    rows = await get_db().model_code_versions.find(
        {}).sort("first_seen", -1).limit(10).to_list(10)
    registry = [{"version": r.pop("_id"), **r} for r in rows]
    return {"current": model_version(), "registry": registry}


@router.post("/ops/signals/{signal_id}/replay-validate")
async def signal_replay_validate(signal_id: str, request: Request):
    """iter-161 — prove a historical AI decision is reproducible against
    the current model (version, feature lineage, explanation)."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from model_lineage import replay_validate
    out = await replay_validate(get_db(), signal_id)
    if out is None:
        return JSONResponse(status_code=404,
                            content={"detail": "signal not found"})
    return out


@router.post("/ops/agents/{agent_id}/config")
async def set_agent_desired_config(agent_id: str, payload: dict,
                                   request: Request):
    """iter-160 — central config sync: agents apply this on next heartbeat."""
    allowed, actor = await _ops_admin_step_up(request, "agent_config_push")
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    ALLOWED = {"mt5_supervise", "mt5_path", "telemetry_interval_sec",
               "update_checks_enabled"}
    cfg = {k: v for k, v in (payload or {}).items() if k in ALLOWED}
    if not cfg:
        return JSONResponse(status_code=400,
                            content={"detail": f"allowed keys: {sorted(ALLOWED)}"})
    db = get_db()
    r = await db.vps_agents.update_one(
        {"agent_id": agent_id},
        {"$set": {"desired_config": cfg,
                  "desired_config_by": actor or "admin"}})
    if r.matched_count == 0:
        return JSONResponse(status_code=404, content={"detail": "agent not found"})
    return {"agent_id": agent_id, "desired_config": cfg}


@router.get("/trades/{trade_id}/timeline")
async def trade_timeline_ep(trade_id: str, request: Request):
    """iter-160 — full order-lifecycle audit for one trade. Owners see their
    own trades; ops actors can inspect any trade."""
    from auth import get_current_user
    user = await get_current_user(request)
    from trade_timeline import assemble_timeline
    db = get_db()
    tl = await assemble_timeline(db, trade_id)
    if not tl:
        return JSONResponse(status_code=404, content={"detail": "trade not found"})
    if tl["user_id"] != user["id"]:
        from auth import require_admin
        if user.get("role") != "admin":
            return JSONResponse(status_code=403, content={"detail": "forbidden"})
        require_admin(user)    # cross-tenant read needs the MFA-verified admin
    return tl


@router.get("/ops/scheduled-drills")
async def scheduled_drills_status(request: Request):
    """iter-159 — nightly resilience suite history."""
    allowed, _actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    runs = await db.scheduled_drill_runs.find(
        {}, {"_id": 0}, sort=[("at", -1)]).limit(7).to_list(7)
    for r in runs:
        if hasattr(r.get("at"), "isoformat"):
            r["at"] = r["at"].isoformat()
    return {"runs": runs}


@router.post("/ops/scheduled-drills/run")
async def scheduled_drills_run_now(request: Request):
    """Manual trigger of the full nightly suite (admin)."""
    allowed, actor = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    from scheduled_drills import run_nightly_suite
    out = await run_nightly_suite(get_db(), actor=actor or "admin")
    out["at"] = out["at"].isoformat()
    return out


@router.post("/stress-test/run")
async def user_stress_test_run(request: Request, severity: str = "moderate"):
    """iter-159 — customer-facing crash test against THEIR own config."""
    from auth import get_current_user
    user = await get_current_user(request)
    from stress_test import run_user_stress_test, SEVERITIES
    if severity not in SEVERITIES:
        return JSONResponse(status_code=400,
                            content={"detail": f"severity must be one of "
                                               f"{sorted(SEVERITIES)}"})
    db = get_db()
    from security import rate_limit
    await rate_limit(db, "user_stress_test", user["id"], 10, 3600,
                     message="Stress-test limit reached — try again later.",
                     request=request)
    out = await run_user_stress_test(db, user["id"], severity=severity)
    out["at"] = out["at"].isoformat()
    return out


@router.get("/stress-test/runs")
async def user_stress_test_runs(request: Request):
    from auth import get_current_user
    user = await get_current_user(request)
    db = get_db()
    runs = await db.stress_tests.find(
        {"user_id": user["id"]}, {"_id": 0},
        sort=[("at", -1)]).limit(5).to_list(5)
    for r in runs:
        if hasattr(r.get("at"), "isoformat"):
            r["at"] = r["at"].isoformat()
    return {"runs": runs}


@router.get("/ops/alerts")
async def list_alerts(request: Request, include_acked: bool = False,
                      scope: str = "real",
                      limit: int = 100):
    allowed, _ = await _ops_actor(request)
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    db = get_db()
    from synthetic_data import alert_scope_filter
    sf = alert_scope_filter(scope)
    q = {**sf} if include_acked else {"acked_at": None, **sf}
    out = []
    async for a in db.ops_alerts.find(q).sort("created_at", -1).limit(
            max(1, min(limit, 500))):
        a["id"] = str(a.pop("_id"))
        out.append(a)
    return {"alerts": out, "scope": (scope or "real").lower(),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "unacked": await db.ops_alerts.count_documents(
                {"acked_at": None, **sf}),
            "unacked_critical": await db.ops_alerts.count_documents(
                {"acked_at": None, "severity": "critical",
                 "synthetic": {"$ne": True}}),
            "synthetic_unacked": await db.ops_alerts.count_documents(
                {"acked_at": None, "synthetic": True})}


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
