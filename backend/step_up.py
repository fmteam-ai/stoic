"""Step-up MFA — short-lived, single-use tokens that gate live-sensitive
actions (live activation, risk raises, panic release, API-key creation).
Users must have TOTP enrolled; a fresh code is exchanged for a 5-minute
one-shot token via POST /api/auth/step-up. Also hosts the append-only
security audit trail (db.audit_log)."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException

STEP_UP_TTL_SECONDS = 300
STEP_UP_HEADER = "X-Step-Up-Token"
STEP_UP_ACTIONS = {"live_activation", "risk_raise", "panic_release", "api_key_create",
                   # iter-163 — ops release/fleet controls (audit hardening)
                   "release_promote", "release_rollback", "agent_config_push"}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_step_up_token(db, user_id: str, action: str) -> dict:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await db.step_up_tokens.insert_one({
        "user_id": user_id,
        "token_hash": _hash_token(token),
        "action": action,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=STEP_UP_TTL_SECONDS)).isoformat(),
        "used_at": None,
    })
    return {"step_up_token": token, "expires_in": STEP_UP_TTL_SECONDS,
            "action": action}


async def require_step_up(db, user, request, action: str) -> None:
    """403 unless the request carries a fresh, unused step-up token for
    `action`. Users without TOTP enrolled are blocked entirely."""
    # Test-suite bypass (mirrors RATE_LIMIT_BYPASS_TOKEN) — server-side
    # secret, refused outright when APP_ENV=production (SEC-001).
    bypass = os.environ.get("STEP_UP_BYPASS_TOKEN") or ""
    hdr = (request.headers.get("X-Step-Up-Bypass") or "")
    from app_env import is_production
    if (bypass and hdr
            and not is_production()
            and secrets.compare_digest(bypass, hdr)):
        return
    full = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"two_factor_enabled": 1})
    if not (full or {}).get("two_factor_enabled"):
        raise HTTPException(status_code=403, detail={
            "code": "mfa_enrollment_required", "action": action,
            "message": "Two-factor authentication must be enabled before this "
                       "action. Enroll under Settings → Security first."})
    token = (request.headers.get(STEP_UP_HEADER) or "").strip()
    if not token:
        raise HTTPException(status_code=403, detail={
            "code": "step_up_required", "action": action,
            "message": "This action requires a fresh 2FA code."})
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = await db.step_up_tokens.find_one_and_update(
        {"user_id": user["id"], "token_hash": _hash_token(token),
         "action": action, "used_at": None, "expires_at": {"$gt": now_iso}},
        {"$set": {"used_at": now_iso}})
    if not doc:
        raise HTTPException(status_code=403, detail={
            "code": "step_up_invalid", "action": action,
            "message": "Step-up token expired or already used — verify your "
                       "2FA code again."})


async def audit_event(db, user_id: str, action: str, detail: dict = None,
                      request=None, step_up: bool = False) -> None:
    """Append-only audit trail for security-sensitive operations."""
    doc = {"user_id": user_id, "action": action, "detail": detail or {},
           "step_up_verified": bool(step_up),
           "at": datetime.now(timezone.utc).isoformat()}
    if request is not None:
        # SEC-audit hardening: use the same unspoofable IP derivation as
        # rate limiting (rightmost trusted hop / CF-Connecting-IP).
        from security import client_ip
        doc["ip"] = client_ip(request)
    try:
        await db.audit_log.insert_one(doc)
    except Exception:
        pass
