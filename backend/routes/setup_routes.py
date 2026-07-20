"""iter-84 · Pairing-token flow for the PowerShell auto-installer.

End-to-end:

    User adds account in STOIC dashboard
            │
            ▼
    POST /api/setup/pairing-token  {account_id}
            │
            ▼
    Backend stores: {token, account_id, user_id, expires_at}  (15 min TTL)
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
  · Pairing-token is single-use and expires in 15 min.
  · `claim-pairing` is UNAUTH'd by design (the installer runs before the
    user has any cookies on the VPS). The token IS the auth.
  · The token NEVER leaves the user's machine after they paste it into the
    PowerShell prompt — it's redeemed once, immediately consumed.
  · An attacker who intercepts the token has at most 15 min to use it AND
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

router = APIRouter(tags=["setup"])


PAIRING_TTL_MINUTES = 15


# ─────────────── Models ───────────────
class PairingTokenRequest(BaseModel):
    account_id: str = Field(min_length=1)


class ClaimPairingRequest(BaseModel):
    token: str = Field(min_length=10, max_length=120)
    hostname: str | None = Field(default=None, max_length=200)
    installer_version: str | None = Field(default=None, max_length=40)


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

    # Consume — single-use.
    client_host = request.client.host if request.client else None
    await db.pairing_tokens.update_one(
        {"_id": pairing["_id"]},
        {"$set": {
            "consumed_at": datetime.now(timezone.utc).isoformat(),
            "consumed_by_hostname": payload.hostname,
            "consumed_by_ip": client_host,
            "installer_version": payload.installer_version,
        }},
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

    backend_base = os.environ.get(
        "PUBLIC_BACKEND_URL"
    ) or os.environ.get("REACT_APP_BACKEND_URL")
    if not backend_base:
        # Best effort — derive from request URL. The installer needs an
        # absolute URL since it isn't running behind our ingress.
        backend_base = str(request.base_url).rstrip("/")
    backend_base = backend_base.rstrip("/")

    return {
        "bridge_token": account.get("bridge_token"),
        "account_label": account.get("label"),
        "broker": account.get("broker"),
        "account_number": account.get("account_number"),
        "server_url": backend_base,
        "heartbeat_url": f"{backend_base}/api/bridge/heartbeat",
        "ea_script_url": f"{backend_base}/api/ea-script",
        "ea_latest_version": "1.48",
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
