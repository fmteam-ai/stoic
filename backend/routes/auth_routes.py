import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from bson import ObjectId

from auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    set_auth_cookies, clear_auth_cookies, get_current_user,
)
from database import get_db
from hibp import is_password_breached, BREACHED_DETAIL
from models import (
    RegisterRequest, LoginRequest, UserOut,
    ProfileUpdateRequest, ChangePasswordRequest,
    TOTPVerifyRequest, TOTPDisableRequest,
    VerifyEmailRequest, ResendActivationRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from totp import (
    new_secret, provisioning_uri, qr_png_data_url,
    verify_code, generate_recovery_codes, hash_recovery_codes,
    consume_recovery_code,
)
from activation import (
    new_activation_token, send_activation_email,
    RESEND_COOLDOWN_SECONDS,
)
from password_reset import (
    new_reset_token, send_reset_email,
    RESET_RESEND_COOLDOWN_SECONDS,
)
from terms_of_use import TERMS_VERSION
from security import (
    rate_limit, client_ip,
    check_failure_limit, record_failure, clear_failures,
    create_session, stamp_session_token, consume_and_rotate,
    revoke_all_user_sessions, revoke_session_by_token_payload,
)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_session_cookies(db, uid: str, email: str, response,
                                 request: Request | None = None):
    """Access token + session-tracked (revocable, rotating) refresh token."""
    claims = await create_session(db, uid, request)
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid, claims)
    await stamp_session_token(db, claims["jti"], refresh)
    set_auth_cookies(response, access, refresh)


def _user_to_out(user_doc: dict) -> UserOut:
    return UserOut(
        id=str(user_doc.get("_id") or user_doc.get("id")),
        email=user_doc["email"],
        name=user_doc.get("name"),
        role=user_doc.get("role", "user"),
        created_at=user_doc.get("created_at") if isinstance(user_doc.get("created_at"), datetime) else None,
        two_factor_enabled=bool(user_doc.get("two_factor_enabled", False)),
        email_verified=bool(user_doc.get("email_verified", True)),
        must_change_password=bool(user_doc.get("must_change_password", False)),
    )


@router.post("/register")
async def register(payload: RegisterRequest, request: Request, response: Response):
    db = get_db()
    email = payload.email.lower()
    await rate_limit(db, "register", client_ip(request),
                     int(os.environ.get("REGISTER_RATE_MAX_PER_HOUR", "30")),
                     3600,
                     "Too many registrations from this address. Try later.",
                     request=request)

    # Terms of Use must be accepted.
    if not payload.terms_agreed:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "terms_required",
                "message": "You must accept the Terms of Use to create an account.",
            },
        )

    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    # HIBP k-anonymity breached-password screen (fail-open on outage).
    if await is_password_breached(payload.password):
        raise HTTPException(status_code=422, detail=BREACHED_DETAIL)

    ref_code = request.cookies.get("stoic_ref")
    ref_at = request.cookies.get("stoic_ref_at")
    now_iso = datetime.now(timezone.utc).isoformat()
    token, exp_iso = new_activation_token()

    user_doc = {
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name or email.split("@")[0],
        "role": "user",
        "status": "active",
        "created_at": now_iso,
        "two_factor_enabled": False,
        # Terms acceptance audit trail
        "accepted_terms_version": payload.terms_version or TERMS_VERSION,
        "accepted_terms_at": now_iso,
        # Email verification gate
        "email_verified": False,
        "activation_token": token,
        "activation_expires_at": exp_iso,
        "activation_sent_at": now_iso,
    }
    if ref_code:
        user_doc["referred_by_code"] = ref_code.upper()
        user_doc["referred_at"] = ref_at or now_iso
    result = await db.users.insert_one(user_doc)
    uid = str(result.inserted_id)

    await db.bot_configs.update_one(
        {"user_id": uid, "$or": [{"account_id": None},
                                 {"account_id": {"$exists": False}}]},
        {"$setOnInsert": {
            "user_id": uid,
            "account_id": None,
            "risk_level": "medium",
            "symbols": ["XAUUSD", "BTCUSD"],
            "active": False,
            "max_concurrent_trades": 3,
            "auto_execute": True,
            "max_lot_size": 0.0,
            "updated_at": now_iso,
        }},
        upsert=True,
    )

    # Fire activation email (async, non-blocking on errors)
    send_result = await send_activation_email(
        recipient=email, name=user_doc["name"], token=token,
    )

    # We deliberately DO NOT set auth cookies — the user must verify their
    # email before the dashboard becomes accessible. This blocks accidental
    # auto-login and forces the explicit "click the link" step.
    return {
        "id": uid,
        "email": email,
        "name": user_doc["name"],
        "email_verified": False,
        "activation_email_sent": bool(send_result.get("ok")),
        "activation_email_error": send_result.get("error"),
        # In dev (no Resend key), surface the link so the user can finish flow.
        "activation_link_dev_only": send_result.get("activation_link_dev_only"),
        "message": "Account created. Check your inbox to activate your STOIC membership.",
    }


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    db = get_db()
    email = payload.email.lower()
    ip = client_ip(request)
    # Failed-attempt lockout: 5 wrong passwords per ip+email per 10 min.
    # Successful logins never count toward the limit.
    await check_failure_limit(db, "login", f"{ip}:{email}", 5, 600,
                              "Too many failed login attempts. "
                              "Try again in a few minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        await record_failure(db, "login", f"{ip}:{email}", 600)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Block suspended/terminated accounts (Terms §8)
    status = user.get("status") or "active"
    if status == "suspended":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_suspended",
                "message": "Your account has been suspended for violating the STOIC Terms of Use. Contact support to appeal.",
                "reason": user.get("suspension_reason") or "",
            },
        )
    if status == "terminated":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_terminated",
                "message": "Your account has been permanently terminated for violating the STOIC Terms of Use.",
                "reason": user.get("terminated_reason") or "",
            },
        )

    # Block until email is verified — admins are grandfathered through.
    if user.get("email_verified") is False and user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "account_unverified",
                "message": "Please verify your email to activate your account. Check your inbox for the activation link.",
                "email": email,
            },
        )

    # 2FA gate — if enabled, require a valid TOTP or recovery code on this same call
    if user.get("two_factor_enabled"):
        provided = (payload.totp_code or "").strip()
        if not provided:
            raise HTTPException(status_code=401, detail="2FA code required")
        await check_failure_limit(db, "2fa", email, 5, 600,
                                  "Too many failed 2FA attempts. "
                                  "Try again in a few minutes.")
        ok = verify_code(user.get("totp_secret") or "", provided)
        if not ok:
            stored_codes = user.get("recovery_codes") or []
            consumed, remaining = consume_recovery_code(stored_codes, provided)
            if not consumed:
                await record_failure(db, "2fa", email, 600)
                raise HTTPException(status_code=401, detail="Invalid 2FA code")
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"recovery_codes": remaining}})
    else:
        # Email OTP gate (admin-toggled) — TOTP-enrolled users skip it.
        from login_otp import otp_gate
        await otp_gate(db, user, payload.email_otp)

    uid = str(user["_id"])
    await clear_failures(db, "login", f"{ip}:{email}")
    await clear_failures(db, "2fa", email)
    await _issue_session_cookies(db, uid, email, response, request)
    return _user_to_out({**user, "_id": uid})


@router.post("/logout")
async def logout(request: Request, response: Response):
    # Revoke the refresh session server-side — deleting the browser cookie
    # alone would leave a stolen copy of the token alive until expiry.
    token = request.cookies.get("refresh_token")
    if token:
        try:
            payload = decode_token(token)
            await revoke_session_by_token_payload(get_db(), payload, "logout")
        except Exception:
            pass
    clear_auth_cookies(response)
    return {"ok": True}


# ---------- Email verification / activation ----------
@router.post("/verify-email")
async def verify_email(payload: VerifyEmailRequest, request: Request, response: Response):
    """Activate a user account via the token from the welcome email.

    On success, sets auth cookies so the user lands authenticated on the
    dashboard. Tokens are single-use (cleared after consumption) and
    expire 24h after issuance.
    """
    db = get_db()
    user = await db.users.find_one({"activation_token": payload.token})
    if not user:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_token",
                    "message": "Activation link is invalid or already used."},
        )

    # Expiry check (compare ISO strings safely via datetime parse).
    exp_raw = user.get("activation_expires_at")
    try:
        exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00")) if exp_raw else None
    except Exception:
        exp_dt = None
    if exp_dt and exp_dt < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail={"code": "expired_token",
                    "message": "Activation link has expired. Request a new one."},
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "email_verified": True,
                "email_verified_at": datetime.now(timezone.utc).isoformat(),
            },
            "$unset": {"activation_token": "", "activation_expires_at": ""},
        },
    )

    uid = str(user["_id"])
    await _issue_session_cookies(db, uid, user["email"], response, request)
    return {
        "ok": True,
        "user": _user_to_out({**user, "_id": uid, "email_verified": True}),
        "message": "Email verified. Welcome to STOIC.",
    }


@router.post("/resend-activation")
async def resend_activation(payload: ResendActivationRequest):
    """Re-send the activation email. Rate-limited 1/min per account.

    To avoid email-enumeration, we always return the same generic success
    response — even when the email isn't on file or is already verified.
    """
    generic_ok = {
        "ok": True,
        "message": "If an unverified account exists for that email, an activation link has been sent.",
    }
    db = get_db()
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user:
        return generic_ok
    if user.get("email_verified"):
        return generic_ok

    # Cooldown
    sent_raw = user.get("activation_sent_at")
    if sent_raw:
        try:
            sent_dt = datetime.fromisoformat(sent_raw.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - sent_dt).total_seconds()
            if age < RESEND_COOLDOWN_SECONDS:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "rate_limited",
                        "message": f"Please wait {int(RESEND_COOLDOWN_SECONDS - age)}s before requesting another activation email.",
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass  # ignore parse issues, allow resend

    token, exp_iso = new_activation_token()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "activation_token": token,
            "activation_expires_at": exp_iso,
            "activation_sent_at": now_iso,
        }},
    )
    await send_activation_email(
        recipient=user["email"],
        name=user.get("name") or user["email"].split("@")[0],
        token=token,
    )
    return generic_ok


# ---------- Password reset ----------
@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest, request: Request):
    """Request a password-reset email. Generic-success response so
    attackers can't enumerate emails. 60s per-account cooldown + IP limit."""
    generic_ok = {
        "ok": True,
        "message": "If an account exists for that email, a reset link has been sent.",
    }
    db = get_db()
    await rate_limit(db, "pwreset", client_ip(request),
                     int(os.environ.get("PWRESET_RATE_MAX_PER_HOUR", "20")),
                     3600, "Too many reset requests. Try later.",
                     request=request)
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user:
        return generic_ok

    # Suspended/terminated users can't reset — would re-open access to
    # accounts we've deliberately closed.
    if (user.get("status") or "active") in ("suspended", "terminated"):
        return generic_ok

    # Cooldown
    sent_raw = user.get("password_reset_sent_at")
    if sent_raw:
        try:
            sent_dt = datetime.fromisoformat(sent_raw.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - sent_dt).total_seconds()
            if age < RESET_RESEND_COOLDOWN_SECONDS:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "rate_limited",
                        "message": f"Please wait {int(RESET_RESEND_COOLDOWN_SECONDS - age)}s before requesting another reset email.",
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass

    token, exp_iso = new_reset_token()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_reset_token": token,
            "password_reset_expires_at": exp_iso,
            "password_reset_sent_at": now_iso,
        }},
    )
    await send_reset_email(
        recipient=user["email"],
        name=user.get("name") or user["email"].split("@")[0],
        token=token,
    )
    return generic_ok


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest):
    """Consume a reset token and set the new password."""
    db = get_db()
    user = await db.users.find_one({"password_reset_token": payload.token})
    if not user:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_token",
                    "message": "Reset link is invalid or already used."},
        )

    exp_raw = user.get("password_reset_expires_at")
    try:
        exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00")) if exp_raw else None
    except Exception:
        exp_dt = None
    if exp_dt and exp_dt < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail={"code": "expired_token",
                    "message": "Reset link has expired. Request a new one."},
        )

    # HIBP breached-password screen (fail-open on outage).
    if await is_password_breached(payload.new_password):
        raise HTTPException(status_code=422, detail=BREACHED_DETAIL)

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(payload.new_password),
                "password_reset_at": datetime.now(timezone.utc).isoformat(),
            },
            "$unset": {
                "password_reset_token": "",
                "password_reset_expires_at": "",
            },
        },
    )
    await revoke_all_user_sessions(db, str(user["_id"]), "password_reset")
    return {"ok": True,
            "message": "Password updated. You can now sign in with your new password.",
            "email": user["email"]}


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return _user_to_out({**user, "_id": user["id"]})


@router.post("/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = decode_token(token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Bad token type")
        uid = payload["sub"]
        db = get_db()
        await rate_limit(db, "refresh", payload.get("sid") or uid, 30, 60)
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise HTTPException(status_code=401, detail="User missing")
        # Rotation + reuse detection; a replayed (already-consumed) token
        # revokes its whole family. Legacy tokens without a jti migrate
        # into a tracked session on their first refresh.
        claims = await consume_and_rotate(db, payload, token, request)
        if claims is None:
            claims = await create_session(db, uid, request)
        access = create_access_token(uid, user["email"])
        new_refresh = create_refresh_token(uid, claims)
        await stamp_session_token(db, claims["jti"], new_refresh)
        set_auth_cookies(response, access, new_refresh)
        return {"ok": True}
    except HTTPException as e:
        if e.status_code == 429:
            raise
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@router.get("/csrf")
async def csrf_bootstrap(response: Response):
    """Issues the double-submit CSRF cookie for sessions created before
    CSRF enforcement (or after a cookie wipe)."""
    from security import set_csrf_cookie
    set_csrf_cookie(response)
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(request: Request, user=Depends(get_current_user)):
    db = get_db()
    cur_jti = None
    tok = request.cookies.get("refresh_token")
    if tok:
        try:
            cur_jti = decode_token(tok).get("jti")
        except Exception:
            pass
    out = []
    async for s in db.auth_sessions.find(
            {"user_id": user["id"], "revoked": False, "consumed": False}
    ).sort("created_at", -1).limit(50):
        out.append({"session_id": s["session_id"],
                    "created_at": s.get("created_at"),
                    "last_used_at": s.get("last_used_at"),
                    "ip": s.get("ip"), "user_agent": s.get("user_agent"),
                    "current": s.get("jti") == cur_jti})
    return {"sessions": out}


@router.post("/sessions/revoke-all")
async def revoke_all(user=Depends(get_current_user)):
    n = await revoke_all_user_sessions(get_db(), user["id"], "user_requested")
    return {"ok": True, "revoked": n}


# ---------- Profile ----------
@router.put("/profile", response_model=UserOut)
async def update_profile(payload: ProfileUpdateRequest, user=Depends(get_current_user)):
    db = get_db()
    updates = {}
    if payload.name is not None:
        updates["name"] = payload.name.strip()
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    await db.users.update_one({"_id": ObjectId(user["id"])}, {"$set": updates})
    fresh = await db.users.find_one({"_id": ObjectId(user["id"])})
    return _user_to_out(fresh)


# ---------- Password ----------
@router.post("/change-password")
async def change_password(payload: ChangePasswordRequest, user=Depends(get_current_user)):
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not full or not verify_password(payload.current_password, full["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from current")
    # HIBP breached-password screen (fail-open on outage).
    if await is_password_breached(payload.new_password):
        raise HTTPException(status_code=422, detail=BREACHED_DETAIL)
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {"password_hash": hash_password(payload.new_password),
                  "must_change_password": False}},
    )
    # Password change kills every existing session (stolen-cookie defense).
    await revoke_all_user_sessions(db, user["id"], "password_change")
    return {"ok": True}


# ---------- 2FA ----------
@router.get("/2fa/status")
async def two_fa_status(user=Depends(get_current_user)):
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    return {
        "enabled": bool(full.get("two_factor_enabled", False)),
        "recovery_codes_remaining": len(full.get("recovery_codes") or []),
    }


@router.post("/2fa/enroll")
async def two_fa_enroll(user=Depends(get_current_user)):
    """Issue a NEW secret (overwrites pending) and return QR + URI.

    The secret is stored on the user but `two_factor_enabled` stays false
    until verify-enroll succeeds.
    """
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    if full.get("two_factor_enabled"):
        raise HTTPException(status_code=400, detail="2FA already enabled — disable it first to re-enroll")
    secret = new_secret()
    uri = provisioning_uri(secret, full["email"])
    qr = qr_png_data_url(uri)
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {"totp_secret_pending": secret}},
    )
    return {"secret": secret, "otpauth_uri": uri, "qr_png_data_url": qr}


@router.post("/2fa/verify-enroll")
async def two_fa_verify_enroll(payload: TOTPVerifyRequest, user=Depends(get_current_user)):
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    if full.get("two_factor_enabled"):
        raise HTTPException(status_code=400, detail="2FA already enabled")
    pending = full.get("totp_secret_pending")
    if not pending:
        raise HTTPException(status_code=400, detail="No pending 2FA enrollment — call /2fa/enroll first")
    if not verify_code(pending, payload.code):
        raise HTTPException(status_code=401, detail="Invalid 2FA code")
    recovery_plain = generate_recovery_codes()
    recovery_hashed = hash_recovery_codes(recovery_plain)
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {
            "$set": {
                "totp_secret": pending,
                "two_factor_enabled": True,
                "recovery_codes": recovery_hashed,
            },
            "$unset": {"totp_secret_pending": ""},
        },
    )
    # Plaintext recovery codes are returned ONCE.
    return {"ok": True, "recovery_codes": recovery_plain}


@router.post("/2fa/disable")
async def two_fa_disable(payload: TOTPDisableRequest, user=Depends(get_current_user)):
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not full.get("two_factor_enabled"):
        raise HTTPException(status_code=400, detail="2FA is not enabled")
    if not verify_password(payload.current_password, full["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if not verify_code(full.get("totp_secret") or "", payload.code):
        # Allow recovery-code fallback for disable
        consumed, _ = consume_recovery_code(full.get("recovery_codes") or [], payload.code)
        if not consumed:
            raise HTTPException(status_code=401, detail="Invalid 2FA code")
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {
            "$set": {"two_factor_enabled": False},
            "$unset": {"totp_secret": "", "totp_secret_pending": "", "recovery_codes": ""},
        },
    )
    # 2FA reset is a security-posture change → revoke all other sessions.
    await revoke_all_user_sessions(db, user["id"], "2fa_reset")
    return {"ok": True}


# ------------------------------------------------------------- Step-up MFA
from models import StepUpRequest  # noqa: E402
from step_up import issue_step_up_token, audit_event, STEP_UP_ACTIONS  # noqa: E402


@router.post("/step-up")
async def step_up_verify(payload: StepUpRequest, request: Request,
                         user=Depends(get_current_user)):
    """Exchange a fresh TOTP code for a short-lived (5 min), single-use
    step-up token gating live-sensitive operations."""
    if payload.action not in STEP_UP_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown step-up action: {payload.action}")
    db = get_db()
    full = await db.users.find_one({"_id": ObjectId(user["id"])})
    if not full.get("two_factor_enabled"):
        raise HTTPException(status_code=403, detail={
            "code": "mfa_enrollment_required", "action": payload.action,
            "message": "Enable two-factor authentication first "
                       "(Settings → Security)."})
    await check_failure_limit(db, "stepup", user["id"], 5, 600,
                              "Too many failed step-up attempts. "
                              "Try again in a few minutes.")
    if not verify_code(full.get("totp_secret") or "", payload.code):
        await record_failure(db, "stepup", user["id"], 600)
        await audit_event(db, user["id"], "step_up_failed",
                          {"action": payload.action}, request)
        raise HTTPException(status_code=401, detail="Invalid 2FA code")
    await clear_failures(db, "stepup", user["id"])
    result = await issue_step_up_token(db, user["id"], payload.action)
    await audit_event(db, user["id"], "step_up_verified",
                      {"action": payload.action}, request, step_up=True)
    return result


@router.get("/audit")
async def my_audit_trail(limit: int = 50, user=Depends(get_current_user)):
    """User-visible slice of the append-only security audit trail."""
    db = get_db()
    n = min(max(int(limit), 1), 200)
    docs = await db.audit_log.find({"user_id": user["id"]}) \
        .sort("at", -1).to_list(length=n)
    return [{"action": d.get("action"), "detail": d.get("detail") or {},
             "step_up_verified": bool(d.get("step_up_verified")),
             "ip": d.get("ip"), "at": d.get("at")} for d in docs]
