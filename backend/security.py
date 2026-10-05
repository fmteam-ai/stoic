"""Security hardening: Mongo-backed rate limiting, CSRF double-submit
verification, and revocable refresh-token sessions with rotation + reuse
detection. MongoDB is the shared store (no Redis in this stack)."""
import asyncio
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
        from app_env import bypass_token
        bypass = bypass_token("RATE_LIMIT_BYPASS_TOKEN")
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
    # r23: no X-Real-IP fallback — only the ingress-appended hop or the socket peer.
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
                         session_id: str | None = None,
                         jti: str | None = None) -> dict:
    """Registers a new refresh-token session. Returns the claims to embed."""
    from auth import REFRESH_TOKEN_EXPIRE_DAYS
    now = datetime.now(timezone.utc)
    claims = {"jti": jti or uuid.uuid4().hex,
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
                             request: Request | None = None,
                             _trusted: bool = False) -> dict | None:
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
        # Concurrent refresh (second tab / parallel 401s) re-presents the token the
        # first refresh consumed moments ago. With a matching hash and inside a short
        # grace window this is benign: continue the chain from its newest successor.
        # A genuine replay (hash mismatch, outside grace, successor missing/revoked)
        # still revokes the whole family.
        head = await _grace_successor(db, sess, presented_token, _trusted)
        if head is None:
            await revoke_family(db, sess["family"], reason="refresh_token_reuse")
            from security_agent.events import emit as _sa_emit
            await _sa_emit(db, "refresh_token_reuse", {"user_id": str(sess.get("user_id")), "family": str(sess.get("family"))[:12]})
            raise HTTPException(status_code=401,
                                detail="Refresh token reuse detected — all "
                                       "sessions in this chain were revoked")
        sess, jti = head, head["jti"]
    elif not _trusted and sess.get("token_hash") and sess["token_hash"] != hash_token(presented_token):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    # Consume FIRST and atomically — a parallel refresh of the same jti loses the
    # race here instead of minting a second live successor.
    now_iso = datetime.now(timezone.utc).isoformat()
    next_jti = uuid.uuid4().hex          # published with the consume so racers can follow the chain
    won = await db.auth_sessions.find_one_and_update(
        {"jti": jti, "consumed": False, "revoked": False},
        {"$set": {"consumed": True, "consumed_at": now_iso, "last_used_at": now_iso,
                  "replaced_by_jti": next_jti}})
    if won is None:
        sess = await db.auth_sessions.find_one({"jti": jti}) or sess
        head = await _grace_successor(db, sess, presented_token, True)
        if head is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        return await consume_and_rotate(db, {**payload, "jti": head["jti"]}, presented_token, request, _trusted=True)
    return await create_session(db, sess["user_id"], request, family=sess["family"],
                                session_id=sess["session_id"], jti=next_jti)


REFRESH_REUSE_GRACE_SECONDS = int(os.environ.get("REFRESH_REUSE_GRACE_SECONDS", "60"))


async def _grace_successor(db, sess: dict, presented_token: str,
                           trusted: bool = False) -> dict | None:
    """For a consumed session re-presented within the grace window (same token
    hash, unless the caller already proved possession), returns the newest
    unconsumed, unrevoked successor in its chain."""
    if REFRESH_REUSE_GRACE_SECONDS <= 0:
        return None
    if not trusted and (not sess.get("token_hash") or sess["token_hash"] != hash_token(presented_token)):
        return None
    stamp = sess.get("consumed_at") or sess.get("last_used_at")
    try:
        consumed_dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if consumed_dt.tzinfo is None:
            consumed_dt = consumed_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    if (datetime.now(timezone.utc) - consumed_dt).total_seconds() > REFRESH_REUSE_GRACE_SECONDS:
        return None
    cur, hops = sess, 0
    while cur.get("consumed") and cur.get("replaced_by_jti") and hops < 10:
        nxt = None
        for _ in range(20):              # successor is inserted right after the consume — may still be in flight
            nxt = await db.auth_sessions.find_one({"jti": cur["replaced_by_jti"]})
            if nxt is not None:
                break
            await asyncio.sleep(0.05)
        cur, hops = nxt, hops + 1
        if cur is None or cur.get("revoked"):
            return None
    if cur.get("consumed") or cur.get("revoked") or cur["jti"] == sess["jti"]:
        return None
    return cur


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


class StripStrayCorsCredentials:
    """r21: never emit Access-Control-Allow-Credentials without a matched origin."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def _send(message):
            if message["type"] == "http.response.start":
                hdrs = message.get("headers") or []
                has_origin = any(k.lower() == b"access-control-allow-origin" for k, _ in hdrs)
                if not has_origin:
                    message["headers"] = [(k, v) for k, v in hdrs
                                          if k.lower() != b"access-control-allow-credentials"]
            await send(message)

        await self.app(scope, receive, _send)


def token_digest(token: str) -> str:
    """r22: one-time e-mail tokens (activation / password reset) are stored as
    SHA-256 digests so a database read cannot redeem an outstanding link."""
    return hashlib.sha256((token or "").strip().encode()).hexdigest()


MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES") or 4 * 1024 * 1024)


class RequestBodyLimit:
    """r23: application-level request body cap (413) — defence in depth behind the edge."""

    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_bytes or MAX_REQUEST_BODY_BYTES

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = next((v for k, v in scope.get("headers") or [] if k.lower() == b"content-length"), None)
        try:
            too_big = declared is not None and int(declared) > self.max_bytes
        except ValueError:
            too_big = True
        if too_big:
            return await self._reject(send)
        state = {"seen": 0, "responded": False}

        async def _send(message):
            if not state["responded"]:
                await send(message)

        async def _receive():
            message = await receive()
            if message["type"] == "http.request":
                state["seen"] += len(message.get("body") or b"")
                if state["seen"] > self.max_bytes and not state["responded"]:
                    await self._reject(send)
                    state["responded"] = True
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, _receive, _send)
        except Exception:
            if not state["responded"]:
                raise

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"Request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

