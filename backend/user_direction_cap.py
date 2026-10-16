"""v1.60.50 — per-user (symbol, direction) exposure reservation.

The cross-account cap (v1.60.48) counted open trades, but account loops run in asyncio.gather so two accounts
both saw 0 and both opened. The slot is now reserved ATOMICALLY: a unique reservation document per
(user, symbol, direction, slot) with a TTL; it is released when the order is rejected/blocked/fails, and
counts against the cap until then. Only LIVE automated trades count (paper / shadow never do)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId

COLL = "user_direction_reservations"
TTL_SEC = 180            # a pending order that never confirmed frees the slot after 3 min


async def ensure_indexes(db) -> None:
    await db[COLL].create_index([("user_id", 1), ("symbol", 1), ("action", 1), ("slot", 1)], unique=True)
    await db[COLL].create_index("expires_at", expireAfterSeconds=0)


def live_same_direction_filter(user_id: str, symbol_match_q, action: str) -> dict:
    return {"user_id": user_id, "symbol": symbol_match_q, "action": action,
            "status": {"$in": ["pending", "open"]}, "origin": "auto",
            "mode": {"$ne": "paper"}, "shadow": {"$ne": True}}


async def reserve_user_direction(db, *, user_id: str, base_symbol: str, action: str, cap: int,
                                 account_id: str | None, symbol_match_q) -> tuple[str | None, int]:
    """Try to take one of `cap` slots. Returns (reservation_id, occupied) — id None when the cap is reached.
    occupied = live open/pending trades + unexpired reservations held by OTHER in-flight entries."""
    now = datetime.now(timezone.utc)
    await db[COLL].delete_many({"expires_at": {"$lt": now}})       # belt and braces when the TTL monitor lags
    live = await db.trades.count_documents(live_same_direction_filter(user_id, symbol_match_q, action))
    held = await db[COLL].count_documents({"user_id": user_id, "symbol": base_symbol, "action": action})
    occupied = live + held
    if occupied >= cap:
        return None, occupied
    for slot in range(cap):
        if slot < live:
            continue                                         # slots already consumed by open trades
        try:
            res = await db[COLL].insert_one({"user_id": user_id, "symbol": base_symbol, "action": action,
                                             "slot": slot, "account_id": account_id, "created_at": now,
                                             "expires_at": now + timedelta(seconds=TTL_SEC)})
            return str(res.inserted_id), occupied
        except Exception as e:  # noqa: BLE001 — DuplicateKeyError: another account took this slot first
            if "duplicate" not in str(e).lower() and type(e).__name__ != "DuplicateKeyError":
                raise
    return None, occupied


async def user_direction_cap_for(db, user_id: str, account_id: str | None) -> int:
    """v1.60.52 (#38) — the cap the scalp fast path must honour: the account's bot_config, else the user's default
    config, else the model default (1). 0 = off."""
    from models import BotConfigUpdate
    default = int(BotConfigUpdate().user_direction_cap)
    cfg = None
    if account_id:
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": account_id}, {"user_direction_cap": 1})
    if cfg is None:
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": None}, {"user_direction_cap": 1})
    try:
        return int((cfg or {}).get("user_direction_cap", default) or 0)
    except (TypeError, ValueError):
        return default


async def release_user_direction(db, reservation_id: str | None) -> None:
    if reservation_id:
        await db[COLL].delete_one({"_id": ObjectId(reservation_id)})
