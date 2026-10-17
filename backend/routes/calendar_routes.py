from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from database import get_db
from economic_calendar import get_events, upcoming_for, macro_freeze_check, next_freeze, freeze_settings

router = APIRouter(prefix="/calendar", tags=["calendar"])


async def user_freeze_cfg(user: dict, account_id: str | None = None) -> dict:
    """v1.60.39 — the account's Bot Config drives the freeze windows (falls back to the user's default config)."""
    q = {"user_id": user["id"]}
    if account_id:
        q["account_id"] = account_id
    cfg = await get_db().bot_configs.find_one(q)
    if not cfg and account_id:
        cfg = await get_db().bot_configs.find_one({"user_id": user["id"], "account_id": None})
    return cfg or {}


@router.get("")
async def all_events(user=Depends(get_current_user)):
    return {"events": await get_events()}


@router.get("/upcoming/{symbol}")
async def upcoming(symbol: str, hours: int = Query(24, ge=1, le=168), account_id: str | None = None,
                   user=Depends(get_current_user)):
    cfg = await user_freeze_cfg(user, account_id)
    from economic_calendar import feed_status, released_surprises
    fs = feed_status()
    act = fs.get("active_source")
    return {"symbol": symbol.upper(),
            "hours": hours,
            "events": await upcoming_for(symbol, hours, cfg),
            # v1.60.53 (A6) — source + last refresh + released actual/forecast/surprise (advisory)
            "feed": {"active_source": act, "status": fs.get("status"),
                     "last_refresh": ((fs.get("sources") or {}).get(act) or {}).get("last_fetch_at") if act else None,
                     "finnhub": (fs.get("finnhub") or {}).get("configured")},
            "released": await released_surprises(symbol, hours=6, cfg=cfg)}


@router.get("/freeze/{symbol}")
async def freeze(symbol: str, account_id: str | None = None, user=Depends(get_current_user)):
    cfg = await user_freeze_cfg(user, account_id)
    out = await macro_freeze_check(symbol, cfg)
    return {**out, "settings": freeze_settings(cfg)}


@router.get("/next-freeze/{symbol}")
async def next_freeze_for(symbol: str, account_id: str | None = None, user=Depends(get_current_user)):
    cfg = await user_freeze_cfg(user, account_id)
    return {"symbol": symbol.upper(), "next_freeze": next_freeze(symbol, await get_events(), cfg),
            "settings": freeze_settings(cfg)}
