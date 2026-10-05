"""iter-177 — WebAuthn passkey endpoints (admin-only step-up factor)."""
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from security import check_failure_limit, clear_failures, record_failure
from step_up import STEP_UP_ACTIONS, audit_event, issue_step_up_token, require_step_up

router = APIRouter(prefix="/auth/webauthn", tags=["webauthn"])


def _require_admin(user: dict) -> None:
    if (user or {}).get("role") != "admin":
        raise HTTPException(status_code=403,
                            detail="Passkeys are available to administrators")


def _origin(request: Request, payload: dict | None = None) -> str:
    """The browser's true origin. Client-declared (body) takes priority: the
    edge proxy rewrites the Origin header in some deployments. Safe because
    credentials are RP-scoped — a challenge minted for a foreign origin can
    only ever create/assert credentials for THAT RP ID, never ours — and the
    challenge pins rp_id+origin for the verify step."""
    # fix plan S1 — when WEBAUTHN_ORIGIN is pinned it is the ONLY accepted
    # origin (client-declared / header values are ignored); production
    # refuses the ceremony without a pinned origin and RP ID.
    pinned = (os.environ.get("WEBAUTHN_ORIGIN") or "").strip()
    if pinned:
        return pinned
    from app_env import is_production
    if is_production():
        raise HTTPException(status_code=503, detail="Passkeys unavailable: WEBAUTHN_RP_ID and WEBAUTHN_ORIGIN must be configured")
    declared = str((payload or {}).get("origin") or "").strip()
    return (declared or request.headers.get("origin") or "").strip()


@router.get("/credentials")
async def my_passkeys(user=Depends(get_current_user)):
    _require_admin(user)
    from webauthn_mfa import list_credentials
    return {"credentials": await list_credentials(get_db(), user["id"])}


@router.post("/register/begin")
async def register_begin(request: Request, payload: dict | None = None,
                         user=Depends(get_current_user)):
    _require_admin(user)
    await require_step_up(get_db(), user, request, "passkey_enrol")     # fix plan S1
    from webauthn_mfa import begin_registration
    try:
        return await begin_registration(get_db(), user,
                                        _origin(request, payload))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # deliberate ValueError message


@router.post("/register/complete")
async def register_complete(payload: dict, request: Request,
                            user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    # main92 frontend review — the step-up was already verified at /register/begin, which
    # is the only way to obtain a challenge_id (user-bound, single-use, short-lived); asking
    # for a second code here made passkey enrolment prompt for 2FA twice.
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
    await require_step_up(db, user, request, "passkey_enrol")           # fix plan S1
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
    result = await issue_step_up_token(db, user["id"], action)
    await audit_event(db, user["id"], "step_up_verified",
                      {"action": action, "method": "webauthn"},
                      request, step_up=True)
    return result
