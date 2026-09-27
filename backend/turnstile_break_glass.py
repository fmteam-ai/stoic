"""Governed Turnstile break-glass (audit round 9 P1-05).

Durable state in db.platform_state {_id:"turnstile_break_glass"}; activation,
expiry, deactivation and post-incident review are appended to the hash-chained
admin_audit_log; every bypassed request is a durable db.turnstile_bypass_events
row (no submitted token is ever stored). Promotion is blocked while a record is
active OR unreviewed (see ops release-readiness gate `turnstile_break_glass`).
"""
import hashlib
import os
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

DOC_ID = "turnstile_break_glass"
MAX_TTL_MINUTES = 60
DEFAULT_TTL_MINUTES = 30
MIN_REASON_CHARS = 20
DEFAULT_SCOPE = ["login"]
ALLOWED_SCOPE = {"login", "register", "password_reset"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def validate_request(payload: dict, actor_email: str) -> dict:
    """Refuse activation unless incident ID, actor, approver, reason, scope and
    expiry are all present and sane. Registration/reset are excluded unless
    explicitly requested with allow_registration_reset=true."""
    errors = []
    incident_id = str(payload.get("incident_id") or "").strip()
    approver = str(payload.get("approver") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    scope = payload.get("scope", DEFAULT_SCOPE)
    ttl = payload.get("ttl_minutes", DEFAULT_TTL_MINUTES)
    if len(incident_id) < 3:
        errors.append("incident_id required")
    if not actor_email:
        errors.append("actor required")
    if "@" not in approver:
        errors.append("approver (email) required")
    if approver and approver == actor_email.lower():
        errors.append("approver must differ from actor")
    if len(reason) < MIN_REASON_CHARS:
        errors.append(f"reason must be at least {MIN_REASON_CHARS} characters")
    if not isinstance(scope, list) or not scope or not set(scope) <= ALLOWED_SCOPE:
        errors.append(f"scope must be a non-empty subset of {sorted(ALLOWED_SCOPE)}")
    elif set(scope) - {"login"} and payload.get("allow_registration_reset") is not True:
        errors.append("register/password_reset bypass requires allow_registration_reset=true")
    try:
        ttl = int(ttl)
        if ttl < 1 or ttl > MAX_TTL_MINUTES:
            errors.append(f"ttl_minutes must be 1..{MAX_TTL_MINUTES}")
    except (TypeError, ValueError):
        errors.append("ttl_minutes must be an integer")
        ttl = 0
    if errors:
        raise HTTPException(status_code=400, detail={"code": "break_glass_refused", "errors": errors})
    return {"incident_id": incident_id[:64], "approver": approver, "reason": reason[:500],
            "scope": sorted(set(scope)), "ttl_minutes": ttl}


PENDING_ID = "turnstile_break_glass_pending"


async def request_activation(db, payload: dict, actor_email: str) -> dict:
    """Step 1 (round 10 P1-06): the requesting admin records the request; nothing
    is bypassed until the named approver confirms from their OWN authenticated,
    step-up-verified session (`approve`)."""
    from audit_chain import append_chained
    req = validate_request(payload, actor_email)
    if await active(db):
        raise HTTPException(status_code=409, detail={"code": "break_glass_already_active"})
    now = _now()
    doc = {"_id": PENDING_ID, **req, "actor": actor_email, "requested_at": _iso(now),
           "expires_at": _iso(now + timedelta(minutes=15))}
    # round 11 P2-02 — atomic create: an unexpired pending request is never overwritten
    await db.platform_state.delete_one({"_id": PENDING_ID, "expires_at": {"$lte": _iso(now)}})
    try:
        await db.platform_state.insert_one(doc)
    except Exception:  # noqa: BLE001 — DuplicateKeyError
        raise HTTPException(status_code=409, detail={"code": "break_glass_request_pending",
                                                     "message": "an unexpired request exists — cancel it first"})
    await append_chained(db, {"actor_email": actor_email, "action": "turnstile_break_glass_requested",
                              "target_kind": "platform", "target_id": DOC_ID, "reason": req["reason"], "at": _iso(now),
                              "meta": {k: req[k] for k in ("incident_id", "approver", "scope", "ttl_minutes")}})
    return {"pending": True, "request": {k: doc[k] for k in ("incident_id", "actor", "approver", "scope",
                                                             "ttl_minutes", "requested_at", "expires_at")}}


async def cancel_request(db, actor_email: str, note: str = "") -> dict:
    from audit_chain import append_chained
    pending = await db.platform_state.find_one_and_delete({"_id": PENDING_ID})
    if not pending:
        raise HTTPException(status_code=404, detail={"code": "no_pending_break_glass"})
    await append_chained(db, {"actor_email": actor_email, "action": "turnstile_break_glass_request_cancelled",
                              "target_kind": "platform", "target_id": DOC_ID, "reason": (note or "")[:500],
                              "at": _iso(_now()), "meta": {"incident_id": pending["incident_id"], "requested_by": pending["actor"]}})
    return await status(db)


async def approve_activation(db, approver_email: str) -> dict:
    """Step 2: the approver named in the request — and nobody else — activates it."""
    pending = await db.platform_state.find_one({"_id": PENDING_ID})
    if not pending:
        raise HTTPException(status_code=404, detail={"code": "no_pending_break_glass"})
    if datetime.fromisoformat(pending["expires_at"]) <= _now():
        await db.platform_state.delete_one({"_id": PENDING_ID})
        raise HTTPException(status_code=410, detail={"code": "break_glass_request_expired"})
    if pending["approver"] != approver_email.lower():
        raise HTTPException(status_code=403, detail={"code": "approver_mismatch",
                                                     "message": "only the named approver may activate this request"})
    if pending["actor"].lower() == approver_email.lower():
        raise HTTPException(status_code=403, detail={"code": "second_admin_required"})
    payload = {k: pending[k] for k in ("incident_id", "approver", "reason", "scope", "ttl_minutes")}
    payload["allow_registration_reset"] = bool(set(pending["scope"]) - {"login"})
    out = await activate(db, payload, pending["actor"], approved_by=approver_email)
    await db.platform_state.delete_one({"_id": PENDING_ID})
    return out


async def activate(db, payload: dict, actor_email: str, *, approved_by: str | None = None) -> dict:
    """Direct activation is only reachable through approve_activation (signed
    approver session) — `approved_by` is mandatory."""
    from audit_chain import append_chained
    from alerting import raise_alert
    if not approved_by:
        raise HTTPException(status_code=403, detail={"code": "approval_required",
                                                     "message": "break-glass requires a second admin's approval"})
    req = validate_request(payload, actor_email)
    if await active(db):
        raise HTTPException(status_code=409, detail={"code": "break_glass_already_active"})
    now = _now()
    until = now + timedelta(minutes=req["ttl_minutes"])
    doc = {"_id": DOC_ID, "active": True, "incident_id": req["incident_id"], "actor": actor_email,
           "approver": req["approver"], "approved_by": approved_by, "reason": req["reason"], "scope": req["scope"],
           "activated_at": _iso(now), "until": _iso(until), "deactivated_at": None,
           "deactivated_by": None, "reviewed_at": None, "reviewed_by": None, "review_note": None,
           "bypass_count": 0}
    await db.platform_state.replace_one({"_id": DOC_ID}, doc, upsert=True)
    await append_chained(db, {"actor_email": actor_email, "action": "turnstile_break_glass_activated",
                              "target_kind": "platform", "target_id": DOC_ID,
                              "reason": req["reason"], "at": _iso(now),
                              "meta": {k: doc[k] for k in ("incident_id", "approver", "approved_by", "scope", "until")}})
    await raise_alert(db, "turnstile_break_glass", "critical",
                      f"Turnstile break-glass ACTIVE ({req['incident_id']}) scope={','.join(req['scope'])} until {_iso(until)}",
                      dedup_key=f"turnstile_break_glass:{req['incident_id']}",
                      meta={"incident_id": req["incident_id"], "actor": actor_email, "approver": req["approver"]})
    return await status(db)


async def _load(db) -> dict | None:
    return await db.platform_state.find_one({"_id": DOC_ID})


async def active(db) -> dict | None:
    """Currently effective bypass (auto-expires by `until`), else None."""
    doc = await _load(db)
    if not doc or not doc.get("active"):
        return None
    try:
        until = datetime.fromisoformat(doc["until"])
    except (KeyError, ValueError):
        return None
    if until <= _now():
        await db.platform_state.update_one({"_id": DOC_ID, "active": True},
                                           {"$set": {"active": False, "expired_at": _iso(_now())}})
        return None
    return {"incident_id": doc["incident_id"], "scope": list(doc.get("scope") or DEFAULT_SCOPE),
            "until": doc["until"], "reason": doc.get("reason", "")}


async def record_bypass(db, bg: dict, *, action: str, remote_ip: str | None, request_id: str | None) -> None:
    salt = os.environ.get("LEDGER_ANCHOR_KEY") or os.environ.get("JWT_SECRET") or ""
    ip_hash = hashlib.sha256(f"{salt}:{remote_ip or ''}".encode()).hexdigest()[:24]
    await db.turnstile_bypass_events.insert_one({
        "incident_id": bg["incident_id"], "action": action, "ip_hash": ip_hash,
        "request_id": request_id, "at": _iso(_now())})
    await db.platform_state.update_one({"_id": DOC_ID}, {"$inc": {"bypass_count": 1}})


async def deactivate(db, actor_email: str, note: str = "") -> dict:
    from audit_chain import append_chained
    doc = await _load(db)
    if not doc or not doc.get("active"):
        raise HTTPException(status_code=404, detail={"code": "break_glass_not_active"})
    now = _iso(_now())
    await db.platform_state.update_one({"_id": DOC_ID}, {"$set": {"active": False, "deactivated_at": now,
                                                                  "deactivated_by": actor_email}})
    await append_chained(db, {"actor_email": actor_email, "action": "turnstile_break_glass_deactivated",
                              "target_kind": "platform", "target_id": DOC_ID, "reason": note[:500], "at": now,
                              "meta": {"incident_id": doc["incident_id"]}})
    return await status(db)


async def review(db, actor_email: str, note: str) -> dict:
    """Post-incident review closes the promotion block. Refused while active."""
    from audit_chain import append_chained
    doc = await _load(db)
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "break_glass_never_used"})
    if await active(db):
        raise HTTPException(status_code=409, detail={"code": "break_glass_still_active"})
    if len((note or "").strip()) < MIN_REASON_CHARS:
        raise HTTPException(status_code=400, detail={"code": "review_note_required",
                                                     "message": f"note must be at least {MIN_REASON_CHARS} characters"})
    now = _iso(_now())
    await db.platform_state.update_one({"_id": DOC_ID}, {"$set": {"reviewed_at": now, "reviewed_by": actor_email,
                                                                  "review_note": note.strip()[:1000]}})
    await append_chained(db, {"actor_email": actor_email, "action": "turnstile_break_glass_reviewed",
                              "target_kind": "platform", "target_id": DOC_ID, "reason": note.strip()[:500],
                              "at": now, "meta": {"incident_id": doc["incident_id"],
                                                  "bypass_count": doc.get("bypass_count", 0)}})
    return await status(db)


async def _pending(db) -> dict | None:
    p = await db.platform_state.find_one({"_id": PENDING_ID})
    if p and datetime.fromisoformat(p["expires_at"]) <= _now():
        await db.platform_state.delete_one({"_id": PENDING_ID})
        return None
    return p


async def status(db) -> dict:
    doc = await _load(db)
    pending_req = await _pending(db)          # round 11 P2-01 — loaded BEFORE any early return
    pending_view = ({k: pending_req[k] for k in ("incident_id", "actor", "approver", "scope", "expires_at")}
                    if pending_req else None)
    if not doc:
        return {"active": False, "promotion_blocked": pending_req is not None, "pending_review": False,
                "record": None, "pending_request": pending_view}
    is_active = await active(db) is not None
    pending_review = (not is_active) and not doc.get("reviewed_at")
    rec = {k: doc.get(k) for k in ("incident_id", "actor", "approver", "approved_by", "reason", "scope", "activated_at",
                                   "until", "deactivated_at", "deactivated_by", "expired_at", "reviewed_at",
                                   "reviewed_by", "review_note", "bypass_count")}
    return {"active": is_active, "promotion_blocked": is_active or pending_review or pending_req is not None,
            "pending_review": pending_review, "record": rec, "pending_request": pending_view}


async def readiness_check(db) -> dict:
    s = await status(db)
    detail = ("break-glass ACTIVE" if s["active"] else
              "break-glass used and awaiting post-incident review" if s["pending_review"] else
              "break-glass request pending approval" if s["pending_request"] else "clear")
    return {"ok": not s["promotion_blocked"], "detail": detail,
            "incident_id": ((s["pending_request"] or {}).get("incident_id") if s["pending_request"] and not s["active"]
                            else (s["record"] or {}).get("incident_id"))}
