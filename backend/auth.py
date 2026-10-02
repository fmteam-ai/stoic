import hashlib
import os
import time
import jwt
import bcrypt
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, Request
from bson import ObjectId

from database import get_db

JWT_ALGORITHM = "HS256"
# Short-lived access tokens (financial app) — the frontend silently rotates
# via /auth/refresh; refresh sessions are durable, rotated and revocable.
ACCESS_TOKEN_EXPIRE_MIN = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MIN", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "30"))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def create_access_token(user_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        # impr-auth — iat is compared against users.tokens_valid_after so
        # logout-all / password change / suspension kill live access tokens.
        "iat": int(now.timestamp()),
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MIN),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str, claims: dict | None = None) -> str:
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    if claims:                      # jti / sid / fam — revocable session
        payload.update(claims)
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])


def set_auth_cookies(response, access_token: str, refresh_token: str):
    response.set_cookie(
        key="access_token", value=access_token, httponly=True,
        secure=True, samesite="none", max_age=ACCESS_TOKEN_EXPIRE_MIN * 60, path="/"
    )
    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True,
        secure=True, samesite="none", max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400, path="/"
    )
    from security import set_csrf_cookie
    set_csrf_cookie(response)


def clear_auth_cookies(response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("csrf_token", path="/")


def require_admin(user: dict) -> None:
    """Admin gate: role check + mandatory TOTP MFA (iter-136).
    ADMIN_MFA_ENFORCED=false is a preview/CI escape hatch only — server.py
    refuses it when APP_ENV=production."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    if (os.environ.get("ADMIN_MFA_ENFORCED", "true").lower() == "true"
            and not user.get("two_factor_enabled")):
        raise HTTPException(status_code=403, detail={
            "code": "admin_mfa_required",
            "message": "Admin accounts require authenticator (TOTP) 2FA. "
                       "Enroll it in Settings → Security to unlock admin "
                       "functions.",
        })


# ------------------------------------------------ access-token revocation
# impr-auth — a per-user `tokens_valid_after` (epoch seconds) revokes every
# access token minted before it. Tolerates a little clock skew between
# workers so a token minted right after the revocation on a lagging host is
# not rejected.
TOKEN_REVOCATION_SKEW_SEC = int(os.environ.get("TOKEN_REVOCATION_SKEW_SEC", "1"))


def access_token_revoked(payload: dict, user: dict) -> bool:
    """True when the access token predates the user's revocation watermark.
    Tokens without `iat` (minted before this change) are treated as iat=0,
    i.e. revoked as soon as a watermark exists."""
    tva = user.get("tokens_valid_after")
    if not tva:
        return False
    try:
        iat = int(payload.get("iat") or 0)
        return iat + TOKEN_REVOCATION_SKEW_SEC < int(tva)
    except (TypeError, ValueError):
        return True


async def revoke_user_access_tokens(db, user_id: str) -> int:
    """Stamp `tokens_valid_after = now` (monotonic via $max). Returns the
    watermark. Callers that keep the acting session alive must mint fresh
    tokens AFTER this call."""
    now = int(time.time())
    try:
        oid = ObjectId(user_id)
    except Exception:
        return now
    await db.users.update_one({"_id": oid},
                              {"$max": {"tokens_valid_after": now}})
    return now


# impr-auth — server-side must_change_password gate: while the flag is set
# only these endpoints are reachable (frontend routes the user to Settings).
MUST_CHANGE_PASSWORD_ALLOWED = {"/api/auth/change-password", "/api/auth/logout",
                                "/api/auth/me"}
MUST_CHANGE_PASSWORD_CODE = "password_change_required"


def _request_path(request) -> str:
    try:
        return str(request.url.path).rstrip("/") or "/"
    except Exception:
        return ""


def check_user_access(user: dict | None, payload: dict) -> None:
    """Shared HTTP/WebSocket gate on an already-decoded access token: user
    exists, not suspended/terminated (admins exempt), token not revoked."""
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Block requests from suspended/terminated users mid-session (Terms §8).
    # Admins are never auto-blocked by their own suspension status.
    status = user.get("status") or "active"
    if status in ("suspended", "terminated") and user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "code": f"account_{status}",
                "message": f"Account {status}.",
            },
        )
    if access_token_revoked(payload, user):
        raise HTTPException(status_code=401, detail="Token revoked")


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        db = get_db()
        # The user doc is loaded on every request already, so the
        # revocation watermark check adds no extra round-trip.
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        check_user_access(user, payload)
        if user.get("must_change_password") \
                and _request_path(request) not in MUST_CHANGE_PASSWORD_ALLOWED:
            raise HTTPException(status_code=403, detail={
                "code": MUST_CHANGE_PASSWORD_CODE,
                "message": "You must change your one-time password before "
                           "using the platform (Settings → Password)."})
        user["id"] = str(user["_id"])
        user.pop("_id", None)
        user.pop("password_hash", None)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def authenticate_ws_token(token: str) -> str | None:
    """WebSocket auth helper (server.py /api/ws): returns the user id when
    the access token is valid, unrevoked and the account is usable, else
    None. Same gate as get_current_user minus the must_change_password
    allow-list (the socket is push-only)."""
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        user = await get_db().users.find_one({"_id": ObjectId(payload["sub"])})
        check_user_access(user, payload)
        if user.get("must_change_password"):
            return None
        return str(user["_id"])
    except Exception:
        return None


def generate_bridge_token() -> str:
    return secrets.token_urlsafe(32)


# ------------------------------------------------------- bridge tokens
# impr-auth — bridge tokens are stored as sha256 digests (`bridge_token_hash`)
# and the plaintext is returned to the user exactly once at creation/rotation.
def hash_bridge_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def bridge_token_fields(token: str) -> dict:
    """The fields a writer stores for a freshly minted bridge token
    (never the plaintext). `bridge_token_last4` keeps the masked display."""
    return {"bridge_token_hash": hash_bridge_token(token),
            "bridge_token_last4": (token or "")[-4:]}


def bridge_plaintext_fallback_enabled() -> bool:
    """Transitional (one release): accept accounts that still carry the
    legacy plaintext `bridge_token` field. Default ON; set
    BRIDGE_TOKEN_PLAINTEXT_FALLBACK=false once migrations/hash_bridge_tokens
    has run everywhere."""
    return (os.environ.get("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", "true")
            .strip().lower() not in ("0", "false", "no", "off"))
