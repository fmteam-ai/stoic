from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user, generate_bridge_token
from database import get_db
from models import AccountCreate, AccountCredsUpdate
from secrets_vault import encrypt as vault_encrypt, decrypt as vault_decrypt

router = APIRouter(prefix="/accounts", tags=["accounts"])


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


@router.post("")
async def create_account(payload: AccountCreate, user=Depends(get_current_user)):
    db = get_db()
    is_paper = payload.mode == "paper"
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
async def delete_account(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    result = await db.accounts.delete_one(
        {"_id": ObjectId(account_id), "user_id": user["id"]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True}


@router.post("/{account_id}/rotate-token")
async def rotate_token(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    new_token = generate_bridge_token()
    result = await db.accounts.update_one(
        {"_id": ObjectId(account_id), "user_id": user["id"]},
        {"$set": {"bridge_token": new_token, "status": "disconnected"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"bridge_token": new_token}


@router.patch("/{account_id}/credentials")
async def update_credentials(account_id: str, payload: AccountCredsUpdate, user=Depends(get_current_user)):
    """Encrypt and store (or clear) broker login passwords on the account.

    Empty string clears a stored credential. None leaves it untouched.
    Live accounts only — paper accounts have no broker credentials.
    """
    db = get_db()
    account = await db.accounts.find_one({"_id": ObjectId(account_id), "user_id": user["id"]})
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
        {"_id": ObjectId(account_id), "user_id": user["id"]},
        {"$set": {"creds": creds}},
    )
    return {
        "has_investor_password": bool(creds.get("investor")),
        "has_master_password": bool(creds.get("master")),
    }


@router.post("/{account_id}/credentials/reveal")
async def reveal_credentials(account_id: str, user=Depends(get_current_user)):
    """Decrypt and return stored broker passwords for the owner.

    Requires an authenticated session. The plaintext is returned ONCE and is never
    logged. Use the returned values immediately — there is no caching.
    """
    db = get_db()
    account = await db.accounts.find_one({"_id": ObjectId(account_id), "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    creds = account.get("creds") or {}
    out = {"investor_password": None, "master_password": None}
    try:
        if creds.get("investor"):
            out["investor_password"] = vault_decrypt(creds["investor"])
        if creds.get("master"):
            out["master_password"] = vault_decrypt(creds["master"])
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt stored credentials")
    return out
