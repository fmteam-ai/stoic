"""Pre-news existing-position protector.

The 10-layer veto cascade already blocks NEW entries during high-impact
macro events (Veto #2). This module covers the OTHER half of the problem:
positions that were already open when an event approaches.

Behaviour:
  - Every bot-runner tick, for each user with auto_execute ON:
      • Find OPEN trades on symbols affected by a high-impact event
        within `pre_news_protect_minutes` (default 5) minutes.
      • Queue a FULL_CLOSE via `pending_modification` so the EA flatten
        the position next poll.
      • Broadcast WebSocket + Telegram alert.
"""
import logging
from datetime import datetime, timezone
from bson import ObjectId

from close_commands import request_close
from economic_calendar import upcoming_for
from ws_manager import manager as ws_manager
from intelligence_counters import increment as inc_intel_counter

logger = logging.getLogger("position-protector")


async def _imminent_high_impact(symbol: str, lead_min: int) -> dict | None:
    """Return the soonest HIGH-impact event for `symbol` within `lead_min` minutes, or None."""
    try:
        events = await upcoming_for(symbol, hours=1)
    except Exception as e:
        logger.warning("upcoming_for failed sym=%s: %s", symbol, e)
        return None
    now = datetime.now(timezone.utc).timestamp()
    for e in events:
        if e.get("impact") != "high":
            continue
        ts = e.get("when_ts") or 0
        mins_to = (ts - now) / 60
        if 0 <= mins_to <= lead_min:
            return {**e, "minutes_until": round(mins_to, 1)}
    return None


async def sweep_user(db, user_id: str, cfg: dict) -> int:
    """Run protector for one user. Returns number of trades flattened."""
    if not cfg.get("pre_news_protect_enabled", True):
        return 0
    lead_min = int(cfg.get("pre_news_protect_minutes", 5))
    if lead_min <= 0:
        return 0

    cursor = db.trades.find({
        "user_id": user_id,
        "status": "open",
        "close_requested": {"$ne": True},
    })
    open_trades = await cursor.to_list(length=200)
    flattened = 0

    # Cache event lookups per symbol within one sweep
    cache: dict[str, dict | None] = {}

    for tr in open_trades:
        sym = (tr.get("symbol") or "").upper()
        if not sym:
            continue
        if sym not in cache:
            cache[sym] = await _imminent_high_impact(sym, lead_min)
        event = cache[sym]
        if not event:
            continue

        trade_id = tr["_id"]
        await request_close(db, {"_id": trade_id}, reason="pre_news_protect", actor="position_protector", stamp={
            "close_reason_pending": "pre_news_protect",
            "pre_news_protect": {
                "event_title": event.get("title"),
                "event_country": event.get("country"),
                "minutes_until": event["minutes_until"],
                "queued_at": datetime.now(timezone.utc).isoformat(),
            },
            "pending_modification": {
                "type": "FULL_CLOSE",
                "reason": "pre_news_protect",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        flattened += 1

        await ws_manager.broadcast(user_id, "position_protected", {
            "trade_id": str(trade_id),
            "symbol": sym,
            "event_title": event.get("title"),
            "minutes_until": event["minutes_until"],
        })

        try:
            from notifier import notify_pre_news_close
            await notify_pre_news_close(
                user_id,
                str(trade_id),
                sym,
                event.get("title") or "high-impact event",
                event["minutes_until"],
            )
        except Exception as e:
            logger.debug("notify_pre_news_close failed: %s", e)

        try:
            await inc_intel_counter(user_id, "pre_news_protect")
        except Exception:
            pass

        logger.warning(
            "Pre-news protect user=%s sym=%s trade=%s event='%s' in %.1fmin",
            user_id, sym, trade_id, event.get("title"), event["minutes_until"],
        )

    return flattened


async def sweep_all(db) -> int:
    """Run the protector across every active bot config. Returns total flattened."""
    total = 0
    cursor = db.bot_configs.find({"active": True})
    cfgs = await cursor.to_list(length=200)
    for cfg in cfgs:
        try:
            n = await sweep_user(db, cfg["user_id"], cfg)
            total += n
        except Exception as e:
            logger.exception("sweep_user failed user=%s: %s", cfg.get("user_id"), e)
    return total


__all__ = ["sweep_user", "sweep_all"]
