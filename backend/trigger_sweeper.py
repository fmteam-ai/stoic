"""Conditional trigger sweeper — evaluates user-set NL triggers each tick.

Reads `db.conditional_triggers` where active=True, captures a baseline price on
first run, then fires the embedded `then` actions when condition + threshold is
crossed. After firing, the trigger is marked active=False (one-shot).
"""
from datetime import datetime, timezone
from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager


async def sweep_once() -> dict:
    db = get_db()
    cursor = db.conditional_triggers.find({"active": True})
    triggers = await cursor.to_list(length=200)
    fired = 0
    quote_cache = {}
    for t in triggers:
        sym = (t.get("symbol") or "BTCUSD").upper()
        if sym not in quote_cache:
            try:
                quote_cache[sym] = await get_quote(sym)
            except Exception:
                continue
        q = quote_cache[sym]
        price = q.get("price")
        if not price:
            continue
        baseline = t.get("baseline_price")
        if baseline is None:
            await db.conditional_triggers.update_one(
                {"_id": t["_id"]}, {"$set": {"baseline_price": price,
                                             "baseline_set_at":
                                             datetime.now(timezone.utc).isoformat()}}
            )
            continue
        condition = (t.get("condition") or "drop").lower()
        thresh = float(t.get("threshold_pct") or 0)
        change_pct = ((price - baseline) / baseline) * 100
        triggered = (
            (condition == "drop" and change_pct <= -thresh) or
            (condition == "rise" and change_pct >= thresh) or
            (condition == "move" and abs(change_pct) >= thresh)
        )
        if not triggered:
            continue
        # Execute the embedded actions
        from routes.nl_routes import _execute_actions
        receipts = await _execute_actions(t["user_id"], t.get("then") or [])
        await db.conditional_triggers.update_one(
            {"_id": t["_id"]},
            {"$set": {"active": False, "fired_at":
                      datetime.now(timezone.utc).isoformat(),
                      "fired_change_pct": round(change_pct, 3),
                      "fired_receipts": receipts}},
        )
        await ws_manager.broadcast(t["user_id"], "trigger_fired", {
            "trigger_id": str(t["_id"]),
            "symbol": sym,
            "change_pct": round(change_pct, 3),
            "condition": condition,
            "threshold_pct": thresh,
            "receipts": receipts,
        })
        fired += 1
    return {"evaluated": len(triggers), "fired": fired}
