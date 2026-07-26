"""Partner broker recommendations with IB/referral tracking links.

  GET    /api/partners/brokers            — active partner brokers (any user)
  POST   /api/partners/brokers            — create/update (admin)
  DELETE /api/partners/brokers/{bid}      — remove (admin)
  POST   /api/partners/brokers/{bid}/click — track click, return target URL
"""
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/partners", tags=["partners"])

DEFAULT_BROKERS = [
    {
        "name": "Exness",
        "tagline": "Instant withdrawals · raw spreads on XAUUSD",
        "regulation": "FSA · CySEC · FCA",
        "min_deposit": "$10",
        "ib_link": "https://one.exness-track.com/a/PLACEHOLDER",
        "highlight": True,
        "is_placeholder": True,
        "sort": 1,
    },
    {
        "name": "IC Markets",
        "tagline": "True ECN · deep liquidity for indices",
        "regulation": "ASIC · CySEC",
        "min_deposit": "$200",
        "ib_link": "https://icmarkets.com/?camp=PLACEHOLDER",
        "highlight": False,
        "is_placeholder": True,
        "sort": 2,
    },
    {
        "name": "Vantage",
        "tagline": "Fast execution · MT5 native · copy-friendly",
        "regulation": "ASIC · FSCA",
        "min_deposit": "$50",
        "ib_link": "https://vantagemarkets.com/open-live-account/?affid=PLACEHOLDER",
        "highlight": False,
        "is_placeholder": True,
        "sort": 3,
    },
]


class BrokerIn(BaseModel):
    id: str | None = None
    name: str
    tagline: str = ""
    regulation: str = ""
    min_deposit: str = ""
    ib_link: str
    highlight: bool = False
    active: bool = True
    sort: int = 99


def _ser(d: dict) -> dict:
    return {
        "id": str(d["_id"]),
        "name": d.get("name"),
        "tagline": d.get("tagline"),
        "regulation": d.get("regulation"),
        "min_deposit": d.get("min_deposit"),
        "ib_link": d.get("ib_link"),
        "highlight": bool(d.get("highlight")),
        "is_placeholder": bool(d.get("is_placeholder")),
        "clicks": int(d.get("clicks") or 0),
        "sort": int(d.get("sort") or 99),
    }


def _require_admin(user: dict):
    from auth import require_admin
    require_admin(user)


@router.get("/brokers")
async def list_partner_brokers(user=Depends(get_current_user)):
    db = get_db()
    if await db.partner_brokers.count_documents({}) == 0:
        now = datetime.now(timezone.utc).isoformat()
        await db.partner_brokers.insert_many(
            [{**b, "active": True, "clicks": 0, "created_at": now} for b in DEFAULT_BROKERS])
    cur = db.partner_brokers.find({"active": {"$ne": False}}).sort("sort", 1)
    return {"items": [_ser(d) async for d in cur], "is_admin": user.get("role") == "admin"}


@router.post("/brokers")
async def upsert_partner_broker(body: BrokerIn, user=Depends(get_current_user)):
    _require_admin(user)
    db = get_db()
    doc = body.model_dump(exclude={"id"})
    doc["is_placeholder"] = "PLACEHOLDER" in (body.ib_link or "")
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    if body.id:
        res = await db.partner_brokers.update_one(
            {"_id": ObjectId(body.id)}, {"$set": doc})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail="Broker not found")
        saved = await db.partner_brokers.find_one({"_id": ObjectId(body.id)})
    else:
        doc["clicks"] = 0
        ins = await db.partner_brokers.insert_one(doc)
        saved = await db.partner_brokers.find_one({"_id": ins.inserted_id})
    return _ser(saved)


@router.delete("/brokers/{bid}")
async def delete_partner_broker(bid: str, user=Depends(get_current_user)):
    _require_admin(user)
    res = await get_db().partner_brokers.delete_one({"_id": ObjectId(bid)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Broker not found")
    return {"ok": True}


@router.post("/brokers/{bid}/click")
async def track_broker_click(bid: str, user=Depends(get_current_user)):
    db = get_db()
    doc = await db.partner_brokers.find_one({"_id": ObjectId(bid)})
    if not doc:
        raise HTTPException(status_code=404, detail="Broker not found")
    await db.partner_brokers.update_one({"_id": doc["_id"]}, {"$inc": {"clicks": 1}})
    await db.affiliate_clicks.insert_one({
        "type": "partner_broker", "broker": doc.get("name"),
        "user_id": user["id"],
        "clicked_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"url": doc.get("ib_link")}
