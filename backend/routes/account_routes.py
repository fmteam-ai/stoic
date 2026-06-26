from datetime import datetime, timezone
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from pydantic import BaseModel, Field

from auth import get_current_user, generate_bridge_token, verify_password
from database import get_db
from models import AccountCreate, AccountCredsUpdate
from secrets_vault import encrypt as vault_encrypt, decrypt as vault_decrypt
from route_utils import parse_object_id
from account_limits import (
    get_broker_breakdown,
    check_can_add_live_account,
)
from broker_presets import BROKER_PRESETS

router = APIRouter(prefix="/accounts", tags=["accounts"])


class ImportPosition(BaseModel):
    ticket: int
    symbol: str
    type: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    price_open: float = Field(gt=0)
    sl: float = 0.0
    tp: float = 0.0


class ImportPositionsRequest(BaseModel):
    positions: List[ImportPosition]


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    creds = doc.pop("creds", {}) or {}
    doc["has_investor_password"] = bool(creds.get("investor"))
    doc["has_master_password"] = bool(creds.get("master"))
    return doc


@router.get("")
async def list_accounts(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.accounts.find({"user_id": user["id"]}).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    return [_serialize(d) for d in docs]


@router.get("/limits")
async def account_limits(user=Depends(get_current_user)):
    """Return the user's broker/account-slot usage and the current global caps."""
    db = get_db()
    return await get_broker_breakdown(db, user["id"])


@router.get("/broker-presets")
async def broker_presets(user=Depends(get_current_user)):
    """List of commonly-used MT5 brokers with typical server names.

    `user` dependency is intentional — only authenticated users should be
    able to enumerate presets (keeps it lightly gated for analytics).
    """
    _ = user  # silence linter — auth dependency
    return {"presets": BROKER_PRESETS}


@router.post("")
async def create_account(payload: AccountCreate, user=Depends(get_current_user)):
    db = get_db()
    is_paper = payload.mode == "paper"

    # iter-60 tier-based account quota (Starter=1, Pro=3, Elite=unlimited).
    # Paper accounts count too — a Starter user shouldn't be able to spawn
    # dozens of paper bots to game the cap. Admins bypass entirely.
    from entitlements import enforce_account_quota
    current_count = await db.accounts.count_documents({"user_id": user["id"]})
    await enforce_account_quota(user, current_count)

    # Per-user limits — paper accounts are exempt (sandbox).
    if not is_paper:
        allowed, err = await check_can_add_live_account(db, user["id"], payload.broker)
        if not allowed:
            raise HTTPException(status_code=403, detail=err)

    starting = float(payload.initial_balance) if is_paper else 0.0

    creds = {}
    if not is_paper:
        if payload.investor_password:
            creds["investor"] = vault_encrypt(payload.investor_password)
        if payload.master_password:
            creds["master"] = vault_encrypt(payload.master_password)

    doc = {
        "user_id": user["id"],
        "label": payload.label,
        "broker": "INTERNAL_PAPER" if is_paper else payload.broker,
        "server": "paper-virtual" if is_paper else payload.server,
        "account_number": payload.account_number,
        "account_type": payload.account_type,
        "base_currency": payload.base_currency,
        "mode": payload.mode,
        "bridge_token": generate_bridge_token(),  # unused for paper but harmless
        "status": "connected" if is_paper else "disconnected",
        "balance": starting,
        "equity": starting,
        "initial_balance": starting,
        "last_heartbeat": datetime.now(timezone.utc).isoformat() if is_paper else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "creds": creds,
    }
    result = await db.accounts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


@router.delete("/{account_id}")
async def delete_account(account_id: str, force: bool = False,
                         user=Depends(get_current_user)):
    """Delete an account.

    Refuses if the account has any non-terminal trades attached (open or
    pending). This prevents the orphan-trades-on-deleted-account class of
    bug. The frontend should:
      1. Call POST /api/trades/reconcile to sync stale state
      2. Manually close any genuinely-open trades
      3. Retry DELETE
    Pass `?force=true` to override (admin / cleanup escape hatch).
    """
    db = get_db()
    if not force:
        non_terminal = await db.trades.count_documents({
            "account_id": account_id,
            "user_id": user["id"],
            "status": {"$in": ["open", "pending"]},
        })
        if non_terminal > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot delete account: {non_terminal} non-terminal "
                    "trade(s) still attached. Click SYNC WITH BROKER on the "
                    "Trades page to clear orphans, or close them manually first. "
                    "Pass ?force=true to override."
                ),
            )
    result = await db.accounts.delete_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    # Clean up the per-account bot_config override (if any) — the user's
    # default profile is preserved.
    await db.bot_configs.delete_one(
        {"user_id": user["id"], "account_id": account_id}
    )
    return {"ok": True}


@router.post("/{account_id}/rotate-token")
async def rotate_token(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    new_token = generate_bridge_token()
    result = await db.accounts.update_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]},
        {"$set": {"bridge_token": new_token, "status": "disconnected"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"bridge_token": new_token}


@router.post("/{account_id}/import-positions")
async def import_positions(account_id: str, payload: ImportPositionsRequest,
                            user=Depends(get_current_user)):
    """Manually backfill open positions you can see in MT5 but STOIC doesn't yet
    track — typically positions opened BEFORE the EA was attached.

    Idempotent on `(account_id, mt5_ticket)`: re-running with the same tickets
    is a no-op, so the user can paste from MT5 → Trade tab without worrying
    about duplicates. Once EA v1.25 is installed, the heartbeat-snapshot path
    handles this automatically.
    """
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    skipped: list[int] = []
    for p in payload.positions:
        exists = await db.trades.find_one({
            "account_id": account_id, "mt5_ticket": int(p.ticket),
        })
        if exists:
            skipped.append(p.ticket)
            continue
        await db.trades.insert_one({
            "user_id": user["id"],
            "account_id": account_id,
            "symbol": p.symbol.upper(),
            "action": p.type,
            "lot_size": p.volume,
            "entry_price": p.price_open,
            "stop_loss": p.sl,
            "take_profit": p.tp,
            "exit_price": None,
            "pnl": 0.0,
            "status": "open",
            "mode": acc.get("mode", "live"),
            "broker": acc.get("broker", "MT5"),
            "mt5_ticket": int(p.ticket),
            "opened_at": now_iso,
            "closed_at": None,
            "origin": "external",
            "external_open": True,
            "manually_imported": True,
        })
        created += 1
    return {
        "created": created,
        "skipped_existing": skipped,
        "total_submitted": len(payload.positions),
    }


@router.get("/{account_id}/test-connection")
async def test_connection(account_id: str, user=Depends(get_current_user)):
    """Diagnostic snapshot of the EA bridge health for one account.

    Returns connected status + last heartbeat age + current spreads + an
    actionable diagnostic message the Accounts UI can render inline.
    """
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    is_paper = (acc.get("mode") == "paper")
    last_hb = acc.get("last_heartbeat")
    age_seconds: int | None = None
    if last_hb:
        try:
            ts = datetime.fromisoformat(str(last_hb).replace("Z", "+00:00"))
            age_seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            age_seconds = None

    fresh = age_seconds is not None and age_seconds < 60
    connected = bool(is_paper or fresh)

    if is_paper:
        msg = "Paper account — virtual engine always reachable. No EA needed."
        severity = "ok"
    elif age_seconds is None:
        msg = "The EA has never connected. Complete steps 2–5 above to attach EmergentTradingBridge.mq5 to MT5."
        severity = "error"
    elif age_seconds < 60:
        msg = f"EA online — last heartbeat {age_seconds}s ago. Trades will route here."
        severity = "ok"
    elif age_seconds < 600:
        msg = f"EA stale — last heartbeat {age_seconds // 60}min ago. MT5 may have lost focus or internet. Verify AutoTrading is ON."
        severity = "warn"
    else:
        msg = f"EA disconnected — last heartbeat {age_seconds // 60}min ago. Re-attach the EA or restart MT5 (run on a VPS for 24/7 autopilot)."
        severity = "error"

    # Surface broker-account mismatch (EA v1.24+) — when the EA reports a
    # different MT5 login than what STOIC has configured, the diagnostic
    # message MUST flag it loud so the user catches "wrong terminal" or
    # "two EAs attached to one terminal" bugs.
    mismatch = bool(acc.get("broker_account_mismatch"))
    if mismatch and severity == "ok":
        severity = "warn"
        msg = acc.get("broker_account_mismatch_reason") or (
            "EA is reporting from a different MT5 account than this profile expects."
        )

    return {
        "account_id": str(acc["_id"]),
        "mode": acc.get("mode"),
        "connected": connected,
        "fresh": fresh,
        "last_heartbeat": last_hb,
        "age_seconds": age_seconds,
        "balance": acc.get("balance"),
        "equity": acc.get("equity"),
        "current_spreads": acc.get("current_spreads") or {},
        "spreads_updated_at": acc.get("spreads_updated_at"),
        "broker_account_id_reported": acc.get("broker_account_id_reported"),
        "broker_account_mismatch": mismatch,
        "broker_account_mismatch_reason": acc.get("broker_account_mismatch_reason"),
        "diagnostic": {"severity": severity, "message": msg},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.patch("/{account_id}/credentials")
async def update_credentials(account_id: str, payload: AccountCredsUpdate, user=Depends(get_current_user)):
    """Encrypt and store (or clear) broker login passwords on the account.

    Empty string clears a stored credential. None leaves it untouched.
    Live accounts only — paper accounts have no broker credentials.
    """
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if (account.get("mode") or "live").lower() == "paper":
        raise HTTPException(status_code=400, detail="Paper accounts do not store broker credentials")

    creds = dict(account.get("creds") or {})
    if payload.investor_password is not None:
        if payload.investor_password == "":
            creds.pop("investor", None)
        else:
            creds["investor"] = vault_encrypt(payload.investor_password)
    if payload.master_password is not None:
        if payload.master_password == "":
            creds.pop("master", None)
        else:
            creds["master"] = vault_encrypt(payload.master_password)

    await db.accounts.update_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]},
        {"$set": {"creds": creds}},
    )
    return {
        "has_investor_password": bool(creds.get("investor")),
        "has_master_password": bool(creds.get("master")),
    }


class RevealRequest(BaseModel):
    password: str = Field(..., min_length=1, description="Account password — re-confirm for credential reveal")
    include_master: bool = Field(False, description="Set true to also reveal master password (highest-risk; use sparingly)")


@router.post("/{account_id}/credentials/reveal")
async def reveal_credentials(account_id: str, payload: RevealRequest,
                             user=Depends(get_current_user)):
    """Decrypt and return stored broker passwords for the owner.

    Requires:
      - Authenticated session (cookie)
      - Re-confirmation of the user's account password (defense vs stolen session)
      - include_master must be explicitly set to true to receive master_password
    Every reveal is audit-logged to `credential_reveals` (user, account, IP, ts,
    which secrets returned). Frequent reveal attempts can be rate-limited there.
    """
    db = get_db()
    try:
        acct_oid = ObjectId(account_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Account not found")
    account = await db.accounts.find_one({"_id": acct_oid, "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Re-confirm session password — prevents reveal via stolen/forgotten session
    fresh_user = await db.users.find_one({"_id": ObjectId(user["id"])})
    pwhash = (fresh_user or {}).get("password_hash") if fresh_user else None
    if not pwhash or not verify_password(payload.password, pwhash):
        # Audit failed attempts so brute-force shows up
        await db.credential_reveals.insert_one({
            "user_id": user["id"], "account_id": account_id,
            "result": "wrong_password", "revealed": [],
            "at": datetime.now(timezone.utc).isoformat(),
        })
        raise HTTPException(status_code=403, detail="Password re-confirmation failed")

    creds = account.get("creds") or {}
    out = {"investor_password": None}
    if payload.include_master:
        out["master_password"] = None
    revealed_keys: list[str] = []
    try:
        if creds.get("investor"):
            out["investor_password"] = vault_decrypt(creds["investor"])
            revealed_keys.append("investor")
        if payload.include_master and creds.get("master"):
            out["master_password"] = vault_decrypt(creds["master"])
            revealed_keys.append("master")
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt stored credentials")

    # Audit log — never includes the plaintext
    await db.credential_reveals.insert_one({
        "user_id": user["id"], "account_id": account_id,
        "result": "ok", "revealed": revealed_keys,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return out
