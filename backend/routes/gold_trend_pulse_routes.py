"""v1.60.57 — gold_trend_pulse read-only card + per-account config (shadow / demo_live / layering)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

router = APIRouter(prefix="/gold-trend-pulse", tags=["gold-trend-pulse"])


@router.get("/card")
async def gtp_card(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    acc = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}, {"label": 1})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    from gold_trend_pulse.engine import card
    out = await card(db, user["id"], account_id)
    out["account"] = acc.get("label")
    return out


class GtpConfig(BaseModel):
    account_id: str
    enabled: bool = False
    mode: str = "shadow"
    layering_enabled: bool = False
    sweep_runner_enabled: bool = True
    news_mode_enabled: bool = True
    news_pre_action: str = "protect"
    news_release_min: int = 5
    news_events: list[str] | None = None
    confirm_live: bool = False


@router.post("/config")
async def gtp_config(req: GtpConfig, user=Depends(get_current_user)):
    if req.mode not in ("shadow", "demo_live"):
        raise HTTPException(status_code=422, detail="mode must be shadow or demo_live")
    if req.mode == "demo_live" and not req.confirm_live:
        raise HTTPException(status_code=422, detail="demo_live requires confirm_live=true — orders WILL be sent once the live gate passes")
    if req.news_pre_action not in ("protect", "close", "tighten") or not (1 <= req.news_release_min <= 30):
        raise HTTPException(status_code=422, detail="news_pre_action must be protect|close|tighten; news_release_min 1–30")
    db = get_db()
    acc = await db.accounts.find_one({"_id": parse_object_id(req.account_id, "Account"), "user_id": user["id"]}, {"_id": 1})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    res = await db.bot_configs.update_one({"user_id": user["id"], "account_id": req.account_id},
                                          {"$set": {"gold_trend_pulse_enabled": req.enabled, "gold_trend_pulse_mode": req.mode,
                                                    "gold_trend_pulse_layering_enabled": req.layering_enabled,
                                                    "gold_trend_pulse_sweep_runner_enabled": req.sweep_runner_enabled,
                                                    "gold_trend_pulse_news_mode_enabled": req.news_mode_enabled,
                                                    "gold_trend_pulse_news_pre_action": req.news_pre_action,
                                                    "gold_trend_pulse_news_release_min": req.news_release_min,
                                                    "gold_trend_pulse_news_events": req.news_events,
                                                    "updated_at": datetime.now(timezone.utc).isoformat()}})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Bot config for this account not found — save Bot Config first")
    from step_up import audit_event
    await audit_event(db, user["id"], "gold_trend_pulse_config", req.model_dump(), None)
    from gold_trend_pulse.engine import card
    return {"ok": True, "card": await card(db, user["id"], req.account_id)}
