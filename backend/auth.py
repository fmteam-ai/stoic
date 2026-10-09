import os
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


def create_access_token(user_id: str, email: str, sid: str | None = None) -> str:
    """P1-02 — access tokens carry iat + the refresh session id (sid) so revocation of the
    session (logout, revoke-all, password change, security agent) ends them immediately."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MIN),
    }
    if sid:
        payload["sid"] = sid
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
    # A20-P2-02 — JS-readable session HINT (no secret: constant "1"): lets the SPA skip the /auth/refresh
    # round trip on anonymous visits; the real session stays in the httpOnly cookies above.
    response.set_cookie(
        key="stoic_session", value="1", httponly=False,
        secure=True, samesite="none", max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400, path="/"
    )
    from security import set_csrf_cookie
    set_csrf_cookie(response)


def clear_auth_cookies(response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("stoic_session", path="/")
    response.delete_cookie("csrf_token", path="/")


def master_admin_email() -> str:
    return (os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com").strip().lower()


def is_master_admin(email: str | None) -> bool:
    return bool(email) and str(email).strip().lower() == master_admin_email()


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


# P1-03 — while must_change_password is set, only these paths are reachable (HTTP); WebSockets refuse.
PASSWORD_CHANGE_ALLOWED_PATHS = ("/api/auth/me", "/api/auth/change-password", "/api/auth/logout",
                                 "/api/auth/refresh", "/api/auth/csrf", "/api/auth/sessions",
                                 "/api/auth/sessions/revoke-all")


class TokenRejected(Exception):
    def __init__(self, status: int, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def validate_access_token(db, token: str, *, path: str | None = None) -> dict:
    """P1-02 / P1-03 — the ONE validator for HTTP and WebSockets.
    Decode → type → user exists → status → session (sid) not revoked → iat ≥ user's
    tokens_valid_after → forced-password-change gate. Raises TokenRejected."""
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise TokenRejected(401, "Token expired")
    except jwt.InvalidTokenError:
        raise TokenRejected(401, "Invalid token")
    if payload.get("type") != "access":
        raise TokenRejected(401, "Invalid token type")
    if payload.get("iat") is None:
        raise TokenRejected(401, "Token predates revocable sessions — sign in again")
    try:
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:  # noqa: BLE001
        user = None
    if not user:
        raise TokenRejected(401, "User not found")
    status = user.get("status") or "active"
    # audit r30 P3 — the anti-lockout exemption applies to the MASTER admin only; any other
    # suspended/terminated admin is stopped like every other user.
    if status in ("suspended", "terminated") and not is_master_admin(user.get("email")):
        raise TokenRejected(403, {"code": f"account_{status}", "message": f"Account {status}."})
    sid = payload.get("sid")
    if sid:
        revoked = await db.auth_sessions.find_one({"session_id": sid, "revoked": True}, {"_id": 1})
        if revoked:
            raise TokenRejected(401, {"code": "session_revoked", "message": "Session revoked — sign in again."})
    valid_after = user.get("tokens_valid_after")
    if valid_after:
        try:
            va = datetime.fromisoformat(str(valid_after).replace("Z", "+00:00"))
            if va.tzinfo is None:
                va = va.replace(tzinfo=timezone.utc)
            if int(payload["iat"]) < int(va.timestamp()):
                raise TokenRejected(401, {"code": "session_revoked", "message": "Session revoked — sign in again."})
        except (TypeError, ValueError):
            pass
    if user.get("must_change_password"):
        if path is None or not any(path == p or path.startswith(p + "/") for p in PASSWORD_CHANGE_ALLOWED_PATHS):
            raise TokenRejected(403, {"code": "password_change_required",
                                      "message": "You must change your password before continuing."})
    user["id"] = str(user["_id"])
    user.pop("_id", None)
    user.pop("password_hash", None)
    return user


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return await validate_access_token(get_db(), token, path=request.url.path)
    except TokenRejected as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


def generate_bridge_token() -> str:
    return secrets.token_urlsafe(32)
