"""iter-161 — fleet deployment health scoring + AUTOMATIC rollback.

After every promotion the release enters a bake window (deploy_watch in
platform_state). During the bake the scheduler recomputes a 0-100 fleet
health score (heartbeat freshness, MT5 connectivity, deployment/command
failure alerts). Any deployment_failed alert OR a score collapse triggers
an automatic rollback to previous stable (channel pinned) + critical alert.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("deployment-health")

STATE_ID = "release_state"
BAKE_HOURS = float(os.environ.get("DEPLOY_BAKE_HOURS", "4"))
MIN_SCORE = float(os.environ.get("DEPLOY_MIN_HEALTH", "60"))
MAX_DROP = float(os.environ.get("DEPLOY_MAX_HEALTH_DROP", "25"))
HB_FRESH_SEC = 300
# SEC (3rd audit) — auto-rollback only fires after INDEPENDENT corroboration
# from distinct TENANTS (not tenant-controlled agent_ids, which one customer
# can mint without limit) AND only for the exact release digest under
# evaluation. A single tenant — however many agents it spins up — counts once.
MIN_FAIL_TENANTS = int(os.environ.get("DEPLOY_MIN_FAIL_TENANTS",
                       os.environ.get("DEPLOY_MIN_FAIL_AGENTS", "2")))
MIN_FAIL_AGENTS = MIN_FAIL_TENANTS  # back-compat alias


async def distinct_fail_tenants(db, since_iso, digests=None) -> int:
    """Number of DISTINCT owning tenants (meta.user_id) that raised a fleet
    deployment_failed alert since `since_iso`, optionally scoped to a set of
    release `digests` (meta.sha256). Counting tenants — not agent_ids —
    defeats a single customer minting many agents to fake corroboration."""
    q = {"kind": "deployment_failed", "created_at": {"$gte": since_iso}}
    if digests:
        q["meta.sha256"] = {"$in": list(digests)}
    ids = await db.ops_alerts.distinct("meta.user_id", q)
    return len([i for i in ids if i])


# Legacy name kept for callers/tests that scoped by agent (now tenant-based).
async def distinct_fail_agents(db, since_iso, digests=None) -> int:
    return await distinct_fail_tenants(db, since_iso, digests)


def _now():
    return datetime.now(timezone.utc)


def _aware(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def score_fleet(db) -> dict:
    """Weighted health score: 40% heartbeat freshness, 30% MT5 connectivity,
    20% deploy-failure alerts, 10% command-failure alerts (last 2h)."""
    now = _now()
    total = fresh = mt5_ok = mt5_reporting = 0
    async for a in db.vps_agents.find(
            {"revoked": {"$ne": True}},
            {"last_heartbeat": 1, "last_metrics": 1}).limit(500):
        total += 1
        hb = _aware(a.get("last_heartbeat"))
        if hb and (now - hb).total_seconds() <= HB_FRESH_SEC:
            fresh += 1
        m = a.get("last_metrics") or {}
        if m.get("mt5_connected") is not None:
            mt5_reporting += 1
            if m.get("mt5_connected"):
                mt5_ok += 1
    two_h = now - timedelta(hours=2)
    deploy_fails = await db.ops_alerts.count_documents(
        {"kind": "deployment_failed", "created_at": {"$gte": two_h}})
    cmd_fails = await db.ops_alerts.count_documents(
        {"kind": "vps_command_failed", "created_at": {"$gte": two_h}})
    components = {
        "heartbeat_fresh": f"{fresh}/{total}",
        "mt5_connected": f"{mt5_ok}/{mt5_reporting}",
        "deployment_failed_2h": deploy_fails,
        "command_failed_2h": cmd_fails,
    }
    if total == 0:
        return {"score": 100.0, "fleet_size": 0, "components": components,
                "note": "no host agents reporting — score neutral",
                "at": now.isoformat()}
    hb_pct = fresh / total
    mt5_pct = (mt5_ok / mt5_reporting) if mt5_reporting else 1.0
    deploy_pen = min(1.0, deploy_fails / max(1, total))
    cmd_pen = min(1.0, cmd_fails / max(1, total))
    score = round(100 * (0.40 * hb_pct + 0.30 * mt5_pct
                         + 0.20 * (1 - deploy_pen)
                         + 0.10 * (1 - cmd_pen)), 1)
    return {"score": score, "fleet_size": total, "components": components,
            "at": now.isoformat()}


async def start_watch(db, artifacts: dict, actor: str) -> dict:
    """Record a pre-deployment baseline and open the bake window."""
    base = await score_fleet(db)
    watch = {"started_at": _now().isoformat(),
             "baseline_score": base["score"],
             "artifacts": artifacts, "by": actor,
             "bake_hours": BAKE_HOURS}
    await db.platform_state.update_one(
        {"_id": STATE_ID}, {"$set": {"deploy_watch": watch}}, upsert=True)
    logger.info("deploy watch opened by %s (baseline %.1f, bake %.1fh)",
                actor, base["score"], BAKE_HOURS)
    return watch


async def _clear_watch(db):
    await db.platform_state.update_one(
        {"_id": STATE_ID}, {"$unset": {"deploy_watch": ""}})


async def watch_deployment(db) -> dict | None:
    """Called by the scheduler. Auto-rolls back if the deployment degrades
    fleet health during the bake window; clears the watch when the bake
    completes cleanly."""
    st = await db.platform_state.find_one({"_id": STATE_ID}) or {}
    watch = st.get("deploy_watch")
    if not watch:
        return None
    started = _aware(watch.get("started_at"))
    bake = float(watch.get("bake_hours") or BAKE_HOURS)
    baseline = float(watch.get("baseline_score") or 100)
    health = await score_fleet(db)
    # Only failures reported for THIS release's artifact digests count, and
    # only DISTINCT tenants corroborate (a single customer's agents == 1).
    watch_digests = set((watch.get("artifacts") or {}).values())
    fail_tenants = 0
    if started:
        fail_tenants = await distinct_fail_tenants(db, started, watch_digests)
    corroborated = fail_tenants >= MIN_FAIL_TENANTS
    degraded = (health["fleet_size"] > 0
                and (health["score"] < MIN_SCORE
                     or baseline - health["score"] >= MAX_DROP))
    if corroborated or degraded:
        reason = (f"{fail_tenants} distinct tenants reported deploy failure "
                  f"during bake" if corroborated
                  else (f"fleet health {health['score']} vs baseline "
                        f"{baseline} (floor {MIN_SCORE}, max drop {MAX_DROP})"))
        from release_channels import _history, rollback
        try:
            await rollback(db, actor="auto-health")
            outcome = "rolled_back"
        except ValueError as e:
            outcome = f"rollback_unavailable: {e}"
        await _clear_watch(db)
        await _history(db, "auto_rollback",
                       {"reason": reason, "health": health,
                        "baseline": baseline, "outcome": outcome})
        from alerting import raise_alert
        await raise_alert(
            db, "deployment_auto_rollback", "critical",
            f"Deployment health degraded — automatic rollback: {reason} "
            f"({outcome})",
            dedup_key=f"auto_rb_{watch.get('started_at')}")
        logger.error("AUTO-ROLLBACK: %s (%s)", reason, outcome)
        return {"status": "auto_rollback", "reason": reason,
                "outcome": outcome, "health": health}
    if started and _now() - started >= timedelta(hours=bake):
        await _clear_watch(db)
        from release_channels import _history
        await _history(db, "bake_complete",
                       {"health": health, "baseline": baseline})
        logger.info("deploy bake complete — health %.1f", health["score"])
        return {"status": "bake_complete", "health": health}
    return {"status": "baking", "health": health, "baseline": baseline,
            "started_at": watch.get("started_at"), "bake_hours": bake}
