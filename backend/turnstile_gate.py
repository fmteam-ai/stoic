"""Cloudflare Turnstile server-side gate — FAIL-CLOSED. Single source of truth
for the state machine documented in docs/TURNSTILE.md (audit round 9).

Public state (GET /api/auth/turnstile-config):
    disabled | ready | misconfigured | provider_degraded | break_glass
Verify outcome (per request):
    ok | client_token_invalid | provider_unavailable | configuration_invalid
Gate decision modes returned to handlers:
    verified | policy_disabled | force_disabled | break_glass | degraded_otp_required | deny

Rules: a missing/mismatched action or hostname claim is a client-token fault;
configuration_invalid is always closed; provider_unavailable is closed except
login with TURNSTILE_LOGIN_DEGRADED_POLICY=otp_required, which hands the login
handler a typed decision so it can authenticate the password and then demand a
bound one-time code (degraded_login_otp). There is no fail-open path.
"""
import hashlib
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import HTTPException

logger = logging.getLogger("turnstile")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
VERIFY_TIMEOUT_SECONDS = 4.0
PROVIDER_DEGRADED_WINDOW_SECONDS = 60
CONSUMED_TOKEN_TTL_SECONDS = 600
ACTIONS = ("login", "register", "password_reset")
MAX_FUTURE_SKEW_SECONDS = 30

_CLIENT_FAULT_CODES = {
    "missing-input-response", "invalid-input-response",
    "timeout-or-duplicate", "invalid-widget-id", "bad-request",
}
_RECENT_REJECTIONS: deque = deque(maxlen=20)
_LAST_PROVIDER_FAILURE_MONO: float | None = None


def secret_key() -> str:
    return (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()


def site_key() -> str:
    return (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()


def is_production() -> bool:
    from app_env import is_production as _shared
    return _shared()


def max_token_age_seconds() -> int:
    try:
        return max(30, int(os.environ.get("TURNSTILE_MAX_TOKEN_AGE_SECONDS", "300")))
    except ValueError:
        return 300


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
    if transport_error or (http_status is not None and http_status >= 500):
        return "provider_unavailable"
    if any(c in ("missing-input-secret", "invalid-input-secret", "bad-request") for c in codes):
        return "configuration_invalid"
    if any(c in _CLIENT_FAULT_CODES for c in codes):
        return "client_token_invalid"
    return "provider_unavailable" if not codes else "configuration_invalid"


def expected_hostnames() -> set:
    return {h.strip().lower() for h in os.environ.get("TURNSTILE_EXPECTED_HOSTNAMES", "").split(",") if h.strip()}


def _mark_provider_failure() -> None:
    global _LAST_PROVIDER_FAILURE_MONO
    _LAST_PROVIDER_FAILURE_MONO = time.monotonic()


def provider_recently_degraded() -> bool:
    return (_LAST_PROVIDER_FAILURE_MONO is not None
            and time.monotonic() - _LAST_PROVIDER_FAILURE_MONO < PROVIDER_DEGRADED_WINDOW_SECONDS)


def _token_age_seconds(challenge_ts: str | None) -> float | None:
    if not challenge_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(challenge_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if ts.tzinfo is None:                      # naive timestamp → treat as UTC, never TypeError
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def bind_claims(body: dict, action: str | None) -> tuple[bool, list]:
    """Strict claim binding on a successful siteverify body (round 9 P1-02):
    expected action ⇒ exact equality (missing claim fails); non-empty hostname
    allowlist ⇒ non-empty hostname contained in it; freshness ⇒ challenge_ts
    within TURNSTILE_MAX_TOKEN_AGE_SECONDS."""
    codes: list = []
    cf_action = body.get("action") or None
    hostname = (body.get("hostname") or "").lower() or None
    if action:
        if not cf_action:
            codes.append("action-missing")
        elif cf_action != action:
            codes.append("action-mismatch")
    exp = expected_hostnames()
    if exp:
        if not hostname:
            codes.append("hostname-missing")
        elif hostname not in exp:
            codes.append("hostname-mismatch")
    age = _token_age_seconds(body.get("challenge_ts"))
    if age is None:
        codes.append("challenge-ts-missing")             # round 10 P1-04: freshness is mandatory
    elif age > max_token_age_seconds():
        codes.append("token-stale")
    elif age < -MAX_FUTURE_SKEW_SECONDS:
        codes.append("token-from-future")
    return (not codes), codes


async def _consume_token_once(db, token: str, action: str | None) -> bool:
    """Local single-use ledger (defence in depth over Cloudflare's own
    timeout-or-duplicate) — a token verified for one surface can never be
    replayed on another. Stores a hash, never the token."""
    if db is None or not hasattr(db, "turnstile_consumed_tokens"):
        return True
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        await db.turnstile_consumed_tokens.insert_one({
            "_id": digest, "action": action, "at": now.isoformat(),
            "expires_at": now + timedelta(seconds=CONSUMED_TOKEN_TTL_SECONDS)})
        return True
    except Exception as e:  # noqa: BLE001 — DuplicateKeyError or driver error → replay/deny
        logger.warning("turnstile token replay refused (%s)", type(e).__name__)
        return False


async def verify_token(token: str, remote_ip: str | None = None, action: str | None = None) -> dict:
    """Calls Cloudflare siteverify and applies strict claim binding.
    Returns {ok, state, error_codes, hostname, action, challenge_ts}."""
    def _r(ok, state, codes, hostname=None, act=None, ts=None):
        return {"ok": ok, "state": state, "outage": state == "provider_unavailable",
                "error_codes": codes, "hostname": hostname, "action": act, "challenge_ts": ts}
    if not token:
        return _r(False, "client_token_invalid", ["missing-input-response"])
    data = {"secret": secret_key(), "response": token, "idempotency_key": str(uuid.uuid4())}
    if remote_ip and remote_ip != "unknown":
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            resp = await client.post(SITEVERIFY_URL, data=data)
    except httpx.HTTPError as e:
        logger.warning("turnstile siteverify unreachable: %s", type(e).__name__)
        _mark_provider_failure()
        return _r(False, "provider_unavailable", ["network-error"])
    if resp.status_code != 200:
        state = _classify([], resp.status_code, False)
        if state == "provider_unavailable":
            _mark_provider_failure()
        return _r(False, state, [f"http-{resp.status_code}"])
    try:
        body = resp.json()
    except ValueError:
        _mark_provider_failure()
        return _r(False, "provider_unavailable", ["malformed-json"])
    if not isinstance(body, dict):
        _mark_provider_failure()
        return _r(False, "provider_unavailable", ["malformed-body"])
    codes = body.get("error-codes") or []
    if not isinstance(codes, list):
        codes = ["malformed-error-codes"]
    hostname = (body.get("hostname") or "").lower() or None
    cf_action = body.get("action") or None
    ts = body.get("challenge_ts")
    if body.get("success"):
        ok, bind_codes = bind_claims(body, action)
        if not ok:
            return _r(False, "client_token_invalid", bind_codes, hostname, cf_action, ts)
        return _r(True, "ok", [], hostname, cf_action, ts)
    state = _classify(codes, None, False)
    if state == "provider_unavailable":
        _mark_provider_failure()
    if state == "configuration_invalid":
        logger.error("turnstile CONFIGURATION invalid: %s", codes)
    return _r(False, state, codes, hostname, cf_action, ts)


def degraded_policy(action: str) -> str:
    if action != "login":
        return "closed"
    pol = os.environ.get("TURNSTILE_LOGIN_DEGRADED_POLICY", "closed").strip().lower()
    return pol if pol in ("closed", "otp_required") else "closed"


def _audit(event: str, **fields) -> None:
    rec = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    _RECENT_REJECTIONS.append(rec)
    logger.warning("turnstile %s %s", event, {k: v for k, v in fields.items() if k != "ip"})


def _fail(code: str, message: str, retryable: bool, state: str, status: int = 403) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "retryable": retryable, "state": state})


@dataclass
class GateDecision:
    allow: bool
    mode: str                 # verified|policy_disabled|force_disabled|break_glass|degraded_otp_required|deny
    state: str                # ok|disabled|client_token_invalid|provider_unavailable|configuration_invalid
    error: HTTPException | None = None
    error_codes: list = field(default_factory=list)
    reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self.mode == "degraded_otp_required"


def configuration_state() -> str:
    """ready | misconfigured (policy-independent view of the env keys)."""
    return "ready" if (secret_key() and site_key()) else "misconfigured"


async def public_state(db) -> dict:
    """Explicit public state — never a silent enabled=false on error. No secrets."""
    from turnstile_break_glass import active as bg_active
    enabled = await is_enabled(db)
    if not enabled:
        return {"state": "disabled", "code": "policy_disabled", "site_key": None,
                "degraded_login": degraded_policy("login")}
    bg = await bg_active(db)
    if bg:
        return {"state": "break_glass", "code": "break_glass_active",
                "site_key": site_key() if configuration_state() == "ready" else None,
                "scope": bg["scope"], "until": bg["until"], "degraded_login": degraded_policy("login")}
    if configuration_state() == "misconfigured":
        return {"state": "misconfigured", "code": "keys_incomplete", "site_key": None,
                "degraded_login": "closed"}
    if provider_recently_degraded():
        return {"state": "provider_degraded", "code": "provider_unavailable", "site_key": site_key(),
                "degraded_login": degraded_policy("login")}
    return {"state": "ready", "code": "ready", "site_key": site_key(),
            "degraded_login": degraded_policy("login")}


async def evaluate(db, token: str | None, remote_ip: str | None, action: str = "login",
                   request_id: str | None = None) -> GateDecision:
    """Typed gate decision (round 9 P1-03). Never raises; callers either
    `raise decision.error` or branch on `decision.mode`."""
    if action not in ACTIONS:
        return GateDecision(False, "deny", "configuration_invalid",
                            _fail("turnstile_unavailable", "Unknown verification surface.", False, "configuration_invalid", 503))
    if os.environ.get("TURNSTILE_FORCE_DISABLE", "").strip().lower() == "true":
        if is_production():
            _audit("force_disable_refused", action=action, ip=remote_ip)
        else:
            logger.warning("turnstile FORCE-DISABLED (non-production env, action=%s)", action)
            return GateDecision(True, "force_disabled", "disabled")
    if not await is_enabled(db):
        return GateDecision(True, "policy_disabled", "disabled")
    from turnstile_break_glass import active as bg_active, record_bypass
    bg = await bg_active(db)
    if bg and action in bg["scope"]:
        await record_bypass(db, bg, action=action, remote_ip=remote_ip, request_id=request_id)
        _audit("break_glass_bypass", action=action, ip=remote_ip, until=bg["until"], incident_id=bg["incident_id"])
        return GateDecision(True, "break_glass", "disabled", reason=bg["incident_id"])
    if configuration_state() == "misconfigured":
        _audit("configuration_invalid", action=action, ip=remote_ip, error_codes=["keys-incomplete"])
        return GateDecision(False, "deny", "configuration_invalid",
                            _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Our team has been alerted.", True, "configuration_invalid", 503),
                            ["keys-incomplete"])
    if not (token or "").strip() and action == "login" and provider_recently_degraded() \
            and degraded_policy("login") == "otp_required":
        # round 10 P1-05: the SERVER's own recent provider failure (not a client
        # claim) routes a tokenless login into the bound OTP path.
        _audit("provider_unavailable", action=action, ip=remote_ip, error_codes=["tokenless-during-server-degraded"])
        return GateDecision(True, "degraded_otp_required", "provider_unavailable", None,
                            ["tokenless-during-server-degraded"], "provider_unavailable")
    result = await verify_token((token or "").strip(), remote_ip, action=action)
    if result["ok"]:
        if not await _consume_token_once(db, (token or "").strip(), action):
            _audit("client_token_invalid", action=action, ip=remote_ip, error_codes=["token-replayed"])
            return GateDecision(False, "deny", "client_token_invalid",
                                _fail("turnstile_required", "Human verification failed. Please complete the challenge and try again.", True, "client_token_invalid", 403),
                                ["token-replayed"])
        return GateDecision(True, "verified", "ok")
    state = result["state"]
    _audit(state, action=action, ip=remote_ip, error_codes=result["error_codes"],
           hostname=result.get("hostname"), cf_action=result.get("action"))
    if state == "provider_unavailable":
        if degraded_policy(action) == "otp_required":
            return GateDecision(True, "degraded_otp_required", state, None, result["error_codes"], "provider_unavailable")
        return GateDecision(False, "deny", state,
                            _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Please try again in a moment.", True, state, 503),
                            result["error_codes"])
    if state == "configuration_invalid":
        return GateDecision(False, "deny", state,
                            _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Our team has been alerted.", True, state, 503),
                            result["error_codes"])
    return GateDecision(False, "deny", state,
                        _fail("turnstile_required", "Human verification failed. Please complete the challenge and try again.", True, state, 403),
                        result["error_codes"])


async def require_turnstile(db, token: str | None, remote_ip: str | None,
                            action: str = "login") -> None:
    """Raise-or-pass wrapper for surfaces that never degrade (register,
    password_reset). A degraded decision is a DENIAL here."""
    d = await evaluate(db, token, remote_ip, action)
    if d.error is not None:
        raise d.error
    if d.degraded:
        raise _fail("turnstile_unavailable", "Human verification is temporarily unavailable. Please try again in a moment.", True, d.state, 503)


def production_config_violation(policy_enabled: bool) -> str | None:
    """Boot/readiness rule: production + policy enabled ⇒ both keys present AND a
    non-empty hostname allowlist (round 10 P1-04)."""
    if is_production() and policy_enabled:
        if configuration_state() == "misconfigured":
            return ("Turnstile policy is ENABLED but TURNSTILE_SITE_KEY/TURNSTILE_SECRET_KEY "
                    "are incomplete — refusing to serve an auth control that cannot verify.")
        if not expected_hostnames():
            return ("Turnstile policy is ENABLED but TURNSTILE_EXPECTED_HOSTNAMES is empty — "
                    "production requires exact hostname binding.")
    return None


async def diagnose(db) -> dict:
    from turnstile_break_glass import status as bg_status
    sk = secret_key()
    out = {
        "enabled": await is_enabled(db),
        "public_state": await public_state(db),
        "force_disabled": os.environ.get("TURNSTILE_FORCE_DISABLE", "").strip().lower() == "true",
        "site_key_set": bool(site_key()),
        "site_key_prefix": site_key()[:14] if site_key() else None,
        "secret_key_set": bool(sk),
        "recent_rejections": list(_RECENT_REJECTIONS),
        "expected_hostnames": sorted(expected_hostnames()),
        "max_token_age_seconds": max_token_age_seconds(),
        "login_degraded_policy": degraded_policy("login"),
        "break_glass": await bg_status(db),
        "build_sha": os.environ.get("GIT_SHA") or None,
    }
    if not sk:
        out["secret_check"] = "not_configured"
        return out
    probe = await verify_token("diagnostic-probe-token")
    codes = probe["error_codes"]
    out["probe_error_codes"] = codes
    if any(c in ("missing-input-secret", "invalid-input-secret") for c in codes):
        out["secret_check"] = "INVALID_SECRET"
        out["hint"] = ("TURNSTILE_SECRET_KEY is wrong or belongs to a different widget than "
                       "TURNSTILE_SITE_KEY. Copy both keys from the SAME widget.")
    elif probe["outage"]:
        out["secret_check"] = "cloudflare_unreachable"
    else:
        out["secret_check"] = "secret_ok"
    out["state"] = probe["state"]
    return out
