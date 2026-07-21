"""Refinement 5 · Broker execution-behaviour learning.

Persists per-(broker, session) execution aggregates from REAL fills, rejects
and acks into `scalp_broker_stats` — incremental $inc counters, O(1) per
event, computed averages on read. Feeds future execution decisions with
actual broker behaviour instead of assumptions.
"""
from datetime import datetime, timezone

from scalp.exec_quality import session_name


def _key(broker: str) -> str:
    return (broker or "unknown").strip() or "unknown"


async def record(db, broker: str, *, submissions: int = 0, rejects: int = 0,
                 entry_slip_pips: float | None = None,
                 exit_slip_pips: float | None = None,
                 ack_ms: float | None = None,
                 spread_pips: float | None = None) -> None:
    inc: dict[str, float] = {}
    if submissions:
        inc["submissions"] = submissions
    if rejects:
        inc["rejects"] = rejects
    if entry_slip_pips is not None:
        inc["entry_slip_sum"] = abs(float(entry_slip_pips))
        inc["entry_slip_n"] = 1
    if exit_slip_pips is not None:
        inc["exit_slip_sum"] = abs(float(exit_slip_pips))
        inc["exit_slip_n"] = 1
    if ack_ms is not None:
        inc["ack_ms_sum"] = float(ack_ms)
        inc["ack_n"] = 1
    if spread_pips is not None:
        inc["spread_sum"] = float(spread_pips)
        inc["spread_n"] = 1
    if not inc:
        return
    now = datetime.now(timezone.utc)
    await db.scalp_broker_stats.update_one(
        {"broker_key": _key(broker), "session": session_name(now.hour)},
        {"$inc": inc, "$set": {"updated_at": now.isoformat()}},
        upsert=True)


def _avg(doc: dict, s: str, n: str) -> float | None:
    if doc.get(n):
        return round(float(doc.get(s) or 0) / float(doc[n]), 3)
    return None


async def summary(db, broker: str) -> dict:
    """Per-session averages + overall totals for one broker."""
    sessions: dict[str, dict] = {}
    tot = {"submissions": 0, "rejects": 0, "fills": 0}
    async for doc in db.scalp_broker_stats.find({"broker_key": _key(broker)}):
        sessions[doc["session"]] = {
            "submissions": int(doc.get("submissions") or 0),
            "rejects": int(doc.get("rejects") or 0),
            "fills": int(doc.get("entry_slip_n") or 0),
            "avg_entry_slippage_pips": _avg(doc, "entry_slip_sum", "entry_slip_n"),
            "avg_exit_slippage_pips": _avg(doc, "exit_slip_sum", "exit_slip_n"),
            "avg_fill_delay_ms": _avg(doc, "ack_ms_sum", "ack_n"),
            "avg_spread_pips": _avg(doc, "spread_sum", "spread_n"),
            "updated_at": doc.get("updated_at"),
        }
        tot["submissions"] += sessions[doc["session"]]["submissions"]
        tot["rejects"] += sessions[doc["session"]]["rejects"]
        tot["fills"] += sessions[doc["session"]]["fills"]
    attempts = tot["submissions"] + tot["rejects"]
    return {"broker_key": _key(broker), "sessions": sessions,
            "totals": {**tot,
                       "reject_rate": (round(tot["rejects"] / attempts, 3)
                                       if attempts else None)}}
