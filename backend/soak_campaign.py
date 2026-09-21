"""Production-proof Soak Campaign (iter-212, hardened iter-213):

  · GREEN is earned by INVARIANTS, not vibes: execution (no duplicate
    executions, no unconfirmed ghosts), latency UNKNOWN rate, version
    freeze, plus global degraded state.
  · Evidence is SCOPED to the campaign account; global platform health
    is reported separately and only platform-critical failures gate.
  · Checkpoint coverage requirement is 100% — one checkpoint per day.
  · Every checkpoint appends an IMMUTABLE hash-chained Production
    Evidence record carrying the release fingerprint.
  · Material versions (EA, backend release) are FROZEN at campaign
    start; any drift marks the day RED."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

DEFAULT_DAYS = 14
MIN_CHECKPOINT_COVERAGE = 1.0   # iter-213 P1: every campaign day, no gaps
MAX_MAJOR_INCIDENTS = 2
# Release review P2-1 — two explicit thresholds, never conflated:
#   RESEARCH  (paper/demo soak, exploratory): up to 20% untraced opens tolerated
#   LIVE PROMOTION: ZERO UNKNOWN broker executions and ZERO untraced opens.
UNKNOWN_RATE_MAX_RESEARCH = 0.2
UNKNOWN_RATE_MAX_LIVE = 0.0
UNKNOWN_RATE_MAX = UNKNOWN_RATE_MAX_RESEARCH   # legacy alias (research lane)


def unknown_rate_threshold(scope_env: str | None) -> float:
    """LIVE-classified campaign scope → 0.0; anything else → research."""
    return (UNKNOWN_RATE_MAX_LIVE if (scope_env or "").upper() == "LIVE"
            else UNKNOWN_RATE_MAX_RESEARCH)

SEVERITIES = {
    "critical": "Money-impacting or trust-destroying: wrong/duplicate "
                "execution, unreconciled position, data loss, security "
                "breach. ONE critical fails the campaign immediately.",
    "major": "Capability degraded: missed trading window, subsystem "
             "failing > 1h, repeated broker rejects. More than "
             f"{MAX_MAJOR_INCIDENTS} majors fail the campaign.",
    "minor": "Transient or cosmetic: brief blip with automatic recovery, "
             "UI defect, noisy log. Recorded, never gating.",
}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


# ───────────────────── material versions / release hash ──────────────────

_release_fp_cache: str | None = None


def release_fingerprint() -> str:
    """Deterministic release hash: GIT_SHA when the deploy provides it,
    else a content hash over the backend source tree."""
    global _release_fp_cache
    sha = os.environ.get("GIT_SHA")
    if sha:
        return sha[:40]
    if _release_fp_cache:
        return _release_fp_cache
    base = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs
                         if d not in ("__pycache__", "tests", ".pytest_cache"))
        for f in sorted(files):
            if f.endswith(".py"):
                p = os.path.join(root, f)
                h.update(os.path.relpath(p, base).encode())
                with open(p, "rb") as fh:
                    h.update(hashlib.sha256(fh.read()).digest())
    _release_fp_cache = "src-" + h.hexdigest()[:40]
    return _release_fp_cache


def material_versions() -> dict:
    try:
        from routes.diagnostic_routes import LATEST_EA
    except Exception:  # noqa: BLE001
        LATEST_EA = None
    return {"ea_version": LATEST_EA, "release": release_fingerprint()}


def version_drift(frozen: dict | None, current: dict | None) -> list:
    """Pure comparison — returns the list of drifted keys."""
    frozen, current = frozen or {}, current or {}
    return sorted(k for k in set(frozen) | set(current)
                  if frozen.get(k) != current.get(k))


# ───────────────────── immutable evidence chain ───────────────────────────

def evidence_hash(record: dict, prev_hash: str) -> str:
    body = {k: v for k, v in record.items() if k not in ("hash", "_id")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def verify_chain(records: list) -> bool:
    prev = "genesis"
    for r in records:
        if r.get("prev_hash") != prev:
            return False
        if evidence_hash(r, prev) != r.get("hash"):
            return False
        prev = r["hash"]
    return True


async def _append_evidence(db, campaign_id: str, checkpoint: dict) -> dict:
    last = await db.production_evidence.find_one(
        {"campaign_id": campaign_id}, sort=[("seq", -1)])
    seq = int(last["seq"]) + 1 if last else 1
    prev = last["hash"] if last else "genesis"
    record = {"campaign_id": campaign_id, "seq": seq, "at": _now(),
              "day": checkpoint.get("day"),
              "checkpoint": {k: v for k, v in checkpoint.items()
                             if k != "_id"},
              "release": release_fingerprint(),
              "prev_hash": prev}
    record["hash"] = evidence_hash(record, prev)
    await db.production_evidence.insert_one(dict(record))
    record.pop("_id", None)
    return record


async def evidence(db, campaign_id: str | None = None) -> dict:
    q = {"campaign_id": campaign_id} if campaign_id else {}
    if not campaign_id:
        camp = await db.soak_campaigns.find_one({}, sort=[("started_at", -1)])
        if camp:
            q = {"campaign_id": camp["campaign_id"]}
    records = [r async for r in db.production_evidence.find(
        q, {"_id": 0}).sort("seq", 1).limit(2000)]
    return {"records": records, "count": len(records),
            "chain_valid": verify_chain(records),
            "note": "hash-chained append-only records — any tampering "
                    "breaks chain_valid"}


# ───────────────────────── evaluation (pure) ──────────────────────────────

def evaluate(campaign: dict, checkpoints: list, incidents: list,
             now: datetime | None = None) -> dict:
    now = now or _now_dt()
    started = datetime.fromisoformat(campaign["started_at"])
    days_target = int(campaign.get("days") or DEFAULT_DAYS)
    elapsed_days = max(1, min(days_target,
                              int((now - started).total_seconds() // 86400)
                              + 1))
    coverage = round(len({c["day"] for c in checkpoints}) / days_target, 3)
    critical = [i for i in incidents
                if str(i.get("severity")).lower() == "critical"]
    major = [i for i in incidents
             if str(i.get("severity")).lower() == "major"]
    red_checkpoints = sorted({c["day"] for c in checkpoints
                              if not c.get("green")})
    criteria = {
        "duration_complete":
            (now - started).total_seconds() >= days_target * 86400,
        "checkpoint_coverage_ok": coverage >= MIN_CHECKPOINT_COVERAGE,
        "no_critical_incidents": not critical,
        "major_incidents_within_budget": len(major) <= MAX_MAJOR_INCIDENTS,
        "no_red_checkpoints": not red_checkpoints,
    }
    if critical:
        verdict = "FAIL"
    elif all(criteria.values()):
        verdict = "PASS"
    else:
        verdict = "RUNNING" if not criteria["duration_complete"] else "FAIL"
    return {"day": elapsed_days, "days_target": days_target,
            "checkpoint_coverage": coverage,
            "coverage_required": MIN_CHECKPOINT_COVERAGE,
            "criteria": criteria,
            "critical_incidents": len(critical),
            "major_incidents": len(major),
            "red_checkpoint_days": red_checkpoints, "verdict": verdict}


# ───────────────────────── campaign lifecycle ─────────────────────────────

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
           "started_by": started_by, "status": "RUNNING",
           # iter-213 P1: material versions FROZEN for the whole campaign
           "frozen_versions": material_versions()}
    await db.soak_campaigns.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def abort(db, actor: str, reason: str | None = None) -> dict:
    running = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not running:
        return {"error": "no_running_campaign"}
    await db.soak_campaigns.update_one(
        {"campaign_id": running["campaign_id"]},
        {"$set": {"status": "ABORTED", "finished_at": _now(),
                  "aborted_by": actor, "abort_reason": reason}})
    return {"campaign_id": running["campaign_id"], "status": "ABORTED"}


async def reset(db, started_by: str, days: int = DEFAULT_DAYS,
                account_id: str | None = None,
                note: str | None = None) -> dict:
    """Manual reset — abort the running campaign (if any) and start fresh,
    frozen at the CURRENT release."""
    aborted = await abort(db, started_by, reason="manual reset")
    fresh = await start(db, started_by, days=days, account_id=account_id,
                        note=note or "manual reset")
    fresh["aborted_campaign"] = (aborted.get("campaign_id")
                                 if "error" not in aborted else None)
    return fresh


# ───────────────── soak tracker (iter-159) ────────────────────────────────

REMINDER_HOURS = 12         # hours into the campaign day before a reminder
AUTO_CHECKPOINT_HOURS = 20  # safety net: auto-record so coverage never slips


def countdown_info(campaign: dict, checkpoints: list,
                   now: datetime | None = None) -> dict:
    """Pure — countdown + today's-checkpoint state for a campaign."""
    now = now or _now_dt()
    started = datetime.fromisoformat(campaign["started_at"])
    days_target = int(campaign.get("days") or DEFAULT_DAYS)
    ends_at = started + timedelta(days=days_target)
    elapsed = max(0.0, (now - started).total_seconds())
    day = int(elapsed // 86400) + 1
    hours_into_day = (elapsed % 86400) / 3600
    done_days = {c["day"] for c in checkpoints}
    return {"ends_at": ends_at.isoformat(),
            "day": min(day, days_target), "days_target": days_target,
            "days_remaining": round(
                max(0.0, (ends_at - now).total_seconds() / 86400), 2),
            "hours_into_day": round(hours_into_day, 1),
            "today_checkpoint_done": day in done_days or day > days_target,
            "missed_days": sorted(
                d for d in range(1, min(day, days_target + 1))
                if d not in done_days and d < day)}


async def tracker_sweep(db) -> dict:
    """Background sweep: auto-start a fresh campaign when a NEW release is
    detected, remind when today's checkpoint is missing, and auto-record it
    near the end of the day so coverage never slips."""
    out = {"auto_started": False, "reminded": False,
           "auto_checkpoint": False}
    running = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not running:
        latest = await db.soak_campaigns.find_one(
            {}, sort=[("started_at", -1)])
        current = material_versions().get("release")
        if latest and ((latest.get("frozen_versions") or {}).get("release")
                       != current):
            res = await start(
                db, "auto", days=DEFAULT_DAYS,
                account_id=latest.get("account_id"),
                note=f"auto-started — new release detected "
                     f"({str(current)[:12]})")
            out["auto_started"] = "error" not in res
            if out["auto_started"]:
                from alerting import raise_alert
                await raise_alert(
                    db, "soak_auto_started", "info",
                    f"New release {str(current)[:12]} detected — a fresh "
                    f"{DEFAULT_DAYS}-day soak campaign was auto-started "
                    f"({res['campaign_id']}).",
                    dedup_key=f"soak_auto_{res['campaign_id']}")
        return out
    cps = [c async for c in db.soak_checkpoints.find(
        {"campaign_id": running["campaign_id"]}, {"day": 1})]
    info = countdown_info(running, cps)
    if info["today_checkpoint_done"]:
        return out
    if info["hours_into_day"] >= AUTO_CHECKPOINT_HOURS:
        cp = await record_checkpoint(db, recorded_by="auto")
        out["auto_checkpoint"] = "error" not in cp
    elif info["hours_into_day"] >= REMINDER_HOURS:
        from alerting import raise_alert
        await raise_alert(
            db, "soak_checkpoint_due", "warning",
            f"Soak day {info['day']}/{info['days_target']}: today's "
            "checkpoint has not been recorded — record it in the Command "
            "Center before the day ends.",
            dedup_key=f"soak_cp_due_{running['campaign_id']}_{info['day']}")
        out["reminded"] = True
    return out


async def _account_invariants(db, account_id: str | None,
                              since: str) -> dict:
    """Execution / reconciliation / duplicate / UNKNOWN invariants,
    SCOPED to the campaign account when one is set."""
    scope = {"account_id": account_id} if account_id else {}
    opened = await db.trades.count_documents(
        {**scope, "opened_at": {"$gte": since}})
    traced = await db.trades.count_documents(
        {**scope, "opened_at": {"$gte": since},
         "latency_trace.t9_ms": {"$exists": True}})
    unknown_rate = round(1 - traced / opened, 3) if opened else None
    dup = 0
    for field in ("signal_id", "mt5_ticket"):
        pipeline = [
            {"$match": {**scope, "opened_at": {"$gte": since},
                        field: {"$nin": [None, ""]}}},
            {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
            {"$limit": 10},
        ]
        dup += len([d async for d in db.trades.aggregate(pipeline)])
    hour_ago = (_now_dt() - timedelta(hours=1)).isoformat()
    ghosts = await db.trades.count_documents(
        {**scope, "status": "open", "opened_at": {"$lt": hour_ago},
         "$or": [{"mt5_ticket": None}, {"mt5_ticket": {"$exists": False}}]})
    rejects = await db.trades.count_documents(
        {**scope, "status": {"$in": ["rejected", "failed"]},
         "opened_at": {"$gte": since}})
    unknown_env = None
    acc_id = account_id
    if acc_id:
        try:
            from broker_env import broker_environment
            from bson import ObjectId as _OID
            _acc = await db.accounts.find_one({"_id": _OID(acc_id)})
            unknown_env = broker_environment(_acc) if _acc else None
        except Exception:  # noqa: BLE001 — unknown env → research lane
            unknown_env = None
    unknown_max = unknown_rate_threshold(unknown_env)
    unknown_executions = await db.execution_intents.count_documents(
        {**({"account_id": acc_id} if acc_id else {}), "status": "unknown"})
    return {"trades_24h": opened, "unknown_rate": unknown_rate,
            "unknown_rate_max": unknown_max,
            "unknown_rate_lane": "live_promotion" if unknown_max == 0 else "research",
            "unknown_executions": unknown_executions,
            "unknown_rate_ok": (unknown_rate is None or unknown_rate <= unknown_max)
            and (unknown_max > 0 or unknown_executions == 0),
            "duplicate_executions": dup, "duplicates_ok": dup == 0,
            "unconfirmed_ghosts": ghosts, "reconciliation_ok": ghosts == 0,
            "rejects_24h": rejects}


async def record_checkpoint(db, recorded_by: str = "manual") -> dict:
    campaign = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not campaign:
        return {"error": "no_running_campaign"}
    started = datetime.fromisoformat(campaign["started_at"])
    day = int((_now_dt() - started).total_seconds() // 86400) + 1
    acc_id = campaign.get("account_id")
    hb_cutoff = (_now_dt() - timedelta(minutes=5)).isoformat()
    if acc_id:
        from route_utils import parse_object_id
        acc = await db.accounts.find_one({"_id": parse_object_id(acc_id)})
        account_connected = bool(
            acc and acc.get("last_heartbeat")
            and str(acc["last_heartbeat"]) >= hb_cutoff)
    else:
        acc = None
        account_connected = bool(await db.accounts.count_documents(
            {"last_heartbeat": {"$gte": hb_cutoff}}, limit=1))
    day_ago = (_now_dt() - timedelta(days=1)).isoformat()
    invariants = await _account_invariants(db, acc_id, day_ago)
    # global platform health — reported SEPARATELY; only platform-critical
    # subsystem failures gate the day
    from degraded_intelligence import status as degraded_status
    deg = await degraded_status(db)
    global_health = {"degraded_mode": deg["mode"],
                     "failing_subsystems": deg.get("failing", []),
                     "critical_failing": deg.get("critical_failing", [])}
    incidents = await db.soak_incidents.count_documents(
        {"campaign_id": campaign["campaign_id"], "severity": "critical"})
    drift = version_drift(campaign.get("frozen_versions"),
                          material_versions())
    green = (incidents == 0
             and not global_health["critical_failing"]
             and account_connected
             and invariants["unknown_rate_ok"]
             and invariants["duplicates_ok"]
             and invariants["reconciliation_ok"]
             and not drift)
    cp = {"campaign_id": campaign["campaign_id"], "day": day, "at": _now(),
          "scope": {"account_id": acc_id} if acc_id else {"account_id": None,
                                                          "note": "no "
                                                          "campaign account "
                                                          "set — platform-"
                                                          "wide scope"},
          "account_connected": account_connected,
          "invariants": invariants,
          "global_health": global_health,
          "version_drift": drift,
          "recorded_by": recorded_by,
          "green": green}
    await db.soak_checkpoints.update_one(
        {"campaign_id": cp["campaign_id"], "day": day},
        {"$set": cp}, upsert=True)
    cp.pop("_id", None)
    cp["evidence"] = await _append_evidence(db, campaign["campaign_id"], cp)
    return cp


async def log_incident(db, severity: str, note: str,
                       logged_by: str) -> dict:
    campaign = await db.soak_campaigns.find_one({"status": "RUNNING"})
    if not campaign:
        return {"error": "no_running_campaign"}
    doc = {"campaign_id": campaign["campaign_id"],
           "severity": str(severity).lower(), "note": note,
           "definition": SEVERITIES.get(str(severity).lower()),
           "logged_by": logged_by, "at": _now()}
    await db.soak_incidents.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def status(db) -> dict:
    campaign = await db.soak_campaigns.find_one(
        {}, sort=[("started_at", -1)])
    if not campaign:
        return {"status": "NO_CAMPAIGN",
                "severity_definitions": SEVERITIES,
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
    ev["version_drift"] = version_drift(campaign.get("frozen_versions"),
                                        material_versions())
    if campaign["status"] == "RUNNING" and ev["verdict"] in ("PASS", "FAIL"):
        await db.soak_campaigns.update_one(
            {"campaign_id": campaign["campaign_id"]},
            {"$set": {"status": ev["verdict"], "finished_at": _now()}})
        campaign["status"] = ev["verdict"]
    return {"campaign": campaign, "evaluation": ev,
            "countdown": countdown_info(campaign, cps),
            "severity_definitions": SEVERITIES,
            "checkpoints": cps, "incidents": incidents}


async def broker_validation(db, account: dict) -> dict:
    """Broker-attached validation checklist — evidence that a REAL broker
    account is wired end-to-end before/while the soak runs."""
    from broker_env import broker_environment
    acc_id = str(account["_id"])
    env = broker_environment(account)
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
        {"key": "live_environment",
         "label": "LIVE broker environment (not DEMO/PAPER)",
         "ok": env == "LIVE", "value": env},
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
    return {"account_id": acc_id, "broker_environment": env,
            "passed": all(c["ok"] for c in checks),
            "checks": checks, "at": _now()}
