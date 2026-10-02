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
                   # iter-163/165/166/171 — ops release/fleet controls (audit hardening)
                   "release_promote", "release_rollback", "agent_config_push",
                   "canary_set", "release_trust", "audit_anchor",
                   # round 10–12 — two-admin governance + model promotion
                   "authority_relax", "model_promotion",
                   # impr-auth — dedicated passkey enrolment proof (was
                   # reusing api_key_create); short TTL: it gates minting a
                   # credential that can itself mint every other step-up.
                   "passkey_enroll",
                   # impr-auth — EA bridge-token rotation (account_routes)
                   "bridge_token_rotate"}
# Per-action TTL overrides (seconds); everything else uses STEP_UP_TTL_SECONDS.
STEP_UP_ACTION_TTL = {"passkey_enroll": 120}


def step_up_ttl(action: str) -> int:
    return int(STEP_UP_ACTION_TTL.get(action, STEP_UP_TTL_SECONDS))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_step_up_token(db, user_id: str, action: str,
                              method: str = "totp") -> dict:
    """`method` records which factor minted the token ("totp" | "webauthn")
    so actions can demand a specific factor (see require_step_up)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    ttl = step_up_ttl(action)
    await db.step_up_tokens.insert_one({
        "user_id": user_id,
        "token_hash": _hash_token(token),
        "action": action,
        "method": method,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "used_at": None,
    })
    return {"step_up_token": token, "expires_in": ttl,
            "action": action}


async def require_step_up(db, user, request, action: str,
                          required_method: str | None = None) -> None:
    """403 unless the request carries a fresh, unused step-up token for
    `action`. Users without TOTP enrolled are blocked entirely.
    `required_method` (e.g. "totp") additionally pins the factor that must
    have minted the token (tokens without a recorded `method` never match)."""
    # Test-suite bypass (mirrors RATE_LIMIT_BYPASS_TOKEN) — server-side
    # secret, refused outright when APP_ENV=production (SEC-001).
    from app_env import bypass_token
    bypass = bypass_token("STEP_UP_BYPASS_TOKEN")
    hdr = (request.headers.get("X-Step-Up-Bypass") or "")
    from app_env import is_production
    if (bypass and hdr
            and not is_production()
            and secrets.compare_digest(bypass, hdr)):
        return
    full = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"two_factor_enabled": 1})
    if not (full or {}).get("two_factor_enabled"):
        # iter-177 — an enrolled passkey (WebAuthn) also satisfies MFA
        # enrollment; the step-up token itself proves whichever factor
        # was used to mint it.
        from webauthn_mfa import has_passkey
        if not await has_passkey(db, user["id"]):
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
    query = {"user_id": user["id"], "token_hash": _hash_token(token),
             "action": action, "used_at": None, "expires_at": {"$gt": now_iso}}
    if required_method:
        query["method"] = required_method
    doc = await db.step_up_tokens.find_one_and_update(
        query, {"$set": {"used_at": now_iso}})
    if not doc:
        raise HTTPException(status_code=403, detail={
            "code": "step_up_invalid", "action": action,
            **({"required_method": required_method} if required_method else {}),
            "message": ("Step-up token expired or already used — verify your "
                        "2FA code again." if required_method != "totp" else
                        "This action requires a fresh authenticator-app (TOTP) "
                        "code — a passkey cannot authorise it.")})


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
