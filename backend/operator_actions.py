"""Phase 3.2 — Operator intervention framework.

One-click, AUDITED interventions. All actions REDUCE authority or risk, so
none needs step-up MFA (raising authority stays behind the promotion gate;
config rollback keeps its own step-up-gated endpoint).
"""
from datetime import datetime, timezone

ACTIONS = {
    "freeze_trading": "set the account's operational mode to observe — "
                      "decisions are intercepted, nothing executes",
    "reduce_exposure": "halve risk per trade (floor 0.05%)",
    "pause_symbol": "remove one symbol from the bot's trade list",
    "defensive_mode": "halve risk AND limit to 1 concurrent trade",
    "panic_mode": "freeze EVERY account to observe immediately",
}


async def run_action(db, user_id: str, action: str,
                     params: dict | None = None) -> dict:
    params = params or {}
    account_id = params.get("account_id")
    now = datetime.now(timezone.utc)
    q = {"user_id": user_id, "active": True}
    if account_id:
        q["account_id"] = account_id

    if action == "freeze_trading":
        r = await db.bot_configs.update_many(
            q, {"$set": {"operational_mode": "observe"}})
        detail = f"{r.modified_count} config(s) frozen to observe"
    elif action == "panic_mode":
        r = await db.bot_configs.update_many(
            {"user_id": user_id, "active": True},
            {"$set": {"operational_mode": "observe"}})
        detail = f"PANIC: all {r.modified_count} config(s) frozen to observe"
    elif action == "reduce_exposure":
        n = 0
        async for cfg in db.bot_configs.find(q):
            new_risk = max(0.05, round(
                float(cfg.get("risk_pct") or 0.5) * 0.5, 3))
            await db.bot_configs.update_one(
                {"_id": cfg["_id"]}, {"$set": {"risk_pct": new_risk}})
            n += 1
        detail = f"risk halved on {n} config(s)"
    elif action == "defensive_mode":
        n = 0
        async for cfg in db.bot_configs.find(q):
            await db.bot_configs.update_one(
                {"_id": cfg["_id"]},
                {"$set": {"risk_pct": max(0.05, round(
                    float(cfg.get("risk_pct") or 0.5) * 0.5, 3)),
                    "max_concurrent_trades": 1}})
            n += 1
        detail = f"defensive mode on {n} config(s): half risk, 1 trade max"
    elif action == "pause_symbol":
        symbol = (params.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("pause_symbol requires params.symbol")
        n = 0
        async for cfg in db.bot_configs.find(q):
            syms = [s for s in (cfg.get("symbols") or []) if s != symbol]
            await db.bot_configs.update_one(
                {"_id": cfg["_id"]}, {"$set": {"symbols": syms}})
            n += 1
        detail = f"{symbol} removed from {n} config(s)"
    else:
        raise ValueError(f"unknown operator action '{action}'")

    await db.audit_log.insert_one({
        "user_id": user_id, "action": f"operator_action:{action}",
        "detail": {"params": params, "result": detail},
        "step_up_verified": False, "at": now.isoformat()})
    return {"action": action, "detail": detail, "at": now.isoformat()}
