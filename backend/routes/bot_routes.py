from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from models import BotConfigUpdate, BotConfigOut

router = APIRouter(prefix="/bot", tags=["bot"])


async def _get_or_create_config(db, user_id: str) -> dict:
    cfg = await db.bot_configs.find_one({"user_id": user_id})
    if cfg:
        return cfg
    new_cfg = {
        "user_id": user_id,
        "risk_level": "medium",
        "symbols": ["XAUUSD", "BTCUSD"],
        "active": False,
        "max_concurrent_trades": 3,
        "auto_execute": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.bot_configs.insert_one(new_cfg)
    new_cfg["_id"] = result.inserted_id
    return new_cfg


def _serialize(cfg: dict) -> dict:
    return {
        "id": str(cfg["_id"]),
        "user_id": cfg["user_id"],
        "risk_level": cfg.get("risk_level", "medium"),
        "symbols": cfg.get("symbols", []),
        "active": cfg.get("active", False),
        "max_concurrent_trades": cfg.get("max_concurrent_trades", 3),
        "auto_execute": cfg.get("auto_execute", True),
        "updated_at": cfg.get("updated_at"),
    }


@router.get("/config")
async def get_config(user=Depends(get_current_user)):
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"])
    return _serialize(cfg)


@router.put("/config")
async def update_config(payload: BotConfigUpdate, user=Depends(get_current_user)):
    db = get_db()
    update = {
        "risk_level": payload.risk_level,
        "symbols": [s.upper() for s in payload.symbols],
        "active": payload.active,
        "max_concurrent_trades": payload.max_concurrent_trades,
        "auto_execute": payload.auto_execute,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.bot_configs.update_one(
        {"user_id": user["id"]}, {"$set": update}, upsert=True
    )
    cfg = await db.bot_configs.find_one({"user_id": user["id"]})
    return _serialize(cfg)


@router.post("/start")
async def start_bot(user=Depends(get_current_user)):
    db = get_db()
    await db.bot_configs.update_one(
        {"user_id": user["id"]},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"active": True}


@router.post("/stop")
async def stop_bot(user=Depends(get_current_user)):
    db = get_db()
    await db.bot_configs.update_one(
        {"user_id": user["id"]},
        {"$set": {"active": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"active": False}
