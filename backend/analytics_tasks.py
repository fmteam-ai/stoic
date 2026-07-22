"""Phase F · Analytics service tasks — DB-only aggregation so the analytics
worker can run as an independent process (no in-memory runner access)."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("analytics")


def _day_start_ms(day: str) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def run_daily_aggregates(db, day: str | None = None) -> dict:
    """Upserts per-(symbol, model_key) daily stats into scalp_daily_stats:
    decision counts, live trades, win rate, net pips, reject-stage mix."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_ms = _day_start_ms(day)
    pipeline = [
        {"$match": {"ts_ms": {"$gte": start_ms}}},
        {"$group": {
            "_id": {"symbol": "$symbol", "model_key": "$model_key"},
            "decisions": {"$sum": 1},
            "live_traded": {"$sum": {"$cond": [
                {"$eq": ["$verdict", "live_traded"]}, 1, 0]}},
            "rejected": {"$sum": {"$cond": [
                {"$eq": ["$verdict", "rejected"]}, 1, 0]}},
            "resolved": {"$sum": {"$cond": [
                {"$eq": ["$outcome.resolved", True]}, 1, 0]}},
            "wins": {"$sum": {"$cond": [
                {"$gt": ["$outcome.net_pips", 0]}, 1, 0]}},
            "net_pips": {"$sum": {"$ifNull": ["$outcome.net_pips", 0]}},
            "avg_quality": {"$avg": "$decision_quality.score"},
        }},
    ]
    rows = 0
    async for g in db.scalp_decisions.aggregate(pipeline):
        key = g["_id"]
        resolved = g["resolved"] or 0
        await db.scalp_daily_stats.update_one(
            {"_id": f"{day}:{key.get('symbol')}:{key.get('model_key')}"},
            {"$set": {
                "day": day, "symbol": key.get("symbol"),
                "model_key": key.get("model_key"),
                "decisions": g["decisions"],
                "live_traded": g["live_traded"],
                "rejected": g["rejected"],
                "resolved": resolved,
                "wins": g["wins"],
                "win_rate": round(g["wins"] / resolved, 4) if resolved else None,
                "net_pips": round(g["net_pips"] or 0.0, 2),
                "avg_decision_quality": (round(g["avg_quality"], 1)
                                         if g.get("avg_quality") is not None
                                         else None),
                "updated_at": datetime.now(timezone.utc)}},
            upsert=True)
        rows += 1

    # reject-stage mix (why is the engine saying no today?)
    stage_pipe = [
        {"$match": {"ts_ms": {"$gte": start_ms}, "verdict": "rejected",
                    "reject_stage": {"$ne": None}}},
        {"$group": {"_id": "$reject_stage", "n": {"$sum": 1}}},
    ]
    stages = {}
    async for g in db.scalp_decisions.aggregate(stage_pipe):
        stages[str(g["_id"])] = g["n"]
    if stages:
        await db.scalp_daily_stats.update_one(
            {"_id": f"{day}:__reject_stages__"},
            {"$set": {"day": day, "kind": "reject_stages", "stages": stages,
                      "updated_at": datetime.now(timezone.utc)}},
            upsert=True)
    return {"day": day, "rows": rows, "reject_stages": len(stages)}
