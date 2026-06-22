from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from bson import ObjectId

from auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    set_auth_cookies, clear_auth_cookies, get_current_user,
)
from database import get_db
from models import (
    RegisterRequest, LoginRequest, UserOut,
    ProfileUpdateRequest, ChangePasswordRequest,
    TOTPVerifyRequest, TOTPDisableRequest,
)
from totp import (
    new_secret, provisioning_uri, qr_png_data_url,
    verify_code, generate_recovery_codes, hash_recovery_codes,
    consume_recovery_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_to_out(user_doc: dict) -> UserOut:
    return UserOut(
        id=str(user_doc.get("_id") or user_doc.get("id")),
        email=user_doc["email"],
        name=user_doc.get("name"),
        role=user_doc.get("role", "user"),
        created_at=user_doc.get("created_at") if isinstance(user_doc.get("created_at"), datetime) else None,
        two_factor_enabled=bool(user_doc.get("two_factor_enabled", False)),
    )


@router.post("/register", response_model=UserOut)
async def register(payload: RegisterRequest, request: Request, response: Response):
    db = get_db()
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    ref_code = request.cookies.get("stoic_ref")
    ref_at = request.cookies.get("stoic_ref_at")

    user_doc = {
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name or email.split("@")[0],
        "role": "user",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "two_factor_enabled": False,
    }
    if ref_code:
        user_doc["referred_by_code"] = ref_code.upper()
        user_doc["referred_at"] = ref_at or datetime.now(timezone.utc).isoformat()
    result = await db.users.insert_one(user_doc)
    uid = str(result.inserted_id)

    await db.bot_configs.update_one(
        {"user_id": uid},
        {"$setOnInsert": {
            "user_id": uid,
            "risk_level": "medium",
            "symbols": ["XAUUSD", "BTCUSD"],
            "active": False,
            "max_concurrent_trades": 3,
            "auto_execute": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    set_auth_cookies(response, access, refresh)
    return UserOut(id=uid, email=email, name=user_doc["name"], role="user", two_factor_enabled=False)


@router.post("/login")
async def login(payload: LoginRequest, response: Response):
    db = get_db()
    email = payload.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # 2FA gate — if enabled, require a valid TOTP or recovery code on this same call
    if user.get("two_factor_enabled"):
        provided = (payload.totp_code or "").strip()
        if not provided:
            raise HTTPException(status_code=401, detail="2FA code required")
        ok = verify_code(user.get("totp_secret") or "", provided)
        if not ok:
            stored_codes = user.get("recovery_codes") or []
            consumed, remaining = consume_recovery_code(stored_codes, provided)
            if not consumed:
                raise HTTPException(status_code=401, detail="Invalid 2FA code")
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"recovery_codes": remaining}})

    uid = str(user["_id"])
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    set_auth_cookies(response, access, refresh)
    return _user_to_out({**user, "_id": uid})


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


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
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise HTTPException(status_code=401, detail="User missing")
        access = create_access_token(uid, user["email"])
        new_refresh = create_refresh_token(uid)
        set_auth_cookies(response, access, new_refresh)
        return {"ok": True}
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


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
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {"password_hash": hash_password(payload.new_password)}},
    )
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
    return {"ok": True}
