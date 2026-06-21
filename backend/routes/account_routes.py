from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user, generate_bridge_token
from database import get_db
from models import AccountCreate

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
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
    doc = {
        "user_id": user["id"],
        "label": payload.label,
        "broker": payload.broker,
        "server": payload.server,
        "account_number": payload.account_number,
        "account_type": payload.account_type,
        "base_currency": payload.base_currency,
        "bridge_token": generate_bridge_token(),
        "status": "disconnected",
        "balance": 0.0,
        "equity": 0.0,
        "last_heartbeat": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
