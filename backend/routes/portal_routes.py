"""Public portal routes (iter-135): legal documents, live status page,
onboarding wizard state.

GET /api/legal/{privacy|risk}  — public legal documents (markdown)
GET /api/status                — public component health (30s cache)
GET /api/onboarding            — wizard state for the current user
PUT /api/onboarding            — persist step / status / risk choice
"""
import os
import time
import logging
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from legal_content import get_legal

logger = logging.getLogger("portal")
router = APIRouter(tags=["portal"])


@router.get("/legal/{kind}")
async def legal(kind: str):
    doc = get_legal(kind)
    if not doc:
        raise HTTPException(status_code=404, detail="unknown document")
    return doc


@router.get("/release-key")
async def release_key():
    """Public Ed25519 verification key for signed release manifests —
    verifiers should pin this out-of-band."""
    import release_signing
    return {"alg": "Ed25519", "key_id": release_signing.KEY_ID,
            "public_key_b64": release_signing.public_key_b64(),
            "signer": release_signing.signer_status()}


# ---------------------------------------------------------------- status

_STATUS_CACHE = {"at": 0.0, "data": None}
_STATUS_TTL = 30.0
_STATUS_LOCK = None  # created lazily on the running loop


def _parse_iso(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@router.get("/status")
async def public_status():
    now = time.monotonic()
    if _STATUS_CACHE["data"] and now - _STATUS_CACHE["at"] < _STATUS_TTL:
        return _STATUS_CACHE["data"]

    # single-flight: a cold-cache burst computes the status once, not N times
    global _STATUS_LOCK
    import asyncio
    if _STATUS_LOCK is None:
        _STATUS_LOCK = asyncio.Lock()
    async with _STATUS_LOCK:
        now = time.monotonic()
        if _STATUS_CACHE["data"] and now - _STATUS_CACHE["at"] < _STATUS_TTL:
            return _STATUS_CACHE["data"]
        return await _compute_status(now)


async def _compute_status(now: float):
    db = get_db()
    utc_now = datetime.now(timezone.utc)
    components = {}

    # Database
    try:
        await db.command("ping")
        components["database"] = {"status": "operational"}
    except Exception:  # noqa: BLE001
        components["database"] = {"status": "down"}

    # API is trivially up if this handler runs
    components["api"] = {"status": "operational"}

    # Bot engine — any active config pulsed within 5 min
    try:
        cutoff = (utc_now - timedelta(minutes=5)).isoformat()
        recent = await db.bot_configs.count_documents(
            {"active": True, "_last_pulse.ts": {"$gte": cutoff}})
        active = await db.bot_configs.count_documents({"active": True})
        if active == 0:
            components["bot_engine"] = {"status": "idle",
                                        "note": "no active bots"}
        elif recent > 0:
            components["bot_engine"] = {"status": "operational"}
        else:
            components["bot_engine"] = {"status": "degraded",
                                        "note": "no recent bot cycles"}
    except Exception:  # noqa: BLE001
        components["bot_engine"] = {"status": "unknown"}

    # EA bridge — any account heartbeat within 10 min
    try:
        hb_cutoff = (utc_now - timedelta(minutes=10)).isoformat()
        hb = await db.accounts.count_documents(
            {"last_heartbeat": {"$gte": hb_cutoff}})
        components["ea_bridge"] = (
            {"status": "operational"} if hb > 0
            else {"status": "idle",
                  "note": "no EA heartbeats in the last 10 minutes"})
    except Exception:  # noqa: BLE001
        components["ea_bridge"] = {"status": "unknown"}

    # Payments / Email — configuration presence only (no external calls
    # from a public unauthenticated endpoint)
    components["payments"] = {
        "status": "operational" if os.environ.get("STRIPE_API_KEY") else "down"}
    components["email"] = {
        "status": "operational" if os.environ.get("RESEND_API_KEY") else "down"}

    hard = [c for c in components.values() if c["status"] == "down"]
    soft = [c for c in components.values() if c["status"] == "degraded"]
    overall = ("major_outage" if hard
               else "degraded" if soft else "operational")
    # round 13 P2-03 — infrastructure availability and TRADING readiness are separate,
    # explicitly labelled objects; account identifiers are never exposed here.
    try:
        fresh = await db.accounts.count_documents({"last_heartbeat": {"$gte": (utc_now - timedelta(minutes=2)).isoformat()}})
        enabled = await db.accounts.count_documents({"trading_enabled": True})
    except Exception:  # noqa: BLE001
        fresh, enabled = 0, 0
    connectivity = ("active" if fresh > 0 else "idle" if components["ea_bridge"]["status"] == "idle" else "not_verified")
    try:
        from canonical_decision import decide_platform
        dec = await decide_platform(db)
        readiness = {"state": dec["state"], "new_exposure_allowed": dec["new_exposure_allowed"],
                     "dominant_code": dec["dominant_code"], "decision_id": dec["decision_id"]}
    except Exception:  # noqa: BLE001
        readiness = {"state": "UNKNOWN", "new_exposure_allowed": False, "dominant_code": "UNAVAILABLE", "decision_id": None}
    if connectivity != "active" and readiness["state"] == "READY":
        readiness = {**readiness, "state": "DEGRADED", "new_exposure_allowed": False, "dominant_code": "TERMINAL_STALE"}
    # audit r15 P1-02 — the public portal reports PLATFORM AVAILABILITY only.
    # A "trading ready" claim is made solely from the signed, approved EXACT
    # inventory (6/3/3: declared expectation, approved hash, zero violations
    # or orphans) with every intended enabled account READY under ONE canonical
    # input version. No raw operational counts are exposed.
    aggregate = {"attested": False, "basis": "platform_availability_only", "input_version": None,
                 "inventory_hash": None, "as_of": None}
    try:
        from canonical_decision import authority_version, decide_account
        from inventory_projection import projection
        v_before = await authority_version(db)
        proj = await projection(db)
        exp_declared = all(proj["counts"].get(k) is not None
                           for k in ("expected_accounts", "expected_enabled", "expected_bots"))
        exact = (exp_declared and not proj["violations"] and not proj["structural_defects"]
                 and proj["approved_hash"] is not None and proj["approved_hash"] == proj["inventory_hash"])
        all_ready = False
        if exact:
            enabled_ids = [r["account_id"] for r in proj["accounts"] if r.get("enabled")]
            states = []
            for aid in enabled_ids:
                acc = await db.accounts.find_one({"_id": ObjectId(aid)})
                states.append((await decide_account(db, acc)).get("state") if acc else "MISSING")
            all_ready = bool(states) and all(st == "READY" for st in states)
        v_after = await authority_version(db)
        aggregate.update(attested=bool(exact and all_ready and v_before == v_after),
                         basis="signed_exact_inventory" if exact else "platform_availability_only",
                         input_version=v_after if v_before == v_after else None,
                         inventory_hash=(proj["approved_hash"] or "")[:12] or None, as_of=proj["as_of"])
    except Exception:  # noqa: BLE001 — fail closed: no attestation
        aggregate["attested"] = False
    if readiness["state"] == "READY" and not aggregate["attested"]:
        readiness = {**readiness, "state": "DEGRADED", "new_exposure_allowed": False,
                     "dominant_code": "INVENTORY_NOT_ATTESTED"}
    trading_ready = readiness["state"] == "READY" and connectivity == "active" and aggregate["attested"]
    # r16 P1-06 — the public banner must never overstate readiness: an idle /
    # unverified EA bridge or non-ready trading downgrades the OVERALL state
    if overall == "operational" and (components["ea_bridge"]["status"] != "operational" or not trading_ready):
        overall = "degraded"
    # public payload: NO raw operational counts (r15 P1-02)
    trading = {"connectivity": connectivity,
               "readiness": {**{k: readiness.get(k) for k in ("state", "dominant_code", "decision_id")},
                             "new_exposure_allowed": bool(readiness.get("new_exposure_allowed")) and trading_ready},
               "attestation": aggregate,
               "label": ("Trading ready" if trading_ready
                         else f"Trading {readiness['state'].lower().replace('_', '-')} · connectivity {connectivity.replace('_', ' ')}")}
    headline = (("Platform controls available" if not hard and not soft else f"Platform {overall.replace('_', ' ')}")
                + (" · trading ready" if trading_ready else f" · {trading['label'].lower()}"))
    data = {"overall": overall, "components": components, "trading": trading, "headline": headline,
            # round 10 P2-01 — deployment metadata, never hard-coded copy
            "deployment": {"region": os.environ.get("DEPLOYMENT_REGION") or None,
                           "app_env": os.environ.get("APP_ENV") or "development"},
            "checked_at": utc_now.isoformat()}
    _STATUS_CACHE.update(at=now, data=data)
    return data


# ---------------------------------------------------------------- onboarding

VALID_STATUS = {"pending", "done", "skipped"}
VALID_RISK = {"low", "medium", "high"}


@router.get("/onboarding")
async def onboarding_state(user=Depends(get_current_user)):
    db = get_db()
    udoc = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"onboarding": 1}) or {}
    ob = udoc.get("onboarding")
    if not ob:
        # Existing users who already trade (and admins) shouldn't get the wizard.
        has_account = await db.accounts.count_documents(
            {"user_id": user["id"]}, limit=1)
        status = "done" if (has_account or user.get("role") == "admin") else "pending"
        ob = {"status": status, "step": 0}
        await db.users.update_one({"_id": ObjectId(user["id"])},
                                  {"$set": {"onboarding": ob}})
    return ob


@router.put("/onboarding")
async def onboarding_update(payload: dict, user=Depends(get_current_user)):
    db = get_db()
    update = {}
    status = payload.get("status")
    if status is not None:
        if status not in VALID_STATUS:
            raise HTTPException(status_code=400, detail="invalid status")
        update["onboarding.status"] = status
        if status in ("done", "skipped"):
            update["onboarding.completed_at"] = datetime.now(
                timezone.utc).isoformat()
    step = payload.get("step")
    if step is not None:
        if not isinstance(step, int) or not 0 <= step <= 10:
            raise HTTPException(status_code=400, detail="invalid step")
        update["onboarding.step"] = step
    risk = payload.get("risk_level")
    if risk is not None:
        if risk not in VALID_RISK:
            raise HTTPException(status_code=400, detail="invalid risk_level")
        update["onboarding.risk_level"] = risk
        # Apply to the user's default bot config (created at registration).
        await db.bot_configs.update_one(
            {"user_id": user["id"], "account_id": None},
            {"$set": {"risk_level": risk}})
    if not update:
        raise HTTPException(status_code=400, detail="nothing to update")
    await db.users.update_one({"_id": ObjectId(user["id"])},
                              {"$set": update})
    udoc = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"onboarding": 1})
    return udoc.get("onboarding") or {}
