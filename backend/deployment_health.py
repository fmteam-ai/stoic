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


async def _trusted_user_ids(db) -> set:
    """Operator-designated release-trust tenants (env RELEASE_TRUST_USER_IDS)
    plus any agent explicitly flagged release_trusted=True by an admin. Only
    these move release state automatically — tenant-controlled telemetry
    (SEC-001/002, 4th audit) is display-only and never triggers rollback."""
    ids = {u.strip() for u in
           (os.environ.get("RELEASE_TRUST_USER_IDS") or "").split(",")
           if u.strip()}
    async for a in db.vps_agents.find(
            {"release_trusted": True, "revoked": {"$ne": True}},
            {"user_id": 1}):
        if a.get("user_id"):
            ids.add(str(a["user_id"]))
    return ids


def _trusted_agent_filter(trusted_user_ids: set) -> dict:
    """Mongo filter for release-trusted agents."""
    ors = [{"release_trusted": True}]
    if trusted_user_ids:
        ors.append({"user_id": {"$in": list(trusted_user_ids)}})
    return {"revoked": {"$ne": True}, "$or": ors}


async def distinct_fail_tenants(db, since_iso, digests=None,
                                only_user_ids=None) -> int:
    """Number of DISTINCT owning tenants (meta.user_id) that raised a fleet
    deployment_failed alert since `since_iso`, optionally scoped to a set of
    release `digests` (meta.sha256) and to a trusted `only_user_ids` set.
    Counting tenants — not agent_ids — defeats a single customer minting many
    agents; restricting to trusted tenants defeats fake-account corroboration."""
    q = {"kind": "deployment_failed", "created_at": {"$gte": since_iso}}
    if digests:
        q["meta.sha256"] = {"$in": list(digests)}
    ids = await db.ops_alerts.distinct("meta.user_id", q)
    ids = [i for i in ids if i]
    if only_user_ids is not None:
        ids = [i for i in ids if str(i) in only_user_ids]
    return len(ids)


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


async def score_fleet(db, agent_filter=None) -> dict:
    """Weighted health score: 40% heartbeat freshness, 30% MT5 connectivity,
    20% deploy-failure alerts, 10% command-failure alerts (last 2h).
    `agent_filter` restricts which agents are aggregated (e.g. trusted-only
    for the auto-rollback decision)."""
    now = _now()
    total = fresh = mt5_ok = mt5_reporting = 0
    q = agent_filter if agent_filter is not None else {"revoked": {"$ne": True}}
    async for a in db.vps_agents.find(
            q, {"last_heartbeat": 1, "last_metrics": 1}).limit(500):
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
    # Full-fleet score is display-only. The auto-rollback DECISION trusts ONLY
    # operator-designated agents/tenants so tenant-controlled telemetry can't
    # force a rollback of a healthy release (4th audit SEC-001/SEC-002).
    trusted_uids = await _trusted_user_ids(db)
    trusted_health = await score_fleet(db, _trusted_agent_filter(trusted_uids))
    display_health = await score_fleet(db)
    watch_digests = set((watch.get("artifacts") or {}).values())
    fail_tenants = 0
    if started:
        fail_tenants = await distinct_fail_tenants(
            db, started, watch_digests, only_user_ids=trusted_uids)
    have_trust = bool(trusted_uids) or trusted_health["fleet_size"] > 0
    corroborated = have_trust and fail_tenants >= MIN_FAIL_TENANTS
    degraded = (have_trust and trusted_health["fleet_size"] > 0
                and (trusted_health["score"] < MIN_SCORE
                     or baseline - trusted_health["score"] >= MAX_DROP))
    if corroborated or degraded:
        health = trusted_health
        reason = (f"{fail_tenants} trusted tenants reported deploy failure "
                  f"during bake" if corroborated
                  else (f"trusted-fleet health {trusted_health['score']} vs "
                        f"baseline {baseline} (floor {MIN_SCORE}, max drop "
                        f"{MAX_DROP})"))
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
    # No trusted-fleet signal but the FULL fleet looks degraded → surface a
    # warning for manual operator review; never auto-rollback on untrusted
    # telemetry alone (fail-safe against tenant-driven false positives).
    if not have_trust and display_health["fleet_size"] > 0 and (
            display_health["score"] < MIN_SCORE
            or baseline - display_health["score"] >= MAX_DROP):
        from alerting import raise_alert
        await raise_alert(
            db, "deployment_review_needed", "warning",
            f"Full-fleet health {display_health['score']} degraded during bake "
            f"but no release-trusted agents are configured — manual review "
            f"required (auto-rollback suppressed).",
            dedup_key=f"deploy_review_{watch.get('started_at')}")
    if started and _now() - started >= timedelta(hours=bake):
        await _clear_watch(db)
        from release_channels import _history
        await _history(db, "bake_complete",
                       {"health": display_health, "baseline": baseline})
        logger.info("deploy bake complete — health %.1f",
                    display_health["score"])
        return {"status": "bake_complete", "health": display_health}
    return {"status": "baking", "health": display_health,
            "trusted_health": trusted_health, "baseline": baseline,
            "started_at": watch.get("started_at"), "bake_hours": bake}
