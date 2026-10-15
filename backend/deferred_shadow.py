"""v1.60.38 — shadow outcome for every DEFERRED signal (execution optimizer: gold off-hours / spread).

Would the deferred entry have hit TP or SL first? Walks the M15 bars that followed the decision (same replay as the
gate ablation — no lookahead, SL-first when both touch in one bar) and stamps the decision row:
    shadow: {outcome: tp_first|sl_first|timeout, r, bars, resolved_at, utc_band}
Runs from the analytics worker; the Strategy Report shows the result per UTC band."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ablation import HORIZON_BARS, replay_outcome
from pip_utils import base_symbol

logger = logging.getLogger("deferred-shadow")

UTC_BANDS = [(0 * 60 + 30, 6 * 60, "Asian hours (00:30–06:00 UTC)"),
             (6 * 60, 13 * 60, "London (06:00–13:00 UTC)"),
             (13 * 60, 21 * 60, "New York (13:00–21:00 UTC)")]
ROLLOVER_BAND = "Rollover (21:00–00:30 UTC)"


def utc_band(ts) -> str:
    """Entry-time UTC band. Rollover wraps midnight (21:00–00:30)."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "n/a"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.weekday() >= 5:
        return "Weekend"
    cur = dt.hour * 60 + dt.minute
    for lo, hi, label in UTC_BANDS:
        if lo <= cur < hi:
            return label
    return ROLLOVER_BAND


async def resolve_deferred_outcomes(db, user_id: str, limit: int = 300) -> dict:
    rows = await db.trade_decisions.find(
        {"user_id": user_id, "status": "deferred", "shadow": {"$exists": False},
         "signal.entry_price": {"$exists": True}, "signal.stop_loss": {"$exists": True}},
        {"symbol": 1, "ts": 1, "signal": 1}).sort("ts", 1).to_list(length=limit)
    bars_cache: dict = {}
    resolved = pending = skipped = 0
    for d in rows:
        sig = d.get("signal") or {}
        base = base_symbol(d.get("symbol") or sig.get("symbol") or "")
        try:
            epoch = datetime.fromisoformat(str(d.get("ts")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            skipped += 1
            continue
        if base not in bars_cache:
            doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}, {"bars": 1})
            bars_cache[base] = (doc or {}).get("bars") or []
        after = [b for b in bars_cache[base] if int(b.get("t") or 0) > epoch]
        if not after:
            pending += 1
            continue
        tp = sig.get("take_profit") or sig.get("tp3") or sig.get("tp1")
        sim = replay_outcome(str(sig.get("action") or "").upper(), float(sig.get("entry_price") or 0),
                             float(sig.get("stop_loss") or 0), float(tp or 0), after)
        if sim is None:
            skipped += 1
            continue
        if sim["outcome"] == "timeout" and len(after) < HORIZON_BARS:
            pending += 1                          # not enough bars yet — try again next sweep
            continue
        await db.trade_decisions.update_one(
            {"_id": d["_id"]},
            {"$set": {"shadow": {**sim, "resolved_at": datetime.now(timezone.utc).isoformat(),
                                 "utc_band": utc_band(d.get("ts"))}}})
        resolved += 1
    return {"resolved": resolved, "pending": pending, "skipped": skipped}


async def sweep_deferred_shadows(db) -> int:
    users = await db.trade_decisions.distinct("user_id", {"status": "deferred", "shadow": {"$exists": False}})
    total = 0
    for uid in users:
        try:
            total += (await resolve_deferred_outcomes(db, uid))["resolved"]
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred shadow sweep failed user=%s: %s", uid, e)
    return total
