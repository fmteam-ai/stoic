from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from bson import ObjectId

from auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    set_auth_cookies, clear_auth_cookies, get_current_user,
)
from database import get_db
from models import RegisterRequest, LoginRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut)
async def register(payload: RegisterRequest, request: Request, response: Response):
    db = get_db()
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    # Pick up affiliate attribution from cookie (set by /api/r/{code})
    ref_code = request.cookies.get("stoic_ref")
    ref_at = request.cookies.get("stoic_ref_at")

    user_doc = {
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name or email.split("@")[0],
        "role": "user",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if ref_code:
        user_doc["referred_by_code"] = ref_code.upper()
        user_doc["referred_at"] = ref_at or datetime.now(timezone.utc).isoformat()
    result = await db.users.insert_one(user_doc)
    uid = str(result.inserted_id)

    # Bootstrap default bot config
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
    return UserOut(id=uid, email=email, name=user_doc["name"], role="user")


@router.post("/login", response_model=UserOut)
async def login(payload: LoginRequest, response: Response):
    db = get_db()
    email = payload.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    uid = str(user["_id"])
    access = create_access_token(uid, email)
    refresh = create_refresh_token(uid)
    set_auth_cookies(response, access, refresh)
    return UserOut(id=uid, email=email, name=user.get("name"), role=user.get("role", "user"))


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(id=user["id"], email=user["email"],
                   name=user.get("name"), role=user.get("role", "user"))


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
