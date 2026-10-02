"""iter-177 — WebAuthn passkey endpoints (admin-only step-up factor)."""
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from security import check_failure_limit, clear_failures, record_failure
from step_up import STEP_UP_ACTIONS, audit_event, issue_step_up_token

router = APIRouter(prefix="/auth/webauthn", tags=["webauthn"])


def _require_admin(user: dict) -> None:
    if (user or {}).get("role") != "admin":
        raise HTTPException(status_code=403,
                            detail="Passkeys are available to administrators")


def _origin(request: Request, payload: dict | None = None) -> str:
    """The WebAuthn ceremony origin — derived from SERVER configuration only.

    Review-sec fix: the client-declared ``origin`` body field is IGNORED
    (``payload`` is accepted for signature compatibility only). Resolution:
      1. WEBAUTHN_ORIGIN env (the pinned public URL) — authoritative.
      2. The request Origin header, but only when it is in the configured
         CORS allow-list (security._allowed_origins, production-filtered).
      3. Non-production with no allow-list configured: a same-origin Origin
         header (Origin host == Host header) — dev/preview convenience.
    Anything else → "" so begin_* rejects with "invalid origin"."""
    pinned = (os.environ.get("WEBAUTHN_ORIGIN") or "").strip().rstrip("/")
    if pinned:
        return pinned
    hdr = (request.headers.get("origin") or "").strip().rstrip("/")
    if not hdr:
        return ""
    from security import _allowed_origins
    allowed = _allowed_origins()
    if allowed:
        return hdr if hdr in allowed else ""
    from app_env import is_production
    if is_production():
        return ""
    host = (request.headers.get("host") or "").strip().lower()
    return hdr if host and hdr.split("://", 1)[-1].lower() == host else ""


async def _require_enrolment_proof(db, user: dict, request: Request,
                                   payload: dict | None) -> None:
    """Review-sec fix: a session alone must not be able to enrol a passkey
    (a passkey mints step-up tokens for every live-sensitive action).

    - User already has an MFA factor (TOTP or a passkey) → a fresh step-up
      token is required (existing require_step_up semantics; the frontend's
      step-up interceptor prompts + retries automatically). A token minted by
      an EXISTING passkey proves possession of an already-enrolled factor, so
      this is not circular. impr-auth — dedicated "passkey_enroll" action;
      when the user has TOTP the token MUST have been minted with TOTP (a
      passkey alone cannot enrol further passkeys for a TOTP user).
    - No MFA factor yet (first factor) → the current password must be
      supplied in the payload and is verified with auth.verify_password."""
    from bson import ObjectId
    full = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"two_factor_enabled": 1,
                                    "password_hash": 1}) or {}
    from webauthn_mfa import has_passkey
    if full.get("two_factor_enabled"):
        from step_up import require_step_up
        await require_step_up(db, user, request, "passkey_enroll",
                              required_method="totp")
        return
    if await has_passkey(db, user["id"]):
        from step_up import require_step_up
        await require_step_up(db, user, request, "passkey_enroll")
        return
    if not full.get("password_hash"):
        # Passwordless (OAuth-only) account with no factor: nothing stronger
        # than the session exists to check — require TOTP enrolment first.
        raise HTTPException(status_code=403, detail={
            "code": "mfa_enrollment_required", "action": "passkey_enroll",
            "message": "Enable authenticator-app 2FA before adding a passkey."})
    from auth import verify_password
    pw = str((payload or {}).get("current_password") or "")
    await check_failure_limit(db, "passkey_enrol", user["id"], 5, 600,
                              "Too many failed attempts. Try again later.")
    if not pw or not verify_password(pw, full["password_hash"]):
        await record_failure(db, "passkey_enrol", user["id"], 600)
        raise HTTPException(status_code=401, detail={
            "code": "password_required",
            "message": "Current password is required to add a passkey."})
    await clear_failures(db, "passkey_enrol", user["id"])


async def _has_totp(db, user: dict) -> bool:
    from bson import ObjectId
    full = await db.users.find_one({"_id": ObjectId(user["id"])},
                                   {"two_factor_enabled": 1}) or {}
    return bool(full.get("two_factor_enabled"))


@router.get("/credentials")
async def my_passkeys(user=Depends(get_current_user)):
    _require_admin(user)
    from webauthn_mfa import list_credentials
    return {"credentials": await list_credentials(get_db(), user["id"])}


@router.post("/register/begin")
async def register_begin(request: Request, payload: dict | None = None,
                         user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    await _require_enrolment_proof(db, user, request, payload)
    from webauthn_mfa import begin_registration
    try:
        return await begin_registration(db, user,
                                        _origin(request, payload))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message


@router.post("/register/complete")
async def register_complete(payload: dict, request: Request,
                            user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    from webauthn_mfa import complete_registration
    try:
        out = await complete_registration(
            db, user, str(payload.get("challenge_id") or ""),
            payload.get("credential") or {},
            str(payload.get("label") or ""))
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Passkey registration failed: {e}")
    await audit_event(db, user["id"], "passkey_enrolled",
                      {"label": out["label"],
                       "credential_id": out["credential_id"][:16]},
                      request)
    return out


@router.delete("/credentials/{credential_id}")
async def delete_passkey(credential_id: str, request: Request,
                         user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    from webauthn_mfa import remove_credential
    if not await remove_credential(db, user["id"], credential_id):
        raise HTTPException(status_code=404, detail="Passkey not found")
    await audit_event(db, user["id"], "passkey_removed",
                      {"credential_id": credential_id[:16]}, request)
    return {"removed": True}


@router.post("/step-up/begin")
async def step_up_begin(payload: dict, request: Request,
                        user=Depends(get_current_user)):
    _require_admin(user)
    action = str(payload.get("action") or "")
    if action not in STEP_UP_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown step-up action: {action}")
    if action == "passkey_enroll" and await _has_totp(get_db(), user):
        raise HTTPException(status_code=400, detail={
            "code": "totp_required", "action": action,
            "message": "Adding a passkey requires your authenticator-app "
                       "code, not a passkey."})
    from webauthn_mfa import begin_step_up
    try:
        return await begin_step_up(get_db(), user, _origin(request, payload),
                                   action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message


@router.post("/step-up/complete")
async def step_up_complete(payload: dict, request: Request,
                           user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    action = str(payload.get("action") or "")
    if action not in STEP_UP_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown step-up action: {action}")
    await check_failure_limit(db, "webauthn_stepup", user["id"], 5, 600,
                              "Too many failed passkey attempts. "
                              "Try again in a few minutes.")
    from webauthn_mfa import complete_step_up
    try:
        await complete_step_up(db, user,
                               str(payload.get("challenge_id") or ""),
                               action, payload.get("credential") or {})
    except Exception as e:
        await record_failure(db, "webauthn_stepup", user["id"], 600)
        await audit_event(db, user["id"], "step_up_failed",
                          {"action": action, "method": "webauthn",
                           "reason": str(e)[:120]}, request)
        raise HTTPException(status_code=401,
                            detail="Passkey verification failed")
    await clear_failures(db, "webauthn_stepup", user["id"])
    result = await issue_step_up_token(db, user["id"], action,
                                       method="webauthn")
    await audit_event(db, user["id"], "step_up_verified",
                      {"action": action, "method": "webauthn"},
                      request, step_up=True)
    return result
