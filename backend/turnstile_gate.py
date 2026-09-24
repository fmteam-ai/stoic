"""Cloudflare Turnstile server-side verification gate (iter-155).

Admin-toggled via db.platform_state {_id:"turnstile"}. When enabled AND
TURNSTILE_SECRET_KEY is configured, login / register / forgot-password
require a valid single-use Turnstile token (field: turnstile_token).

Policy: fail-CLOSED on missing/invalid/expired/duplicate tokens (user-side
failures); fail-OPEN with a logged warning ONLY when Cloudflare's siteverify
infrastructure is unreachable (network error / 5xx) — matches the existing
Audit round 8 P1-1: FAIL-CLOSED. Provider outage, configuration drift and
client token faults are DISTINCT states with distinct audit events.
"""
import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

logger = logging.getLogger("turnstile")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
VERIFY_TIMEOUT_SECONDS = 4.0

# Cloudflare siteverify error codes caused by the CLIENT's token — always
# rejected. Anything else (internal-error, network failure, 5xx) is treated
# as a Cloudflare-side outage → provider_unavailable (fail-closed).
_CLIENT_FAULT_CODES = {
    "missing-input-response", "invalid-input-response",
    "timeout-or-duplicate", "invalid-widget-id", "bad-request",
}

# In-memory ring of the most recent rejections for the /ops/turnstile-diag
# endpoint — lets an admin see the exact Cloudflare error-codes in prod.
_RECENT_REJECTIONS: deque = deque(maxlen=20)


def secret_key() -> str:
    return (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()


def site_key() -> str:
    return (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()


async def is_enabled(db) -> bool:
    doc = await db.platform_state.find_one({"_id": "turnstile"})
    return bool(doc and doc.get("enabled"))


async def set_enabled(db, enabled: bool, actor_email: str = "") -> None:
    await db.platform_state.update_one(
        {"_id": "turnstile"},
        {"$set": {"enabled": bool(enabled),
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "updated_by": actor_email}},
        upsert=True,
    )


def _classify(codes: list, http_status: int | None, transport_error: bool) -> str:
    """Three DISTINCT failure states (audit round 8 P1-1) — never one blob:
    configuration_invalid · provider_unavailable · client_token_invalid."""
    if transport_error or (http_status is not None and http_status >= 500):
        return "provider_unavailable"
    if any(c in ("missing-input-secret", "invalid-input-secret", "bad-request") for c in codes):
        return "configuration_invalid"
    if any(c in _CLIENT_FAULT_CODES for c in codes):
        return "client_token_invalid"
    return "provider_unavailable" if not codes else "configuration_invalid"   # unknown codes = config drift


def expected_hostnames() -> set:
    return {h.strip().lower() for h in os.environ.get("TURNSTILE_EXPECTED_HOSTNAMES", "").split(",") if h.strip()}


async def verify_token(token: str, remote_ip: str | None = None, action: str | None = None) -> dict:
    """Calls Cloudflare siteverify. Returns {ok, state, error_codes, hostname, action}.
    state ∈ ok | client_token_invalid | provider_unavailable | configuration_invalid.
    `outage` is kept as an alias of provider_unavailable for older callers."""
    def _r(ok, state, codes, hostname=None, act=None):
        return {"ok": ok, "state": state, "outage": state == "provider_unavailable",
                "error_codes": codes, "hostname": hostname, "action": act}
    if not token:
        return _r(False, "client_token_invalid", ["missing-input-response"])
    data = {"secret": secret_key(), "response": token, "idempotency_key": str(uuid.uuid4())}
    if remote_ip and remote_ip != "unknown":
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            resp = await client.post(SITEVERIFY_URL, data=data)
    except httpx.HTTPError as e:
        logger.warning("turnstile siteverify unreachable: %s", e)
        return _r(False, "provider_unavailable", ["network-error"])
    if resp.status_code != 200:
        logger.warning("turnstile siteverify HTTP %s", resp.status_code)
        return _r(False, _classify([], resp.status_code, False), [f"http-{resp.status_code}"])
    body = resp.json()
    codes = body.get("error-codes") or []
    hostname = (body.get("hostname") or "").lower() or None
    cf_action = body.get("action") or None
    if body.get("success"):
        # server-side binding of the token to OUR widget/action/hostname
        if action and cf_action and cf_action != action:
            return _r(False, "client_token_invalid", ["action-mismatch"], hostname, cf_action)
        exp = expected_hostnames()
        if exp and hostname and hostname not in exp:
            return _r(False, "client_token_invalid", ["hostname-mismatch"], hostname, cf_action)
        return _r(True, "ok", [], hostname, cf_action)
    state = _classify(codes, None, False)
    if state == "configuration_invalid":
        logger.error("turnstile CONFIGURATION invalid (site/secret pair or widget): %s", codes)
    return _r(False, state, codes, hostname, cf_action)


# per-action degradation policy when the PROVIDER is unavailable.
#   closed  → refuse (registration, password reset — always)
#   login   → env TURNSTILE_LOGIN_DEGRADED_POLICY: closed (default) | otp_required
#             otp_required lets the login proceed ONLY into the email-OTP step-up path
#             (login_otp) — never an unconditional bypass. configuration_invalid is
#             ALWAYS closed: a bad secret is our fault and must page us, not open the door.
def degraded_policy(action: str) -> str:
    if action != "login":
        return "closed"
    pol = os.environ.get("TURNSTILE_LOGIN_DEGRADED_POLICY", "closed").strip().lower()
    return pol if pol in ("closed", "otp_required") else "closed"


def _audit(event: str, **fields) -> None:
    rec = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    _RECENT_REJECTIONS.append(rec)
    logger.warning("turnstile %s %s", event, {k: v for k, v in fields.items() if k != "ip"})


def _break_glass() -> dict | None:
    """Time-limited, audited break-glass. TURNSTILE_BREAK_GLASS_UNTIL=<ISO ts>
    (max 4 h ahead) + TURNSTILE_BREAK_GLASS_REASON. The old unbounded
    TURNSTILE_FORCE_DISABLE is REFUSED in production."""
    until = os.environ.get("TURNSTILE_BREAK_GLASS_UNTIL", "").strip()
    if not until:
        return None
    try:
        exp = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    if exp <= now or (exp - now).total_seconds() > 4 * 3600:
        return None
    return {"until": exp.isoformat(), "reason": os.environ.get("TURNSTILE_BREAK_GLASS_REASON", "") or "unspecified"}


def _fail(code: str, message: str, retryable: bool, state: str, status: int = 403) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "retryable": retryable, "state": state})


async def require_turnstile(db, token: str | None, remote_ip: str | None,
                            action: str = "login") -> None:
    """Gate for sensitive auth actions. FAIL-CLOSED (audit round 8 P1-1):
    provider outage → closed (login may degrade to otp_required by policy);
    configuration invalid → closed + critical audit; client token invalid →
    explicit retryable 403. Break-glass is time-limited and audited."""
    is_prod = os.environ.get("APP_ENV", "").strip().lower() == "production"
    if os.environ.get("TURNSTILE_FORCE_DISABLE", "").strip().lower() == "true":
        if is_prod:
            _audit("force_disable_refused", action=action, ip=remote_ip)
        else:
            logger.warning("turnstile FORCE-DISABLED (non-production env, action=%s)", action)
            return
    bg = _break_glass()
    if bg:
        _audit("break_glass_bypass", action=action, ip=remote_ip, until=bg["until"], reason=bg["reason"])
        return
    if not secret_key():
        if is_prod and await is_enabled(db):
            _audit("configuration_invalid", action=action, ip=remote_ip, error_codes=["secret-not-configured"])
            raise _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Please try again shortly.", True, "configuration_invalid", 503)
        return
    if not await is_enabled(db):
        return
    result = await verify_token((token or "").strip(), remote_ip, action=action)
    if result["ok"]:
        return
    state = result["state"]
    _audit(state, action=action, ip=remote_ip, error_codes=result["error_codes"],
           hostname=result.get("hostname"), cf_action=result.get("action"))
    if state == "provider_unavailable":
        pol = degraded_policy(action)
        if pol == "otp_required":
            raise _fail("turnstile_degraded_otp_required",
                        "Human verification is unavailable; sign in with the emailed one-time code instead.", True, state, 503)
        raise _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Please try again in a moment.", True, state, 503)
    if state == "configuration_invalid":
        raise _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Our team has been alerted.", True, state, 503)
    raise _fail("turnstile_required", "Human verification failed. Please complete the challenge and try again.", True, state, 403)


async def diagnose(db) -> dict:
    """Admin diagnostics: is the secret key valid, and why were recent
    tokens rejected? Probes siteverify with a dummy token — Cloudflare
    answers invalid-input-response when the SECRET is fine, or
    invalid-input-secret when the secret itself is wrong/mismatched."""
    sk = secret_key()
    out = {
        "enabled": await is_enabled(db),
        "force_disabled": os.environ.get(
            "TURNSTILE_FORCE_DISABLE", "").strip().lower() == "true",
        "site_key_set": bool(site_key()),
        "site_key_prefix": site_key()[:14] if site_key() else None,
        "secret_key_set": bool(sk),
        "recent_rejections": list(_RECENT_REJECTIONS),
    }
    if not sk:
        out["secret_check"] = "not_configured"
        return out
    probe = await verify_token("diagnostic-probe-token")
    codes = probe["error_codes"]
    out["probe_error_codes"] = codes
    if any(c in ("missing-input-secret", "invalid-input-secret") for c in codes):
        out["secret_check"] = "INVALID_SECRET"
        out["hint"] = ("TURNSTILE_SECRET_KEY is wrong or belongs to a "
                       "different widget than TURNSTILE_SITE_KEY. Copy both "
                       "keys from the SAME widget in the Cloudflare "
                       "Turnstile dashboard.")
    elif probe["outage"]:
        out["secret_check"] = "cloudflare_unreachable"
    else:
        out["secret_check"] = "secret_ok"
    out["state"] = probe["state"]
    out["expected_hostnames"] = sorted(expected_hostnames())
    out["login_degraded_policy"] = degraded_policy("login")
    out["break_glass"] = _break_glass()
    out["build_sha"] = os.environ.get("GIT_SHA") or None
    return out
