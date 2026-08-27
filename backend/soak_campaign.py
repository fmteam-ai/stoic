"""Production-proof Soak Campaign (iter-212) — a 14-day broker-attached
validation with daily checkpoints, an incident log and explicit pass
criteria. The campaign SELF-DOCUMENTS: verdicts are computed from the
recorded evidence, never asserted."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

DEFAULT_DAYS = 14
MIN_CHECKPOINT_COVERAGE = 0.8


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def evaluate(campaign: dict, checkpoints: list, incidents: list,
             now: datetime | None = None) -> dict:
    """Pure pass/fail evaluation — unit-testable without a DB."""
    now = now or _now_dt()
    started = datetime.fromisoformat(campaign["started_at"])
    days_target = int(campaign.get("days") or DEFAULT_DAYS)
    day = min(days_target, int((now - started).total_seconds() // 86400) + 1)
    elapsed_days = max(1, min(days_target,
                              int((now - started).total_seconds() // 86400)
                              + 1))
    coverage = round(len({c["day"] for c in checkpoints})
                     / days_target, 3)
    critical = [i for i in incidents
                if str(i.get("severity")).lower() == "critical"]
    red_checkpoints = [c["day"] for c in checkpoints if not c.get("green")]
    criteria = {
        "duration_complete": day >= days_target
        and (now - started).total_seconds() >= days_target * 86400,
        "checkpoint_coverage_ok": coverage >= MIN_CHECKPOINT_COVERAGE,
        "no_critical_incidents": not critical,
        "no_red_checkpoints": not red_checkpoints,
    }
    if critical:
        verdict = "FAIL"
    elif all(criteria.values()):
        verdict = "PASS"
    else:
        verdict = "RUNNING" if not criteria["duration_complete"] else "FAIL"
    return {"day": elapsed_days, "days_target": days_target,
            "checkpoint_coverage": coverage, "criteria": criteria,
            "critical_incidents": len(critical),
            "red_checkpoint_days": red_checkpoints, "verdict": verdict}


async def start(db, started_by: str, days: int = DEFAULT_DAYS,
                account_id: str | None = None,
                note: str | None = None) -> dict:
    running = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if running:
        return {"error": "campaign_already_running",
                "campaign_id": running["campaign_id"]}
    doc = {"campaign_id": "soak_" + uuid4().hex[:10],
           "started_at": _now(), "days": max(1, min(int(days), 60)),
           "account_id": account_id, "note": note,
           "started_by": started_by, "status": "RUNNING"}
    await db.soak_campaigns.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def record_checkpoint(db) -> dict:
    campaign = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not campaign:
        return {"error": "no_running_campaign"}
    started = datetime.fromisoformat(campaign["started_at"])
    day = int((_now_dt() - started).total_seconds() // 86400) + 1
    hb_cutoff = (_now_dt() - timedelta(minutes=5)).isoformat()
    total = await db.accounts.count_documents({"status": {"$ne": "deleted"}})
    connected = await db.accounts.count_documents(
        {"last_heartbeat": {"$gte": hb_cutoff}})
    from degraded_intelligence import status as degraded_status
    deg = await degraded_status(db)
    day_ago = (_now_dt() - timedelta(days=1)).isoformat()
    rejects = await db.trades.count_documents(
        {"status": {"$in": ["rejected", "failed"]},
         "opened_at": {"$gte": day_ago}})
    incidents = await db.soak_incidents.count_documents(
        {"campaign_id": campaign["campaign_id"], "severity": "critical"})
    green = deg["mode"] == "NORMAL" and incidents == 0
    cp = {"campaign_id": campaign["campaign_id"], "day": day, "at": _now(),
          "accounts_total": total, "accounts_connected": connected,
          "degraded_mode": deg["mode"],
          "failing_subsystems": deg.get("failing", []),
          "rejects_24h": rejects, "green": green}
    await db.soak_checkpoints.update_one(
        {"campaign_id": cp["campaign_id"], "day": day},
        {"$set": cp}, upsert=True)
    return cp


async def log_incident(db, severity: str, note: str,
                       logged_by: str) -> dict:
    campaign = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not campaign:
        return {"error": "no_running_campaign"}
    doc = {"campaign_id": campaign["campaign_id"],
           "severity": str(severity).lower(), "note": note,
           "logged_by": logged_by, "at": _now()}
    await db.soak_incidents.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def status(db) -> dict:
    campaign = await db.soak_campaigns.find_one(
        {}, sort=[("started_at", -1)])
    if not campaign:
        return {"status": "NO_CAMPAIGN",
                "hint": "POST /api/ops/soak/start to begin the 14-day "
                        "production-proof campaign"}
    campaign.pop("_id", None)
    cps = [c async for c in db.soak_checkpoints.find(
        {"campaign_id": campaign["campaign_id"]}, {"_id": 0})
        .sort("day", 1).limit(100)]
    incidents = [i async for i in db.soak_incidents.find(
        {"campaign_id": campaign["campaign_id"]}, {"_id": 0})
        .sort("at", -1).limit(200)]
    ev = evaluate(campaign, cps, incidents)
    if campaign["status"] == "RUNNING" and ev["verdict"] in ("PASS", "FAIL"):
        await db.soak_campaigns.update_one(
            {"campaign_id": campaign["campaign_id"]},
            {"$set": {"status": ev["verdict"], "finished_at": _now()}})
        campaign["status"] = ev["verdict"]
    return {"campaign": campaign, "evaluation": ev,
            "checkpoints": cps, "incidents": incidents}


async def broker_validation(db, account: dict) -> dict:
    """Broker-attached validation checklist — evidence that a REAL broker
    account is wired end-to-end before/while the soak runs."""
    acc_id = str(account["_id"])
    d30 = (_now_dt() - timedelta(days=30)).isoformat()
    hb_age = None
    try:
        hb = datetime.fromisoformat(str(account.get("last_heartbeat")))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        hb_age = (_now_dt() - hb).total_seconds()
    except (TypeError, ValueError):
        pass
    deals = await db.broker_deals.count_documents(
        {"account_id": acc_id}, limit=1)
    closed = await db.trades.count_documents(
        {"account_id": acc_id, "status": "closed"}, limit=1)
    traced = await db.trades.count_documents(
        {"account_id": acc_id, "latency_trace.t9_ms": {"$exists": True},
         "opened_at": {"$gte": d30}}, limit=1)
    checks = [
        {"key": "not_paper", "label": "Real broker account (not paper)",
         "ok": account.get("mode") != "paper"},
        {"key": "identity_verified", "label": "Verified installation identity",
         "ok": bool((account.get("ea_identity") or {}).get("authoritative"))},
        {"key": "heartbeat_live", "label": "Live heartbeat < 5 min",
         "ok": hb_age is not None and hb_age < 300},
        {"key": "deal_history_synced", "label": "Broker deal history synced",
         "ok": deals > 0},
        {"key": "round_trip_trade", "label": "≥1 closed round-trip trade",
         "ok": closed > 0},
        {"key": "latency_traced", "label": "T0→T9 traces present (30d)",
         "ok": traced > 0},
        {"key": "clock_health", "label": "Agent clock telemetry OK",
         "ok": (account.get("agent_clock") or {}).get("status") == "OK"},
    ]
    return {"account_id": acc_id,
            "passed": all(c["ok"] for c in checks),
            "checks": checks, "at": _now()}
