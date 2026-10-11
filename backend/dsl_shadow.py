"""Shadow runner for backtested strategy DSLs — forward paper trading.

A strategy that has been read back (dsl_engine.explain) and backtested
(dsl_engine.backtest) can be PROMOTED to shadow. From the promotion bar
onwards it trades forward on the user's M15 candles with the exact same
per-bar logic as the backtester (dsl_engine.step), so live-shadow results
are directly comparable with the backtest that justified it.

It never places an order: there is no execution path in this module.

Mechanics
  • state {pos, pending} is persisted after the last processed bar
    (`last_ts`); each advance replays only the CLOSED bars after it, so a
    missed cycle is caught up exactly (the EA only stores closed bars);
  • closed trades are upserted by (strategy_id, entry_ts) and the state is
    then committed with a compare-and-set on `last_ts` — two concurrent
    advances (worker + a page view) can neither duplicate a trade nor
    roll the state back;
  • features are computed over the stored window (~800 bars), so a slow
    indicator such as Wilder RSI can differ marginally from a run that
    saw a longer history;
  • if the candle window rolled past `last_ts` (no advance for ~8 days)
    the gap is counted and a pending entry is dropped — never back-filled;
  • a feed whose clock is not UTC-aligned is not advanced (session rules
    and timestamps would be wrong).
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from bson import ObjectId

import dsl_engine as de
from pip_utils import base_symbol

logger = logging.getLogger(__name__)

STRATEGIES = "dsl_shadow_strategies"
TRADES = "dsl_shadow_trades"
MAX_ACTIVE_PER_USER = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _bars(db, user_id: str, base: str):
    cdoc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}) or {}
    return de.normalize_bars(cdoc.get("bars") or []), cdoc.get("t_basis") == "utc"


async def promote(db, user_id: str, dsl: dict, symbol: str, name: str | None = None,
                  cost_bps: float = 2.0) -> dict:
    """Start forward paper trading from the latest stored bar. Stores the
    backtest snapshot that justified the promotion for later comparison."""
    base = base_symbol(symbol)
    active = await db[STRATEGIES].count_documents({"user_id": user_id, "status": "active"})
    if active >= MAX_ACTIVE_PER_USER:
        raise ValueError(f"at most {MAX_ACTIVE_PER_USER} active shadow strategies")
    bars, clock_ok = await _bars(db, user_id, base)
    bt = de.backtest(dsl, [{"t": b["ts"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars],
                     "M15", cost_bps)
    res = bt.get("result") or {}
    doc = {
        "user_id": user_id, "name": (name or dsl.get("name") or "strategy")[:80], "symbol": base,
        "mode": "shadow", "status": "active", "dsl": dsl, "cost_bps": cost_bps,
        "explanation": bt["explanation"]["text"], "warnings": bt["explanation"]["warnings"],
        "backtest": {k: res.get(k) for k in ("bars", "from", "to", "trades_n", "win_rate", "total_return_pct",
                                              "profit_factor", "max_drawdown_pct", "enough_trades")} if res else None,
        # forward-only: nothing before the promotion bar can ever be traded
        "last_ts": bars[-1]["ts"] if bars else None, "state": de.new_state(),
        "gaps": 0, "created_at": _now(), "updated_at": _now(),
        "clock_ok_at_promotion": clock_ok,
    }
    r = await db[STRATEGIES].insert_one(doc)
    doc["_id"] = r.inserted_id
    return doc


async def advance(db, strat: dict) -> dict:
    """Replay closed bars after `last_ts`. Returns {processed, trades, gap, skipped}."""
    bars, clock_ok = await _bars(db, strat["user_id"], strat["symbol"])
    if not clock_ok:
        return {"processed": 0, "trades": 0, "skipped": "candle clock not UTC-aligned"}
    if not bars:
        return {"processed": 0, "trades": 0, "skipped": "no candles"}
    last_ts = strat.get("last_ts")
    # never mutate the caller's document: a stale concurrent reader must replay from ITS state
    state = copy.deepcopy(strat.get("state") or de.new_state())
    gap = False
    if last_ts is None:                                   # promoted before any candle existed
        new_last = bars[-1]["ts"]
        await db[STRATEGIES].update_one({"_id": strat["_id"], "last_ts": None},
                                        {"$set": {"last_ts": new_last, "updated_at": _now()}})
        return {"processed": 0, "trades": 0, "skipped": "anchored to first candles"}
    fresh = [i for i, b in enumerate(bars) if b["ts"] > last_ts]
    if not fresh:
        return {"processed": 0, "trades": 0}
    if bars[0]["ts"] > last_ts:                           # the window rolled past us (weekends are not gaps)
        gap = True
        state["pending"] = None
    ctx = de.make_ctx(strat["dsl"], True, strat.get("cost_bps", 2.0))
    feats = de.feature_series(bars, True)
    closed = []
    for i in fresh:
        t = de.step(state, bars[i], feats[i], ctx)
        if t:
            closed.append(t)
    for t in closed:
        await db[TRADES].update_one(
            {"strategy_id": str(strat["_id"]), "entry_ts": t["entry_ts"]},
            {"$set": {**t, "strategy_id": str(strat["_id"]), "user_id": strat["user_id"],
                      "symbol": strat["symbol"], "recorded_at": _now()}}, upsert=True)
    upd = {"state": state, "last_ts": bars[fresh[-1]]["ts"], "updated_at": _now(),
           "open": de.mark_open(state, bars[-1]["c"], ctx)}
    inc = {"gaps": 1} if gap else {}
    res = await db[STRATEGIES].update_one({"_id": strat["_id"], "last_ts": last_ts},
                                          {"$set": upd, **({"$inc": inc} if inc else {})})
    if not getattr(res, "matched_count", 1):
        return {"processed": 0, "trades": 0, "skipped": "advanced concurrently"}
    strat.update(upd)
    return {"processed": len(fresh), "trades": len(closed), "gap": gap}


async def advance_user(db, user_id: str) -> None:
    for s in await db[STRATEGIES].find({"user_id": user_id, "status": "active"}).to_list(MAX_ACTIVE_PER_USER):
        try:
            await advance(db, s)
        except Exception as e:  # noqa: BLE001 — one bad strategy never blocks the others
            logger.warning("shadow strategy %s advance failed: %s", s.get("_id"), e)


async def advance_all(db, limit: int = 2000) -> int:
    """Worker entry point (analytics loop). Returns strategies advanced."""
    n = 0
    for s in await db[STRATEGIES].find({"status": "active"}).to_list(limit):
        try:
            if (await advance(db, s)).get("processed"):
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("shadow strategy %s advance failed: %s", s.get("_id"), e)
    return n


def live_stats(trades: list) -> dict:
    rets = [t["return_pct"] for t in sorted(trades, key=lambda t: t["exit_ts"])]
    equity = 1.0
    for r in rets:
        equity *= 1 + r / 100
    wins = sum(1 for r in rets if r > 0)
    return {"trades_n": len(rets), "wins": wins,
            "win_rate": round(wins / len(rets), 4) if rets else None,
            "total_return_pct": round((equity - 1) * 100, 3),
            "enough_trades": len(rets) >= de.MIN_TRADES_FOR_CONFIDENCE}


async def list_for_user(db, user_id: str) -> list:
    await advance_user(db, user_id)                       # exact catch-up before reporting
    out = []
    for s in await db[STRATEGIES].find({"user_id": user_id}).sort("_id", -1).to_list(50):
        sid = str(s.pop("_id"))
        trades = await db[TRADES].find({"strategy_id": sid}).to_list(5000)
        for t in trades:
            t.pop("_id", None)
        s.pop("state", None)
        out.append({**s, "id": sid, "live": live_stats(trades),
                    "recent_trades": sorted(trades, key=lambda t: t["exit_ts"], reverse=True)[:20]})
    return out


async def stop(db, user_id: str, strategy_id: str) -> bool:
    try:
        oid = ObjectId(strategy_id)
    except Exception:  # noqa: BLE001
        return False
    res = await db[STRATEGIES].update_one({"_id": oid, "user_id": user_id, "status": "active"},
                                          {"$set": {"status": "stopped", "stopped_at": _now()}})
    return bool(getattr(res, "matched_count", 0))
