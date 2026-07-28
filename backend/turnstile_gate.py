"""Cloudflare Turnstile server-side verification gate (iter-155).

Admin-toggled via db.platform_state {_id:"turnstile"}. When enabled AND
TURNSTILE_SECRET_KEY is configured, login / register / forgot-password
require a valid single-use Turnstile token (field: turnstile_token).

Policy: fail-CLOSED on missing/invalid/expired/duplicate tokens (user-side
failures); fail-OPEN with a logged warning ONLY when Cloudflare's siteverify
infrastructure is unreachable (network error / 5xx) — matches the existing
HIBP fail-open precedent so a Cloudflare outage can never lock users out.
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
# as a Cloudflare-side outage → fail-open.
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


async def verify_token(token: str, remote_ip: str | None = None) -> dict:
    """Calls Cloudflare siteverify. Returns
    {ok, outage, error_codes} — outage=True means Cloudflare unreachable."""
    if not token:
        return {"ok": False, "outage": False,
                "error_codes": ["missing-input-response"]}
    data = {"secret": secret_key(), "response": token,
            "idempotency_key": str(uuid.uuid4())}
    if remote_ip and remote_ip != "unknown":
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            resp = await client.post(SITEVERIFY_URL, data=data)
    except httpx.HTTPError as e:
        logger.warning("turnstile siteverify unreachable: %s", e)
        return {"ok": False, "outage": True, "error_codes": ["network-error"]}
    if resp.status_code != 200:
        logger.warning("turnstile siteverify HTTP %s", resp.status_code)
        return {"ok": False, "outage": True,
                "error_codes": [f"http-{resp.status_code}"]}
    body = resp.json()
    codes = body.get("error-codes") or []
    if body.get("success"):
        return {"ok": True, "outage": False, "error_codes": []}
    # secret misconfiguration is OUR fault — log loudly, fail-open so a bad
    # key rotation can't lock every user out; ops alerting picks up the log.
    if any(c in ("missing-input-secret", "invalid-input-secret") for c in codes):
        logger.error("turnstile SECRET KEY misconfigured: %s", codes)
        return {"ok": False, "outage": True, "error_codes": codes}
    outage = not any(c in _CLIENT_FAULT_CODES for c in codes)
    return {"ok": False, "outage": outage, "error_codes": codes}


async def require_turnstile(db, token: str | None, remote_ip: str | None,
                            action: str = "login") -> None:
    """Gate for sensitive auth actions. No-op when disabled or unconfigured."""
    if not secret_key():
        return
    if not await is_enabled(db):
        return
    result = await verify_token((token or "").strip(), remote_ip)
    if result["ok"]:
        return
    if result["outage"]:
        logger.warning("turnstile outage — failing OPEN for %s (%s)",
                       action, result["error_codes"])
        return
    logger.warning("turnstile REJECTED %s: error-codes=%s ip=%s",
                   action, result["error_codes"], remote_ip)
    _RECENT_REJECTIONS.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "error_codes": result["error_codes"],
        "ip": remote_ip,
    })
    raise HTTPException(
        status_code=403,
        detail={
            "code": "turnstile_required",
            "message": "Human verification failed. Please complete the "
                       "challenge and try again.",
        },
    )


async def diagnose(db) -> dict:
    """Admin diagnostics: is the secret key valid, and why were recent
    tokens rejected? Probes siteverify with a dummy token — Cloudflare
    answers invalid-input-response when the SECRET is fine, or
    invalid-input-secret when the secret itself is wrong/mismatched."""
    sk = secret_key()
    out = {
        "enabled": await is_enabled(db),
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
    return out
