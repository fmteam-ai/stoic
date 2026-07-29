"""Security hardening: Mongo-backed rate limiting, CSRF double-submit
verification, and revocable refresh-token sessions with rotation + reuse
detection. MongoDB is the shared store (no Redis in this stack)."""
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Cookie-less callers (EA bridge tokens, Stripe signatures, public auth
# bootstrap) are exempt by construction: enforcement triggers only when an
# auth cookie rides the request.
CSRF_EXEMPT_PREFIXES = ("/api/bridge/", "/api/webhook", "/api/payments/webhook")


# ---------------------------------------------------------------- rate limit
def _window_key(scope: str, identifier: str, window_sec: int, now) -> str:
    window_start = int(now.timestamp()) // window_sec * window_sec
    return f"{scope}:{identifier}:{window_start}"


async def rate_limit(db, scope: str, identifier: str, max_attempts: int,
                     window_sec: int, message: str | None = None,
                     request: Request | None = None) -> None:
    """Fixed-window counter shared across workers via an atomic $inc.
    Raises 429 once `max_attempts` is exceeded inside the window.

    Test-suite bypass: a request carrying X-RateLimit-Bypass matching the
    server-side RATE_LIMIT_BYPASS_TOKEN secret skips VOLUME limits only —
    failure-based lockouts (login/2fa) are never bypassed."""
    if request is not None:
        bypass = os.environ.get("RATE_LIMIT_BYPASS_TOKEN") or ""
        hdr = request.headers.get("x-ratelimit-bypass") or ""
        from app_env import is_production
        if (bypass and hdr
                and not is_production()
                and secrets.compare_digest(bypass, hdr)):
            return
    if scope in ("register", "pwreset"):
        exempt = {ip.strip() for ip in
                  (os.environ.get("RATE_LIMIT_EXEMPT_IPS") or "").split(",")
                  if ip.strip()}
        if identifier in exempt:
            return
    now = datetime.now(timezone.utc)
    key = _window_key(scope, identifier, window_sec, now)
    doc = await db.rate_limits.find_one_and_update(
        {"_id": key},
        {"$inc": {"n": 1},
         "$setOnInsert": {"expires_at": now + timedelta(seconds=window_sec * 2)}},
        upsert=True, return_document=True)
    if int((doc or {}).get("n") or 1) > max_attempts:
        window_start = int(now.timestamp()) // window_sec * window_sec
        retry_in = window_sec - (int(now.timestamp()) - window_start)
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited",
                    "message": message or
                    f"Too many attempts. Try again in {max(retry_in, 1)}s.",
                    "retry_after_sec": max(retry_in, 1)})


async def check_failure_limit(db, scope: str, identifier: str,
                              max_failures: int, window_sec: int,
                              message: str | None = None) -> None:
    """Brute-force gate that counts FAILURES only (successful logins never
    lock an account out): call this BEFORE verifying, `record_failure` on
    a bad attempt, `clear_failures` on success."""
    now = datetime.now(timezone.utc)
    doc = await db.rate_limits.find_one(
        {"_id": _window_key(scope, identifier, window_sec, now)})
    if doc and int(doc.get("n") or 0) >= max_failures:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited",
                    "message": message or
                    "Too many failed attempts. Try again later."})


async def record_failure(db, scope: str, identifier: str,
                         window_sec: int) -> None:
    now = datetime.now(timezone.utc)
    await db.rate_limits.update_one(
        {"_id": _window_key(scope, identifier, window_sec, now)},
        {"$inc": {"n": 1},
         "$setOnInsert": {"expires_at": now + timedelta(seconds=window_sec * 2)}},
        upsert=True)


async def clear_failures(db, scope: str, identifier: str) -> None:
    import re as _re
    await db.rate_limits.delete_many(
        {"_id": {"$regex": f"^{_re.escape(f'{scope}:{identifier}')}:"}})


def client_ip(request: Request) -> str:
    """Client IP for rate-limit keying. When TRUST_CF_CONNECTING_IP=true
    (origin reachable only via Cloudflare — e.g. behind cloudflared Tunnel),
    CF-Connecting-IP is authoritative and unspoofable. Otherwise uses the
    RIGHTMOST X-Forwarded-For entry — the hop appended by our trusted
    ingress — so attackers cannot rotate lockout keys by spoofing the
    leftmost value (SEC-002)."""
    if os.environ.get("TRUST_CF_CONNECTING_IP", "false").lower() == "true":
        cf = request.headers.get("cf-connecting-ip", "")
        if cf:
            return cf.strip()
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------- CSRF
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response, token: str | None = None) -> str:
    token = token or new_csrf_token()
    # Deliberately NOT httponly: the double-submit design requires JS to
    # read the cookie and echo it in the X-CSRF-Token header; an attacker's
    # cross-site page cannot read it, which is the whole defense.
    response.set_cookie(key=CSRF_COOKIE, value=token, httponly=False,
                        secure=True, samesite="none", max_age=30 * 86400,
                        path="/")
    return token


def csrf_origin_enforced() -> bool:
    """Origin allowlist enforcement: opt-in via env in preview, ALWAYS ON in
    production (iter-182 — no CSRF_ENFORCE_ORIGIN deploy secret needed)."""
    if os.environ.get("CSRF_ENFORCE_ORIGIN", "").lower() == "true":
        return True
    from app_env import is_production
    return is_production()


def _allowed_origins() -> set:
    raw = (os.environ.get("CORS_ORIGINS") or "").strip().strip('"').strip("'")
    if not raw or raw == "*":
        return set()
    origins = {o.strip().strip('"').strip("'").rstrip("/")
               for o in raw.split(",") if o.strip()}
    from app_env import is_production
    if is_production():
        # iter-182 — dev entries are ignored automatically in production so
        # the same CORS_ORIGINS value is safe in both environments.
        origins = {o for o in origins
                   if "localhost" not in o and "127.0.0.1" not in o
                   and ".preview.emergentagent.com" not in o}
    return origins


def csrf_check(request: Request) -> str | None:
    """Returns an error string when a cookie-authenticated mutating request
    fails CSRF validation; None when the request is fine."""
    if request.method in SAFE_METHODS:
        return None
    path = request.url.path
    if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
        return None
    has_cookie_auth = bool(request.cookies.get("access_token")
                           or request.cookies.get("refresh_token"))
    if not has_cookie_auth:
        return None                       # bearer/public callers: CORS-bound
    # Origin allowlist: opt-in via CSRF_ENFORCE_ORIGIN=true, automatic in
    # production (reverse proxies can rewrite Origin to internal hostnames,
    # so the double-submit token below remains the primary CSRF defense).
    if csrf_origin_enforced():
        allowed = _allowed_origins()
        origin = (request.headers.get("origin") or "").rstrip("/")
        if allowed and origin and origin not in allowed:
            return "origin_not_allowed"
    cookie_val = request.cookies.get(CSRF_COOKIE)
    header_val = request.headers.get(CSRF_HEADER)
    if not cookie_val or not header_val:
        return "csrf_token_missing"
    if not secrets.compare_digest(cookie_val, header_val):
        return "csrf_token_mismatch"
    return None


# ------------------------------------------------------ refresh sessions
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(db, user_id: str, request: Request | None = None,
                         family: str | None = None,
                         session_id: str | None = None) -> dict:
    """Registers a new refresh-token session. Returns the claims to embed."""
    from auth import REFRESH_TOKEN_EXPIRE_DAYS
    now = datetime.now(timezone.utc)
    claims = {"jti": uuid.uuid4().hex,
              "sid": session_id or uuid.uuid4().hex,
              "fam": family or uuid.uuid4().hex}
    await db.auth_sessions.insert_one({
        "jti": claims["jti"], "session_id": claims["sid"],
        "family": claims["fam"], "user_id": user_id,
        "token_hash": None,               # stamped after the JWT is minted
        "created_at": now.isoformat(),
        "last_used_at": now.isoformat(),
        "expires_at": now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        "ip": client_ip(request) if request else None,
        "user_agent": (request.headers.get("user-agent", "")[:200]
                       if request else None),
        "revoked": False, "consumed": False, "replaced_by_jti": None,
    })
    return claims


async def stamp_session_token(db, jti: str, refresh_token: str) -> None:
    await db.auth_sessions.update_one(
        {"jti": jti}, {"$set": {"token_hash": hash_token(refresh_token)}})


async def consume_and_rotate(db, payload: dict, presented_token: str,
                             request: Request | None = None) -> dict | None:
    """Validates + rotates a session-tracked refresh token.

    Returns new claims on success, None on legacy tokens (no jti — accepted
    once for migration), and raises 401 on revoked/reused tokens. REUSE of
    an already-consumed token revokes the ENTIRE family (stolen-token
    replay defense)."""
    jti = payload.get("jti")
    if not jti:
        return None                        # legacy pre-rotation token
    sess = await db.auth_sessions.find_one({"jti": jti})
    if sess is None or sess.get("revoked"):
        raise HTTPException(status_code=401, detail="Session revoked")
    # Idle timeout — a session unused for longer than the window is dead.
    idle_min = int(os.environ.get("SESSION_IDLE_TIMEOUT_MINUTES", "10080"))
    last_used = sess.get("last_used_at") or sess.get("created_at")
    if idle_min > 0 and last_used:
        try:
            last_dt = datetime.fromisoformat(
                str(last_used).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            idle = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            if idle > idle_min:
                await revoke_family(db, sess["family"],
                                    reason="session_idle_timeout")
                raise HTTPException(
                    status_code=401,
                    detail={"code": "session_idle_timeout",
                            "message": "Session expired due to inactivity. "
                                       "Please sign in again."})
        except HTTPException:
            raise
        except (ValueError, TypeError):
            pass
    if sess.get("consumed"):
        await revoke_family(db, sess["family"], reason="refresh_token_reuse")
        raise HTTPException(status_code=401,
                            detail="Refresh token reuse detected — all "
                                   "sessions in this chain were revoked")
    if sess.get("token_hash") and sess["token_hash"] != hash_token(presented_token):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    new_claims = await create_session(db, sess["user_id"], request,
                                      family=sess["family"],
                                      session_id=sess["session_id"])
    await db.auth_sessions.update_one(
        {"jti": jti, "consumed": False},
        {"$set": {"consumed": True, "replaced_by_jti": new_claims["jti"],
                  "last_used_at": datetime.now(timezone.utc).isoformat()}})
    return new_claims


async def revoke_family(db, family: str, reason: str) -> int:
    res = await db.auth_sessions.update_many(
        {"family": family, "revoked": False},
        {"$set": {"revoked": True, "revoked_reason": reason,
                  "revoked_at": datetime.now(timezone.utc).isoformat()}})
    return res.modified_count


async def revoke_all_user_sessions(db, user_id: str, reason: str) -> int:
    """Password change / reset / 2FA reset / suspension → every session dies."""
    res = await db.auth_sessions.update_many(
        {"user_id": user_id, "revoked": False},
        {"$set": {"revoked": True, "revoked_reason": reason,
                  "revoked_at": datetime.now(timezone.utc).isoformat()}})
    return res.modified_count


async def revoke_session_by_token_payload(db, payload: dict, reason: str):
    jti = payload.get("jti")
    if jti:
        sess = await db.auth_sessions.find_one({"jti": jti})
        if sess:
            await revoke_family(db, sess["family"], reason)
