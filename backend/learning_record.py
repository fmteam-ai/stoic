"""Autopilot #8 — unified post-trade learning record.

One structured document per closed trade composing everything the learners
need: strategy, regime, model versions, confidence, expected value, costs,
slippage, latency, MFE/MAE, exit reason, realized R, external events, model
disagreement, lifecycle incidents and the failure classification. Stored in
`learning_records`; trades flagged `learning_recorded`.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("learning-record")

SWEEP_EVERY_S = 300
MAX_PER_SWEEP = 15
LOOKBACK_H = 72
_last_sweep = 0.0


def _iso_delta_ms(a, b):
    try:
        ta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        tb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=timezone.utc)
        if tb.tzinfo is None:
            tb = tb.replace(tzinfo=timezone.utc)
        return int((tb - ta).total_seconds() * 1000)
    except Exception:  # noqa: BLE001
        return None


async def build_learning_record(db, trade: dict) -> dict:
    from bson import ObjectId
    sig = {}
    try:
        if trade.get("signal_id"):
            sig = await db.signals.find_one(
                {"_id": ObjectId(trade["signal_id"])}) or {}
    except Exception:  # noqa: BLE001
        pass
    ev = await db.trade_evaluations.find_one(
        {"trade_id": str(trade["_id"])}) or {}

    commission = swap = 0.0
    try:
        if trade.get("mt5_ticket"):
            async for d in db.broker_deals.find(
                    {"account_id": trade.get("account_id"),
                     "position_id": int(trade["mt5_ticket"])},
                    {"commission": 1, "swap": 1}).limit(20):
                commission += float(d.get("commission") or 0)
                swap += float(d.get("swap") or 0)
    except Exception:  # noqa: BLE001
        pass

    incidents = []
    try:
        incidents = await db.trade_events.distinct(
            "event_type", {"trade_id": str(trade["_id"])})
    except Exception:  # noqa: BLE001
        pass

    pnl = float(trade.get("pnl") or 0)
    failure = None
    if pnl < 0:
        from failure_classifier import classify_trade_failure
        try:
            failure = await classify_trade_failure(db, trade)
        except Exception as e:  # noqa: BLE001
            logger.debug("failure classification skipped: %s", e)

    mc = sig.get("monte_carlo") or {}
    unc = sig.get("uncertainty") or {}
    cons = sig.get("consensus") or {}
    return {
        "trade_id": str(trade["_id"]),
        "user_id": trade["user_id"],
        "account_id": trade.get("account_id"),
        "symbol": trade.get("symbol"),
        "action": trade.get("action"),
        "strategy": {"scope": trade.get("scope"),
                     "strategy_class": trade.get("strategy_class"),
                     "engine": (sig.get("strategy_engine")
                                or sig.get("engine_label"))},
        "market_regime": trade.get("market_regime"),
        "model_versions": trade.get("versions") or sig.get("versions"),
        "entry_confidence": sig.get("confidence"),
        "expected_value_r": mc.get("ev_r_net", mc.get("ev_r")),
        "spread_pips": sig.get("spread") or (sig.get("execution") or {}).get("spread"),
        "slippage_pips": trade.get("slippage_pips"),
        "latency_ms": _iso_delta_ms(sig.get("created_at"),
                                    trade.get("opened_at")),
        "lot_size": trade.get("lot_size"),
        "risk_pct": trade.get("risk_pct"),
        "stop_loss": trade.get("stop_loss"),
        "take_profit": trade.get("take_profit"),
        "mfe_r": ev.get("mfe_r"),
        "mae_r": ev.get("mae_r"),
        "exit_reason": trade.get("close_reason"),
        "realized_r": ev.get("realized_r"),
        "pnl": pnl,
        "commission": round(commission, 2),
        "swap": round(swap, 2),
        "external_events": {
            "news_net": (sig.get("news_ai") or {}).get("net"),
            "calendar": ((sig.get("calendar_intel") or {}).get("event")
                         or {}).get("title")
                        if isinstance((sig.get("calendar_intel") or {}).get("event"), dict)
                        else (sig.get("calendar_intel") or {}).get("event"),
            "fed_tone": (sig.get("fed_tone") or {}).get("score"),
        },
        "model_disagreement": {
            "uncertainty": unc.get("uncertainty", unc.get("U")),
            "risk_tier": unc.get("risk"),
            "calibrated_confidence": unc.get("confidence_pct"),
            "consensus_score": cons.get("score"),
        },
        "lifecycle_incidents": incidents,
        "failure": failure,
        "opened_at": trade.get("opened_at"),
        "closed_at": trade.get("closed_at"),
        "recorded_at": datetime.now(timezone.utc),
    }


async def sweep_learning_records(db) -> int:
    """Compose learning records for freshly closed auto trades. Runs AFTER
    the self-evaluation sweep so MFE/MAE grades are available."""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < SWEEP_EVERY_S:
        return 0
    _last_sweep = now
    since = (datetime.now(timezone.utc)
             - timedelta(hours=LOOKBACK_H)).isoformat()
    trades = await db.trades.find(
        {"status": "closed", "pnl": {"$ne": None}, "origin": "auto",
         "closed_at": {"$gte": since}, "self_evaluated": True,
         "learning_recorded": {"$ne": True}}).limit(
        MAX_PER_SWEEP).to_list(MAX_PER_SWEEP)
    done = 0
    for t in trades:
        try:
            rec = await build_learning_record(db, t)
            await db.learning_records.insert_one(rec)
            await db.trades.update_one(
                {"_id": t["_id"]}, {"$set": {"learning_recorded": True}})
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("learning record failed trade=%s: %s",
                             t.get("_id"), e)
    if done:
        logger.info("learning records composed: %d", done)
    return done


async def failure_summary(db, user_id: str, days: int = 30) -> dict:
    """Aggregate failure taxonomy — which component owns the losses."""
    from failure_classifier import ROUTE_FIX
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    buckets: dict[str, dict] = {}
    total_losses = 0
    async for r in db.learning_records.find(
            {"user_id": user_id, "closed_at": {"$gte": since},
             "failure.category": {"$exists": True}},
            {"failure": 1, "pnl": 1}).limit(2000):
        cat = r["failure"]["category"]
        b = buckets.setdefault(cat, {"count": 0, "pnl": 0.0, "examples": []})
        b["count"] += 1
        b["pnl"] += float(r.get("pnl") or 0)
        if len(b["examples"]) < 2:
            b["examples"].append((r["failure"].get("evidence") or [""])[0])
        total_losses += 1
    rows = [{"category": c, "count": b["count"],
             "pnl": round(b["pnl"], 2),
             "share_pct": round(b["count"] / total_losses * 100, 1),
             "route_fix_to": ROUTE_FIX[c], "examples": b["examples"]}
            for c, b in sorted(buckets.items(), key=lambda kv: -kv[1]["count"])]
    return {"window_days": days, "classified_losses": total_losses,
            "categories": rows,
            "principle": "route each fix to the component that failed — "
                         "don't retrain the entry model for an execution problem"}
