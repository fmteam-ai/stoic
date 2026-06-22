"""Daily rolling counters for trading-intelligence vetoes/blocks.

Tracks per-user daily counts of "things the intelligence layer prevented":

  - mtf_veto        : Multi-Timeframe trend gate vetoed a directional signal
  - auto_tune_block : Auto-Tune raised the confidence floor above the signal
  - spread_block    : MT5 Spread filter blocked auto-execute (live only)
  - slippage_veto   : Server-side slippage check force-closed a fill

Stored in `db.intelligence_counters` with one document per (user_id, day).
A 24-hour rolling figure is computed by summing today + yesterday.
"""
from datetime import datetime, timezone, timedelta

from database import get_db

CATEGORIES = ("mtf_veto", "auto_tune_block", "spread_block", "slippage_veto", "learned_meta_veto")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


async def increment(user_id: str, kind: str, *, by: int = 1) -> None:
    if kind not in CATEGORIES or not user_id or by <= 0:
        return
    db = get_db()
    await db.intelligence_counters.update_one(
        {"user_id": user_id, "day": _today()},
        {
            "$inc": {kind: int(by)},
            "$set": {"updated_at": datetime.now(timezone.utc).isoformat()},
        },
        upsert=True,
    )


async def get_today(user_id: str) -> dict:
    db = get_db()
    doc = await db.intelligence_counters.find_one({"user_id": user_id, "day": _today()})
    out = {k: 0 for k in CATEGORIES}
    if doc:
        for k in CATEGORIES:
            out[k] = int(doc.get(k, 0) or 0)
    return out


async def get_window_24h(user_id: str) -> dict:
    """Sum today + yesterday (rolling-ish 24h)."""
    db = get_db()
    cursor = db.intelligence_counters.find(
        {"user_id": user_id, "day": {"$in": [_today(), _yesterday()]}}
    )
    docs = await cursor.to_list(length=2)
    out = {k: 0 for k in CATEGORIES}
    for d in docs:
        for k in CATEGORIES:
            out[k] += int(d.get(k, 0) or 0)
    out["total"] = sum(out.values())
    return out
