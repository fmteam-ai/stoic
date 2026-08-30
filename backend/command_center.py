"""Command Center (iter-152) — one-glance GREEN/YELLOW/RED aggregation of
soak progress, certifications and guard health, plus a hash-chained,
downloadable production evidence report."""
import hashlib
import os
from datetime import datetime, timedelta, timezone

RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def worst(statuses: list) -> str:
    if not statuses:
        return "YELLOW"
    return max(statuses, key=lambda s: RANK.get(s, 1))


async def _soak_section(db) -> dict:
    from soak_campaign import status as soak_status
    out = await soak_status(db)
    if out.get("status") == "NO_CAMPAIGN":
        return {"status": "YELLOW", "verdict": None, "day": None,
                "days_target": None, "coverage": None, "incidents": 0,
                "days_remaining": None, "ends_at": None,
                "today_checkpoint_done": None,
                "detail": "no soak campaign — start the 14-day "
                          "production-proof campaign"}
    ev = out.get("evaluation") or {}
    cd = out.get("countdown") or {}
    verdict = ev.get("verdict")
    drift = ev.get("version_drift") or []
    reds = ev.get("red_checkpoint_days") or []
    majors = int(ev.get("major_incidents") or 0)
    campaign_status = (out.get("campaign") or {}).get("status")
    if verdict == "FAIL":
        color, detail = "RED", "soak campaign FAILED"
    elif verdict == "PASS":
        color, detail = "GREEN", "soak campaign PASSED"
    elif drift or reds or majors:
        color = "YELLOW"
        detail = "; ".join(filter(None, [
            f"version drift: {', '.join(drift)}" if drift else "",
            f"red checkpoint days: {reds}" if reds else "",
            f"{majors} major incident(s)" if majors else ""]))
    elif (campaign_status == "RUNNING"
          and cd.get("today_checkpoint_done") is False
          and (cd.get("hours_into_day") or 0) >= 12):
        color = "YELLOW"
        detail = "today's checkpoint is due — record it before the day ends"
    else:
        color, detail = "GREEN", "on track"
    return {"status": color, "verdict": verdict,
            "day": ev.get("day"), "days_target": ev.get("days_target"),
            "coverage": ev.get("checkpoint_coverage"),
            "incidents": (int(ev.get("critical_incidents") or 0)
                          + majors),
            "campaign_status": campaign_status,
            "days_remaining": cd.get("days_remaining"),
            "ends_at": cd.get("ends_at"),
            "today_checkpoint_done": cd.get("today_checkpoint_done"),
            "detail": detail}


async def _cert_section(db) -> dict:
    from certification import active
    certs = await active(db, "", admin=True)
    valid = [c for c in certs if c.get("valid")]
    soon = (_now_dt() + timedelta(hours=48)).isoformat()
    expiring = [c for c in valid if str(c.get("expires_at") or "") <= soon]
    if not certs:
        color, detail = "YELLOW", "no certifications issued yet"
    elif not valid:
        color, detail = "RED", "no VALID certification (expired/revoked/failed)"
    elif len(expiring) == len(valid):
        color = "YELLOW"
        detail = "every valid certification expires within 48h"
    else:
        color, detail = "GREEN", f"{len(valid)} valid certification(s)"
    return {"status": color, "valid": len(valid), "total": len(certs),
            "expiring_48h": len(expiring), "detail": detail,
            "latest": [{k: c.get(k) for k in
                        ("cert_id", "kind", "subject", "tier", "passed",
                         "issued_at", "expires_at", "valid", "revoked")}
                       for c in certs[:5]]}


async def _guard_section(db) -> dict:
    now = _now_dt()
    since24 = (now - timedelta(hours=24)).isoformat()
    since1h = (now - timedelta(hours=1)).isoformat()
    total = await db.pamm_risk_decisions.count_documents(
        {"at": {"$gte": since24}})
    blocked = await db.pamm_risk_decisions.count_documents(
        {"at": {"$gte": since24}, "authorized": False})
    blocked_1h = await db.pamm_risk_decisions.count_documents(
        {"at": {"$gte": since1h}, "authorized": False})
    unknown_1h = await db.pamm_risk_decisions.count_documents(
        {"at": {"$gte": since1h}, "authorized": False,
         "reason": {"$regex": "risk_unknown|telemetry", "$options": "i"}})
    reasons = []
    async for r in db.pamm_risk_decisions.aggregate([
            {"$match": {"at": {"$gte": since24}, "authorized": False}},
            {"$group": {"_id": "$reason", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 5}]):
        reasons.append({"reason": r["_id"], "count": r["n"]})
    stale = await db.ops_alerts.count_documents(
        {"kind": "ea_heartbeat_stale", "acked_at": None})
    if stale or unknown_1h:
        color = "RED"
        detail = "; ".join(filter(None, [
            f"{stale} account(s) with STALE telemetry" if stale else "",
            f"{unknown_1h} RISK_UNKNOWN block(s) in the last hour"
            if unknown_1h else ""]))
    elif blocked_1h:
        color = "YELLOW"
        detail = f"{blocked_1h} block(s) in the last hour — guard is active"
    else:
        color, detail = "GREEN", "no blocks in the last hour, telemetry fresh"
    return {"status": color, "decisions_24h": total, "blocked_24h": blocked,
            "blocked_1h": blocked_1h, "risk_unknown_1h": unknown_1h,
            "stale_telemetry_alerts": stale, "top_block_reasons": reasons,
            "detail": detail}


async def _canary_section(db) -> dict:
    """iter-159 — release canary: one demo account soaks new releases
    ahead of the fleet; auto-halt on guard-block-rate divergence."""
    from release_canary import status as canary_status
    st = await canary_status(db)
    if not st.get("enabled"):
        return {"status": "GREEN", "enabled": False, "halted": False,
                "detail": "release canary OFF — designate a demo account "
                          "to soak new releases ahead of the fleet"}
    rates = st.get("rates") or {}
    canary = rates.get("canary") or {}
    fleet = rates.get("fleet") or {}
    verdict = st.get("verdict") or {}
    base = {"enabled": True, "halted": bool(st.get("halted")),
            "account_id": st.get("account_id"),
            "account_name": st.get("account_name"),
            "release": st.get("release"),
            "canary_rate": canary.get("block_rate"),
            "fleet_rate": fleet.get("block_rate"),
            "canary_decisions": canary.get("decisions"),
            "fleet_decisions": fleet.get("decisions"),
            "window_hours": rates.get("window_hours")}
    if st.get("halted"):
        return {**base, "status": "RED",
                "detail": f"CANARY HALTED — {st.get('halt_reason')}"}
    return {**base, "status": "GREEN",
            "detail": verdict.get("reason") or "tracking"}


async def _workers_section(db) -> dict:
    now = _now_dt()
    total = alive = crashloops = 0
    async for w in db.worker_leases.find({}):
        total += 1
        exp = w.get("expires_at")
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except ValueError:
                exp = None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp and exp > now:
            alive += 1
        for st in (w.get("loops") or {}).values():
            if (st or {}).get("consecutive_failures", 0) >= 3:
                crashloops += 1
    open_critical = await db.ops_alerts.count_documents(
        {"acked_at": None, "severity": "critical"})
    if (total and alive < total) or crashloops:
        color = "RED"
        detail = f"{total - alive} dead worker(s), {crashloops} crashloop(s)"
    elif open_critical:
        color = "YELLOW"
        detail = f"{open_critical} unacked critical alert(s)"
    else:
        color, detail = "GREEN", f"{alive}/{total} workers alive"
    return {"status": color, "workers_alive": alive, "workers_total": total,
            "crashloops": crashloops, "open_critical_alerts": open_critical,
            "detail": detail}


def _provenance() -> dict:
    from modules.pamm.strategy_guard import GIT_COMMIT, GUARD_VERSION
    from soak_campaign import material_versions
    return {"git_commit": GIT_COMMIT,
            "guard_policy_version": GUARD_VERSION,
            "image_digest": os.environ.get("STOIC_IMAGE_DIGEST"),
            "material_versions": material_versions()}


async def status(db) -> dict:
    sections = {"soak": await _soak_section(db),
                "certifications": await _cert_section(db),
                "guard": await _guard_section(db),
                "canary": await _canary_section(db),
                "workers": await _workers_section(db)}
    alerts = [a async for a in db.ops_alerts.find(
        {}, {"_id": 0, "kind": 1, "severity": 1, "message": 1,
             "created_at": 1, "acked_at": 1, "occurrences": 1})
        .sort("created_at", -1).limit(10)]
    emails = [e async for e in db.guard_alert_emails.find(
        {}, {"_id": 1, "subject": 1, "last_sent_at": 1, "sends": 1,
             "last_delivered": 1, "last_error": 1})
        .sort("last_sent_at", -1).limit(10)]
    from email_sender import is_configured
    return {"overall": worst([s["status"] for s in sections.values()]),
            "at": _now_dt().isoformat(),
            "sections": sections,
            "recent_alerts": alerts,
            "recent_alert_emails": emails,
            "email_alerts_configured": is_configured(),
            "provenance": _provenance()}


# ───────────────── hash-chained evidence report export ────────────────────

async def evidence_report(db, generated_by: str) -> dict:
    """Auditor-grade report: every section is chained with
    hash = sha256(prev_hash + canonical(section)) — the same rule as the
    soak Production Evidence chain (soak_campaign.evidence_hash)."""
    from certification import active
    from release_canary import status as canary_status
    from soak_campaign import evidence, evidence_hash
    from soak_campaign import status as soak_status

    sections: list = []
    prev = "genesis"

    def add(name: str, data) -> None:
        nonlocal prev
        rec = {"section": name, "seq": len(sections) + 1,
               "prev_hash": prev, "data": data}
        rec["hash"] = evidence_hash(rec, prev)
        prev = rec["hash"]
        sections.append(rec)

    add("provenance", _provenance())
    certs = await active(db, "", admin=True)
    add("certifications",
        {"count": len(certs), "valid": len([c for c in certs
                                            if c.get("valid")]),
         "certifications": certs})
    add("soak_status", await soak_status(db))
    add("soak_evidence_chain", await evidence(db))
    guard = await _guard_section(db)
    blocks = [b async for b in db.pamm_risk_decisions.find(
        {"authorized": False},
        {"_id": 0, "snapshot_id": 1, "at": 1, "reason": 1, "mode": 1,
         "program_id": 1, "hash": 1, "signal": 1})
        .sort("at", -1).limit(25)]
    add("guard_health", {**guard, "recent_block_snapshots": blocks})
    add("release_canary", await canary_status(db))

    report_hash = hashlib.sha256(
        "".join(s["hash"] for s in sections).encode()).hexdigest()
    report = {"report": "stoic-production-evidence", "version": 1,
            "generated_at": _now_dt().isoformat(),
            "generated_by": generated_by,
            "verify": "each section: hash = sha256(prev_hash + canonical "
                      "JSON of the record without hash/_id); report_hash = "
                      "sha256(concat of section hashes in order). Any edit "
                      "breaks the chain.",
            "sections": sections,
            "report_hash": report_hash}
    report["chain_verified"] = verify_report(report)
    return report


def verify_report(report: dict) -> bool:
    """Pure re-verification of an exported report (used by tests/auditors)."""
    from soak_campaign import evidence_hash
    prev = "genesis"
    for s in report.get("sections") or []:
        if s.get("prev_hash") != prev:
            return False
        if evidence_hash(s, prev) != s.get("hash"):
            return False
        prev = s["hash"]
    want = hashlib.sha256("".join(
        s["hash"] for s in report.get("sections") or []).encode()).hexdigest()
    return want == report.get("report_hash")
