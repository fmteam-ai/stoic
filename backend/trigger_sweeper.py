"""Conditional trigger sweeper — evaluates user-set NL triggers each tick.

Audit r14 P0-02: a due trigger is CLAIMED atomically (active → executing with a
fire-event id, owner and lease) before any action runs; actions execute through
the exactly-once executor (deterministic idempotency keys, durable receipts);
risk-increasing actions are suppressed when the user's canonical authority is
BLOCKED/CLOSE_ONLY at fire time; stale leases are recovered without replaying
a completed action; every fire produces ONE `trigger_fire_events` row.
"""
from datetime import datetime, timezone

from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager

ACTIVE_STATUSES = {"$in": [None, "active"]}


def _due(t: dict, price: float):
    baseline = t.get("baseline_price")
    if baseline is None:
        return None, None
    condition = (t.get("condition") or "drop").lower()
    thresh = float(t.get("threshold_pct") or 0)
    change_pct = ((price - baseline) / baseline) * 100
    triggered = ((condition == "drop" and change_pct <= -thresh)
                 or (condition == "rise" and change_pct >= thresh)
                 or (condition == "move" and abs(change_pct) >= thresh))
    return triggered, round(change_pct, 3)


async def fire_claimed(db, t: dict) -> dict:
    """Execute a claimed/reclaimed trigger; idempotent per fire event."""
    import nl_execution as nx
    from canonical_decision import decide_user
    ex = t["execution"]
    await db.trigger_fire_events.update_one(
        {"event_id": ex["id"]},
        {"$setOnInsert": {"event_id": ex["id"], "trigger_id": str(t["_id"]),
                          "user_id": t["user_id"], "symbol": t.get("symbol"),
                          "change_pct": t.get("fired_change_pct"),
                          "at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    authority = await decide_user(db, t["user_id"], fresh=True)
    res = await nx.run_claimed(db, "conditional_triggers", t, t["user_id"],
                               t.get("then") or [], authority=authority)
    if res["status"] == "lease_lost":
        return res
    await db.trigger_fire_events.update_one(
        {"event_id": ex["id"]},
        {"$set": {"status": res["status"], "receipts": res["receipts"],
                  "authority": {"state": authority.get("state"),
                                "decision_id": authority.get("decision_id")}}})
    await ws_manager.broadcast(t["user_id"], "trigger_fired", {
        "trigger_id": str(t["_id"]), "symbol": (t.get("symbol") or "").upper(),
        "change_pct": t.get("fired_change_pct"), "condition": t.get("condition"),
        "threshold_pct": t.get("threshold_pct"), "status": res["status"],
        "receipts": res["receipts"], "fire_event_id": ex["id"],
    })
    return res


async def recover_stale(db) -> int:
    """Re-claim triggers whose executing lease expired (crashed sweeper)."""
    import nl_execution as nx
    n = 0
    async for t in db.conditional_triggers.find({"status": "executing"}):
        got = await nx.reclaim_expired(db, "conditional_triggers", t)
        if got is not None:
            await fire_claimed(db, got)
            n += 1
    return n


async def sweep_once() -> dict:
    import nl_execution as nx
    db = get_db()
    recovered = await recover_stale(db)
    triggers = await db.conditional_triggers.find(
        {"active": True, "status": ACTIVE_STATUSES}).to_list(length=200)
    fired = 0
    quote_cache = {}
    for t in triggers:
        sym = (t.get("symbol") or "BTCUSD").upper()
        if sym not in quote_cache:
            try:
                quote_cache[sym] = await get_quote(sym)
            except Exception:
                continue
        price = quote_cache[sym].get("price")
        if not price:
            continue
        if t.get("baseline_price") is None:
            await db.conditional_triggers.update_one(
                {"_id": t["_id"], "baseline_price": None},
                {"$set": {"baseline_price": price,
                          "baseline_set_at": datetime.now(timezone.utc).isoformat()}})
            continue
        triggered, change_pct = _due(t, price)
        if not triggered:
            continue
        claimed = await nx.claim(
            db, "conditional_triggers", {"_id": t["_id"], "active": True},
            from_status=ACTIVE_STATUSES,
            extra_set={"active": False, "fired_at": datetime.now(timezone.utc).isoformat(),
                       "fired_change_pct": change_pct, "fired_price": price})
        if claimed is None:
            continue  # another sweeper won the claim
        await fire_claimed(db, claimed)
        fired += 1
    return {"evaluated": len(triggers), "fired": fired, "recovered": recovered}
