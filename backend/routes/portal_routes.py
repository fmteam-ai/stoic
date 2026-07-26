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
            "public_key_b64": release_signing.public_key_b64()}


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
    data = {"overall": overall, "components": components,
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
