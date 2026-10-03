"""Ops Console (iter-137) — GET /api/admin/ops-console.

One admin endpoint aggregating everything an AI support engineer needs to
diagnose issues without logging into a VPS: VPS fleet, agents, MT5/EA
bridge, deployments, command queue, bots, workers, risk alerts, API
latency, MongoDB, Stripe webhooks and subscriptions.
"""
import logging
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends

from auth import get_current_user, require_admin
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops-console"])


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _age_sec(value, now: datetime):
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, int((now - value).total_seconds()))
    return None


@router.get("/admin/ops-console")
async def ops_console(user=Depends(get_current_user)):
    require_admin(user)
    db = get_db()
    now = datetime.now(timezone.utc)
    iso_now = _iso(now)
    out = {"generated_at": iso_now}

    # ── VPS fleet + agents ──────────────────────────────────────────────
    online_cut = _iso(now - timedelta(minutes=3))
    agents_total = await db.vps_agents.count_documents({"revoked": {"$ne": True}})
    agents_online = await db.vps_agents.count_documents(
        {"revoked": {"$ne": True}, "last_heartbeat": {"$gte": online_cut}})
    offline = []
    async for a in (db.vps_agents
                    .find({"revoked": {"$ne": True},
                           "$or": [{"last_heartbeat": {"$lt": online_cut}},
                                   {"last_heartbeat": None}]},
                          {"agent_id": 1, "last_heartbeat": 1, "user_id": 1})
                    .sort("last_heartbeat", -1).limit(10)):
        offline.append({"agent_id": a.get("agent_id"),
                        "heartbeat_age_sec": _age_sec(a.get("last_heartbeat"), now)})
    out["vps"] = {"agents_total": agents_total, "online": agents_online,
                  "offline": agents_total - agents_online,
                  "offline_list": offline}

    # ── Deployments ─────────────────────────────────────────────────────
    dep_states = {}
    async for row in db.vps_deployments.aggregate(
            [{"$group": {"_id": "$state", "n": {"$sum": 1}}}]):
        dep_states[str(row["_id"])] = row["n"]
    failed_states = [s for s in dep_states if "fail" in s.lower() or "error" in s.lower()]
    recent_failed = []
    if failed_states:
        async for d in (db.vps_deployments
                        .find({"state": {"$in": failed_states}},
                              {"deployment_id": 1, "state": 1, "provider": 1})
                        .sort("_id", -1).limit(5)):
            recent_failed.append({"deployment_id": d.get("deployment_id"),
                                  "state": d.get("state"),
                                  "provider": d.get("provider")})
    active_dep_states = ("requested", "queued", "provisioning", "installing",
                         "configuring", "pending")
    success_states = [s for s in dep_states
                      if any(w in s.lower() for w in ("active", "complete",
                                                      "succeed", "ready",
                                                      "running"))]

    async def _dep_rate(hours: int):
        since = _iso(now - timedelta(hours=hours))
        done = await db.vps_deployments.count_documents(
            {"state": {"$in": success_states or ["__none__"]},
             "created_at": {"$gte": since}})
        bad = await db.vps_deployments.count_documents(
            {"state": {"$in": failed_states or ["__none__"]},
             "created_at": {"$gte": since}})
        total = done + bad
        return {"succeeded": done, "failed": bad,
                "rate_pct": round(done * 100.0 / total, 1) if total else None}

    out["deployments"] = {
        "by_state": dep_states,
        "in_progress": sum(v for k, v in dep_states.items()
                           if k.lower() in active_dep_states),
        "failed": sum(dep_states.get(s, 0) for s in failed_states),
        "recent_failed": recent_failed,
        "success_24h": await _dep_rate(24),
        "success_7d": await _dep_rate(24 * 7),
    }

    # ── Agent command queue ─────────────────────────────────────────────
    day_ago = _iso(now - timedelta(hours=24))
    queue_depth = await db.agent_commands.count_documents(
        {"status": {"$in": ["queued", "pending", "delivered"]}})
    cmd_failed_24h = await db.agent_commands.count_documents(
        {"status": "failed", "created_at": {"$gte": day_ago}})
    oldest_queued = await db.agent_commands.find_one(
        {"status": {"$in": ["queued", "pending"]}}, sort=[("created_at", 1)])
    out["command_queue"] = {
        "depth": queue_depth,
        "failed_24h": cmd_failed_24h,
        "oldest_queued_age_sec": _age_sec(
            (oldest_queued or {}).get("created_at"), now),
    }

    # ── MT5 / EA bridge ─────────────────────────────────────────────────
    hb_cut = _iso(now - timedelta(minutes=3))
    acc_total = await db.accounts.count_documents({})
    acc_connected = await db.accounts.count_documents(
        {"last_heartbeat": {"$gte": hb_cut}})
    stale = []
    async for a in (db.accounts
                    .find({"$or": [{"last_heartbeat": {"$lt": hb_cut}},
                                   {"last_heartbeat": {"$exists": False}}]},
                          {"display_name": 1, "last_heartbeat": 1,
                           "ea_version": 1})
                    .sort("last_heartbeat", -1).limit(10)):
        stale.append({"display_name": a.get("display_name"),
                      "heartbeat_age_sec": _age_sec(a.get("last_heartbeat"), now),
                      "ea_version": a.get("ea_version")})
    ea_versions = {}
    async for row in db.accounts.aggregate(
            [{"$match": {"ea_version": {"$exists": True, "$ne": None}}},
             {"$group": {"_id": "$ea_version", "n": {"$sum": 1}}}]):
        ea_versions[str(row["_id"])] = row["n"]
    newest = await db.accounts.find_one({"last_heartbeat": {"$ne": None}},
                                        sort=[("last_heartbeat", -1)])
    out["mt5_bridge"] = {
        "accounts_total": acc_total,
        "connected": acc_connected,
        "disconnected": acc_total - acc_connected,
        "freshest_heartbeat_age_sec": _age_sec(
            (newest or {}).get("last_heartbeat"), now),
        "ea_versions": ea_versions,
        "stale_list": stale,
    }

    # ── Bots + workers ──────────────────────────────────────────────────
    bots_active = await db.bot_configs.count_documents({"active": True})
    pulse_cut = _iso(now - timedelta(minutes=5))
    bots_pulsing = await db.bot_configs.count_documents(
        {"active": True, "_last_pulse.ts": {"$gte": pulse_cut}})
    workers = []
    async for w in db.worker_leases.find({}).limit(10):
        exp = w.get("expires_at")
        alive = (_age_sec(exp, now) == 0) if exp else False
        if isinstance(exp, datetime):
            alive = (exp.replace(tzinfo=exp.tzinfo or timezone.utc) > now)
        elif isinstance(exp, str):
            alive = exp > iso_now
        workers.append({"holder": str(w.get("holder") or w.get("_id")),
                        "alive": bool(alive),
                        "renewed_age_sec": _age_sec(w.get("renewed_at"), now)})
    out["engine"] = {"bots_active": bots_active, "bots_pulsing": bots_pulsing,
                     "bots_silent": bots_active - bots_pulsing,
                     "workers": workers,
                     "workers_alive": sum(1 for w in workers if w["alive"])}

    # ── Risk alerts ─────────────────────────────────────────────────────
    sev_counts = {}
    async for row in db.ops_alerts.aggregate(
            [{"$match": {"acked_at": None, "synthetic": {"$ne": True}}},
             {"$group": {"_id": "$severity", "n": {"$sum": 1}}}]):
        sev_counts[str(row["_id"])] = row["n"]
    latest_alerts = []
    async for a in (db.ops_alerts.find(
            {"acked_at": None, "synthetic": {"$ne": True}})
                    .sort("last_seen_at", -1).limit(8)):
        latest_alerts.append({"kind": a.get("kind"),
                              "severity": a.get("severity"),
                              "message": (a.get("message") or "")[:160],
                              "occurrences": a.get("occurrences", 1),
                              "age_sec": _age_sec(a.get("last_seen_at"), now)})
    out["alerts"] = {"unacked_by_severity": sev_counts,
                     "unacked_total": sum(sev_counts.values()),
                     "latest": latest_alerts}

    # ── API latency ─────────────────────────────────────────────────────
    import ops_metrics
    out["api"] = ops_metrics.summary()

    # ── MongoDB ─────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        await db.command("ping")
        ping_ms = round((time.perf_counter() - t0) * 1000, 1)
        stats = await db.command("dbStats")
        out["mongo"] = {"ok": True, "ping_ms": ping_ms,
                        "collections": stats.get("collections"),
                        "data_mb": round(stats.get("dataSize", 0) / 1048576, 1),
                        "index_mb": round(stats.get("indexSize", 0) / 1048576, 1)}
    except Exception as e:  # noqa: BLE001
        logger.warning("ops mongo health check failed: %s", e)
        out["mongo"] = {"ok": False, "error": "mongo health check failed"}
    try:
        from seed import ticket_index_check
        out["unique_ticket_index"] = await ticket_index_check(db)   # A6/H12
    except Exception as e:  # noqa: BLE001
        out["unique_ticket_index"] = {"ok": False, "detail": f"ticket index state unavailable: {e}"}

    # ── Stripe webhooks + payments ──────────────────────────────────────
    wh_counts = {}
    async for row in db.stripe_webhook_events.aggregate(
            [{"$match": {"at": {"$gte": day_ago}}},
             {"$group": {"_id": "$type", "n": {"$sum": 1}}}]):
        wh_counts[str(row["_id"])] = row["n"]
    last_wh = await db.stripe_webhook_events.find_one(sort=[("at", -1)])
    paid_24h = await db.payment_transactions.count_documents(
        {"payment_status": "paid", "created_at": {"$gte": day_ago}})
    stuck_initiated = await db.payment_transactions.count_documents(
        {"payment_status": "initiated",
         "created_at": {"$lt": _iso(now - timedelta(hours=1))}})
    out["stripe"] = {"webhooks_24h": wh_counts,
                     "last_webhook_age_sec": _age_sec(
                         (last_wh or {}).get("at"), now),
                     "paid_24h": paid_24h,
                     "stale_initiated_sessions": stuck_initiated}

    # ── Subscriptions ───────────────────────────────────────────────────
    subs_active = await db.subscriptions.count_documents(
        {"valid_until": {"$gt": iso_now},
         "current_plan_id": {"$nin": [None, "admin_grandfather"]}})
    by_tier = {}
    async for row in db.subscriptions.aggregate(
            [{"$match": {"valid_until": {"$gt": iso_now},
                         "current_plan_id": {"$nin": [None, "admin_grandfather"]}}},
             {"$group": {"_id": "$current_plan_id", "n": {"$sum": 1}}}]):
        by_tier[str(row["_id"])] = row["n"]
    expiring_7d = await db.subscriptions.count_documents(
        {"valid_until": {"$gt": iso_now,
                         "$lt": _iso(now + timedelta(days=7))},
         "current_plan_id": {"$nin": [None, "admin_grandfather"]}})
    out["subscriptions"] = {"active": subs_active, "by_plan": by_tier,
                            "expiring_7d": expiring_7d}

    # ── Billing events feed ─────────────────────────────────────────────
    feed = []
    async for tx in (db.payment_transactions
                     .find({"payment_status": {"$in": ["paid", "refunded",
                                                       "revoked"]}},
                           {"user_email": 1, "plan_id": 1, "amount_usd": 1,
                            "payment_status": 1, "created_at": 1})
                     .sort("_id", -1).limit(8)):
        feed.append({"email": tx.get("user_email"),
                     "plan_id": tx.get("plan_id"),
                     "amount_usd": tx.get("amount_usd"),
                     "status": tx.get("payment_status"),
                     "at": tx.get("created_at")})
    out["billing_feed"] = feed

    # ── Security panel ──────────────────────────────────────────────────
    failed_logins = 0
    async for row in db.rate_limits.aggregate(
            [{"$match": {"_id": {"$regex": "^(login|2fa|email_otp_verify):"}}},
             {"$group": {"_id": None, "n": {"$sum": "$n"}}}]):
        failed_logins = row["n"]
    suspended = await db.users.count_documents({"suspended": True})
    from audit_chain import verify_chain
    chain = await verify_chain(db)
    from login_otp import is_enabled as _otp_enabled
    import os as _os
    out["security"] = {
        "failed_auth_recent": failed_logins,
        "suspended_users": suspended,
        "audit_chain_ok": chain["ok"],
        "audit_chain_entries": chain["chained_entries"],
        "admin_mfa_enforced": _os.environ.get(
            "ADMIN_MFA_ENFORCED", "true").lower() == "true",
        "email_otp_login": await _otp_enabled(db),
    }

    # ── Host-agent telemetry aggregates ─────────────────────────────────
    low_disk = 0
    latencies = []
    fleet = []
    async for a in db.vps_agents.find(
            {"revoked": {"$ne": True}, "last_metrics": {"$ne": None}},
            {"last_metrics": 1, "agent_id": 1,
             "last_heartbeat": 1}).limit(200):
        m = a.get("last_metrics") or {}
        pct = m.get("disk_free_pct")
        if isinstance(pct, (int, float)) and pct < 10:
            low_disk += 1
        lat = m.get("broker_latency_ms")
        if isinstance(lat, (int, float)):
            latencies.append(lat)
        if len(fleet) < 20:
            hb = a.get("last_heartbeat")
            age = None
            if hb is not None:
                if isinstance(hb, str):
                    hb = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
                age = round((datetime.now(timezone.utc) - hb).total_seconds())
            fleet.append({
                "agent_id": a.get("agent_id"),
                "heartbeat_age_sec": age,
                "agent_version": m.get("agent_version"),
                "cpu_percent": m.get("cpu_percent"),
                "ram_percent": m.get("ram_percent"),
                "disk_free_pct": m.get("disk_free_pct"),
                "mt5_connected": m.get("mt5_connected"),
                "mt5_restarts": m.get("mt5_restarts"),
                "pending_reboot": m.get("pending_reboot"),
                "broker_latency_ms": m.get("broker_latency_ms"),
            })
    out["host_agents"] = {
        "low_disk_count": low_disk,
        "avg_broker_latency_ms": (round(sum(latencies) / len(latencies), 1)
                                  if latencies else None),
        "reporting_latency": len(latencies),
        "fleet": fleet,
    }
    try:
        from deployment_health import score_fleet
        out["deployment_health"] = await score_fleet(db)
    except Exception:  # noqa: BLE001
        out["deployment_health"] = None
    return out
