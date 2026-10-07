"""iter-84 · Pairing-token flow for the PowerShell auto-installer.

End-to-end:

    User adds account in STOIC dashboard
            │
            ▼
    POST /api/setup/pairing-token  {account_id}
            │
            ▼
    Backend stores: {token, account_id, user_id, expires_at}  (60 min TTL)
            │
            ▼
    UI displays token + one-liner install command
            │
            ▼
    User runs PowerShell installer on MT5 host (PC or VPS)
            │
            ▼
    Installer → POST /api/setup/claim-pairing  {token, hostname}
            │
            ▼
    Backend validates + consumes token, returns:
       { bridge_token, server_url, ea_script_url, account_label }
            │
            ▼
    Installer writes bridge_token → MQL5/Files/STOIC-Token.txt,
    copies EA to MQL5/Experts/, compiles, whitelists URL.

Security model:
  · Pairing-token is single-use and expires in 60 min.
  · `claim-pairing` is UNAUTH'd by design (the installer runs before the
    user has any cookies on the VPS). The token IS the auth.
  · The token NEVER leaves the user's machine after they paste it into the
    PowerShell prompt — it's redeemed once, immediately consumed.
  · An attacker who intercepts the token has at most 60 min to use it AND
    only gets a bridge_token bound to ONE account (which the user can
    rotate from the dashboard the moment they suspect compromise).
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone, timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id
from security import client_ip, rate_limit

router = APIRouter(tags=["setup"])
PAIRING_PREV_TOKEN_GRACE_H = 24   # R-3 — previous EA token survives a pairing until first new heartbeat, max 24 h


PAIRING_TTL_MINUTES = 60   # Easy-Connect P1.3 — one hour (still single-use); 15 min expired while users were still in RDP


# ─────────────── Models ───────────────
class PairingTokenRequest(BaseModel):
    account_id: str = Field(min_length=1)


class DeviceKey(BaseModel):
    algorithm: str = Field(pattern=r"^(RSA-PSS-SHA256|Ed25519)$")
    public_key: str = Field(min_length=32, max_length=8000)


class ClaimPairingRequest(BaseModel):
    token: str = Field(min_length=10, max_length=120)
    hostname: str | None = Field(default=None, max_length=200)
    installer_version: str | None = Field(default=None, max_length=40)
    # r26 P1-02 — the installer enrols its device PUBLIC key while redeeming the
    # operator-issued one-time pairing token; only that key can later attest the EX5.
    device_key: DeviceKey | None = None


# ─────────────── Issue a pairing token (auth'd) ───────────────
@router.post("/setup/pairing-token")
async def issue_pairing_token(
    payload: PairingTokenRequest, user=Depends(get_current_user)
):
    """Issue a single-use 15-min pairing token for a specific account.

    The user must own the account. Re-issuing for the same account
    invalidates any previous token (only one outstanding token per account
    — paste the latest into PowerShell, no stale-token confusion).
    """
    db = get_db()
    oid = parse_object_id(payload.account_id, "Account")
    account = await db.accounts.find_one({"_id": oid})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Account not yours")
    if account.get("mode") == "paper":
        raise HTTPException(
            status_code=400,
            detail={"code": "paper_account",
                    "message": "Paper accounts don't need an MT5 installer."},
        )

    token = secrets.token_urlsafe(24)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=PAIRING_TTL_MINUTES)
    ).isoformat()

    # Invalidate any prior outstanding token for this account.
    await db.pairing_tokens.delete_many({"account_id": payload.account_id,
                                         "consumed_at": {"$exists": False}})
    await db.pairing_tokens.insert_one({
        "token": token,
        "account_id": payload.account_id,
        "user_id": user["id"],
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    })

    return {
        "token": token,
        "expires_at": expires_at,
        "ttl_minutes": PAIRING_TTL_MINUTES,
        "account_label": account.get("label"),
        "broker": account.get("broker"),
    }


# ─────────────── Claim a pairing token (UNauth'd) ───────────────
@router.post("/setup/claim-pairing")
async def claim_pairing_token(payload: ClaimPairingRequest, request: Request):
    """Called by the PowerShell installer on the MT5 host.

    Validates + consumes the token, returns everything the installer needs
    to wire up the EA. Intentionally NOT authenticated (the installer has
    no cookies / no STOIC login on the VPS) — the token IS the proof.
    """
    db = get_db()
    await rate_limit(db, "claim_pairing", client_ip(request), 20, 600,
                     "Too many pairing attempts from this address", request=request)
    pairing = await db.pairing_tokens.find_one({"token": payload.token})
    if not pairing:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_token",
                    "message": "Pairing token is invalid or already used."},
        )
    if pairing.get("consumed_at"):
        raise HTTPException(
            status_code=400,
            detail={"code": "already_used",
                    "message": "This pairing token was already redeemed."},
        )

    # Expiry
    try:
        exp_dt = datetime.fromisoformat(
            pairing["expires_at"].replace("Z", "+00:00")
        )
    except Exception:
        exp_dt = None
    if exp_dt and exp_dt < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail={"code": "expired_token",
                    "message": "Pairing token expired. Generate a fresh one from the STOIC dashboard."},
        )

    account = await db.accounts.find_one({"_id": ObjectId(pairing["account_id"])})
    if not account:
        raise HTTPException(status_code=410, detail="Linked account no longer exists")

    # Consume — single-use, ATOMIC. The conditional update on consumed_at=None
    # closes the check-then-act TOCTOU window so two concurrent installers can
    # never both redeem the same token.
    client_host = request.client.host if request.client else None
    claimed = await db.pairing_tokens.find_one_and_update(
        {"_id": pairing["_id"],
         "$or": [{"consumed_at": None}, {"consumed_at": {"$exists": False}}]},
        {"$set": {
            "consumed_at": datetime.now(timezone.utc).isoformat(),
            "consumed_by_hostname": payload.hostname,
            "consumed_by_ip": client_host,
            "installer_version": payload.installer_version,
        }},
    )
    if not claimed:
        raise HTTPException(
            status_code=400,
            detail={"code": "already_used",
                    "message": "This pairing token was already redeemed."},
        )

    # Also mark on the account so the user sees "installer paired at X" in UI.
    await db.accounts.update_one(
        {"_id": account["_id"]},
        {"$set": {
            "installer_paired_at": datetime.now(timezone.utc).isoformat(),
            "installer_paired_hostname": payload.hostname,
            "installer_version": payload.installer_version,
        }},
    )

    # iter-125 correction #1 — the installer flow registers a VERIFIED
    # installation identity too (one account → one installation → one
    # terminal → one execution lease). EA v1.55 sends this id on every
    # heartbeat; without it, heartbeats are telemetry-only.
    import uuid as _uuid
    now = datetime.now(timezone.utc)
    installation_id = f"inst_{_uuid.uuid4().hex[:12]}"
    account_id_str = str(account["_id"])
    device_key = None
    if payload.device_key is not None:
        import device_attestation as da
        try:
            device_key = da.device_key_record(payload.device_key.algorithm, payload.device_key.public_key)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail={"code": "invalid_device_key",
                                                         "message": f"device public key rejected: {e}"})
    # Q-2 — superseded installations are revoked ONLY when the new one heartbeats (see below);
    # stale pending registrations from earlier installer runs are cleared here.
    # Q-2 — a stale pending registration means the CURRENT token was never used by any EA:
    # the running terminal's token (grace slot) must survive this re-run (keep_prev_grace).
    _stale_pending = await db.installations.count_documents(
        {"account_id": account_id_str, "revoked": {"$ne": True}, "pending_first_heartbeat": True}) > 0
    await db.installations.update_many(
        {"account_id": account_id_str, "revoked": {"$ne": True}, "pending_first_heartbeat": True},
        {"$set": {"revoked": True,
                  "revoked_reason": "superseded by newer installer pairing (never heartbeated)",
                  "revoked_at": now}})
    # Q-2 — the RUNNING installation stays authoritative: the new one is registered as
    # pending and only its first heartbeat (bridge auth, first use of the new token) revokes
    # the others (bridge_tokens.promote_installation). The lease is NOT handed over here.
    await db.installations.insert_one({
        "installation_id": installation_id,
        "user_id": account.get("user_id"),
        "account_id": account_id_str,
        "terminal_path": "installer",
        "host_fingerprint": payload.hostname or "unknown-host",
        "device_key": device_key,
        "pending_first_heartbeat": True,
        "revoked": False, "created_at": now})
    from vps_agent import LEASE_SECONDS
    _has_live = await db.installations.count_documents(
        {"account_id": account_id_str, "revoked": {"$ne": True}, "pending_first_heartbeat": {"$ne": True}}) > 0
    if not _has_live:
        await db.execution_leases.update_one(
            {"account_id": account_id_str},
            {"$set": {"installation_id": installation_id,
                      "user_id": account.get("user_id"), "revoked": False,
                      "broker_server": account.get("server"),
                      "account_number": account.get("account_number"),
                      "acquired_at": now,
                      "expires_at": now + timedelta(seconds=LEASE_SECONDS)}},
            upsert=True)

    backend_base = os.environ.get(
        "PUBLIC_BACKEND_URL"
    ) or os.environ.get("REACT_APP_BACKEND_URL")
    if not backend_base:
        # Best effort — derive from request URL. The installer needs an
        # absolute URL since it isn't running behind our ingress.
        backend_base = str(request.base_url).rstrip("/")
    backend_base = backend_base.rstrip("/")

    # P1-01 — the account's token is not readable any more (hash only): pairing ISSUES a fresh
    # token and hands it to the installer exactly once (one account → one installation).
    # R-3 — the PREVIOUS token stays valid until the new installation's first heartbeat
    # retires it (bridge auth, S2), hard cap 24 h: a running EA is never silently cut off
    # by an installer re-run that never completes.
    import bridge_tokens as _bt
    from auth import generate_bridge_token as _gen
    fresh_token = _gen()
    _upd = _bt.rotation_update(account, fresh_token,
                               grace_until=(datetime.now(timezone.utc) + timedelta(hours=PAIRING_PREV_TOKEN_GRACE_H)).isoformat(),
                               suspended=bool(account.get("bridge_token_suspended")),
                               keep_prev_grace=_stale_pending)
    _upd["$set"]["installation_pending"] = True          # Q-2 — promoted on first heartbeat even without a grace slot
    await db.accounts.update_one({"_id": account["_id"]}, _upd)
    return {
        "bridge_token": fresh_token,
        "installation_id": installation_id,
        "account_label": account.get("label"),
        "broker": account.get("broker"),
        "account_number": account.get("account_number"),
        "server_url": backend_base,
        "heartbeat_url": f"{backend_base}/api/bridge/heartbeat",
        "ea_script_url": f"{backend_base}/api/ea-script",
        "ea_latest_version": "1.61",
        # r26 P1-02 — device key enrolled with THIS pairing (None when the installer sent none)
        "device_key_id": (device_key or {}).get("key_id"),
        "attestation_challenge_url": f"{backend_base}/api/infra/attestation/challenge",
        "attestation_verify_url": f"{backend_base}/api/infra/attestation/verify",
    }


# ─────────────── Lookup outstanding token (for dashboard polling) ───────────────
@router.get("/setup/pairing-status/{account_id}")
async def pairing_status(account_id: str, user=Depends(get_current_user)):
    """Lets the dashboard show 'paired ✓' once the installer redeems the
    token. Frontend polls this after generating a token."""
    db = get_db()
    oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": oid})
    if not account or account.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Account not found")
    pairing = await db.pairing_tokens.find_one(
        {"account_id": account_id},
        sort=[("issued_at", -1)],
    )
    return {
        "installer_paired_at": account.get("installer_paired_at"),
        "installer_paired_hostname": account.get("installer_paired_hostname"),
        "installer_version": account.get("installer_version"),
        "token_outstanding": bool(pairing and not pairing.get("consumed_at")),
        "token_consumed_at": pairing.get("consumed_at") if pairing else None,
    }



# ─────────────── Installer progress (Accounts page panel) ───────────────
@router.get("/setup/install-progress/{account_id}")
async def install_progress(account_id: str, request: Request, user=Depends(get_current_user)):
    """Five derived steps (token → installer → .ex5 → heartbeat/WebRequest → identity) so a
    stuck VPS pairing is visible on the Accounts page. Read-only; grants nothing."""
    db = get_db()
    # polled every 5 s by the panel (15 s by the chip) — 240/min per user is ~10x headroom, still bounded
    await rate_limit(db, "install_progress", f"user:{user['id']}", 240, 60, "Too many install-progress polls", request=request)
    oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": oid})
    if not account or (account.get("user_id") != user["id"] and user.get("role") != "admin"):
        raise HTTPException(status_code=404, detail="Account not found")
    pairing = await db.pairing_tokens.find_one({"account_id": account_id}, sort=[("issued_at", -1)])
    installation = await db.installations.find_one(
        {"account_id": account_id, "revoked": {"$ne": True}}, sort=[("created_at", -1)],
        projection={"installation_id": 1, "host_fingerprint": 1, "ex5_sha256": 1, "ex5_measured_by": 1,
                    "attestation": 1, "device_key": 1, "pending_first_heartbeat": 1, "created_at": 1})
    import device_attestation as da
    import install_progress as ip
    from ea_capabilities import accepted_ea_sha256s
    return ip.derive(account, pairing, installation, attested=da.attested_hash(installation),
                     accepted_hashes=accepted_ea_sha256s(), request_base=str(request.base_url),
                     forwarded_proto=request.headers.get("x-forwarded-proto"),
                     forwarded_host=request.headers.get("x-forwarded-host") or request.headers.get("host"))
