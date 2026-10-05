"""Security & Health Agent — finding records (append-only, deduplicated). SA1."""
from datetime import datetime, timedelta, timezone

from security_agent.config import SEVERITIES
from security_agent.redact import mask, mask_obj

OPEN_STATES = ("open", "contained", "acknowledged")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build(check_id: str, dedup_key: str, *, severity: str, area: str, title: str, what_happened: str,
          why_it_matters: str = "", evidence: dict | None = None, solution: list | None = None,
          verify: str = "") -> dict:
    """Pure constructor used by every check — evidence/text are masked here, once."""
    if severity not in SEVERITIES:
        raise ValueError(f"bad severity {severity}")
    return {"check_id": check_id, "dedup_key": dedup_key, "severity": severity, "area": area,
            "title": mask(title)[:200], "what_happened": mask(what_happened)[:2000],
            "why_it_matters": mask(why_it_matters)[:1000], "evidence": mask_obj(evidence or {}),
            "solution": [mask(s) for s in (solution or [])], "verify": mask(verify)[:500]}


async def ensure_indexes(db) -> None:
    await db.security_findings.create_index(
        "dedup_key", unique=True, name="uniq_open_dedup",
        partialFilterExpression={"status": {"$in": list(OPEN_STATES)}})
    await db.security_findings.create_index([("status", 1), ("severity", 1), ("last_seen", -1)])
    await db.security_findings.create_index("expires_at", expireAfterSeconds=0)
    await db.security_blocks.create_index("expires_at", expireAfterSeconds=0)
    await db.security_blocks.create_index([("kind", 1), ("value", 1), ("active", 1)])
    await db.security_events.create_index("expires_at", expireAfterSeconds=0)
    await db.security_events.create_index([("kind", 1), ("at", -1)])
    await db.security_check_runs.create_index([("check_id", 1), ("at", -1)])
    await db.security_check_runs.create_index("expires_at", expireAfterSeconds=0)
    await db.security_actions.create_index([("at", -1)])
    await db.security_actions.create_index([("dedup", 1), ("at", -1)])
    await db.security_reports.create_index([("kind", 1), ("built_at", -1)])
    # S15 — the per-minute scans (B1 sightings, T2/I1 rate-limit + event windows, S12 denied counts)
    await db.accounts.create_index([("hb_sightings.at", -1)])
    await db.security_events.create_index([("ip", 1), ("at", -1)])
    await db.security_denied_counts.create_index("expires_at", expireAfterSeconds=0)
    await db.security_denied_counts.create_index([("minute", -1), ("ip", 1)])
    await db.security_reports.create_index("expires_at", expireAfterSeconds=0)


async def open_or_update(db, finding: dict, *, retention_days: int = 180) -> dict:
    """One record per dedup_key while open: repeats bump occurrences / last_seen (and
    raise severity if the new one is worse). Returns {id, created: bool}."""
    now = _now()
    cur = await db.security_findings.find_one({"dedup_key": finding["dedup_key"], "status": {"$in": list(OPEN_STATES)}})
    if cur:
        sets = {"last_seen": now, "what_happened": finding["what_happened"], "evidence": finding["evidence"]}
        if SEVERITIES.index(finding["severity"]) > SEVERITIES.index(cur.get("severity", "low")):
            sets["severity"] = finding["severity"]
        await db.security_findings.update_one({"_id": cur["_id"]}, {"$set": sets, "$inc": {"occurrences": 1}})
        return {"id": str(cur["_id"]), "created": False}
    doc = {**finding, "status": "open", "action_taken": "none — alert only", "fixed": "no",
           "first_seen": now, "last_seen": now, "occurrences": 1, "resolved_at": None, "resolved_by": None,
           "expires_at": datetime.now(timezone.utc) + timedelta(days=retention_days)}
    res = await db.security_findings.insert_one(doc)
    return {"id": str(res.inserted_id), "created": True}


async def set_status(db, finding_id, status: str, actor: str, note: str = "") -> bool:
    if status not in ("acknowledged", "resolved", "false_positive", "contained"):
        raise ValueError("bad status")
    sets = {"status": status, "status_note": mask(note)[:500], "updated_at": _now()}
    if status in ("resolved", "false_positive"):
        sets.update({"resolved_at": _now(), "resolved_by": actor, "fixed": "yes" if status == "resolved" else "no"})
    res = await db.security_findings.update_one({"_id": finding_id, "status": {"$in": list(OPEN_STATES)}}, {"$set": sets})
    return bool(res.modified_count)


async def auto_resolve_cleared(db, check_id: str, active_keys: set) -> int:
    """A check ran clean for keys that still have open findings → the condition cleared."""
    # S10 — "contained" findings stay until their action expires or is undone (actions.expire_finished)
    q = {"check_id": check_id, "status": {"$in": ["open", "acknowledged"]},
         "dedup_key": {"$nin": list(active_keys)}}
    res = await db.security_findings.update_many(q, {"$set": {
        "status": "resolved", "resolved_at": _now(), "resolved_by": "security_agent",
        "fixed": "yes", "status_note": "condition cleared"}})
    return res.modified_count
