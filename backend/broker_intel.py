"""Broker execution intelligence (Phase 2) — continuous per-broker scoring.

Measures every connected broker from REAL execution evidence:
  spread (live heartbeat stream vs instrument-typical)
  slippage (requested_price vs fill, EA v1.40+)
  fill speed (dispatch → broker acknowledgement)
  rejected orders + requotes (failed reports, retcode taxonomy)
  freeze/stops levels (symbol_specs, EA v1.48+)

Produces a 0-100 live broker score per account, ranks brokers for routing/
recommendation, and detects deteriorating execution (score drop vs its own
7-day baseline → ops alert).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from database import get_db
from pip_utils import base_symbol, price_to_pips

logger = logging.getLogger("broker-intel")

WINDOW_DAYS = 7
WEIGHTS = {"spread": 0.25, "slippage": 0.25, "fill_speed": 0.20,
           "rejects": 0.20, "freeze": 0.10}
DETERIORATION_DROP = 15.0


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(v):
    try:
        ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _typical_spread_pips(symbol: str) -> float:
    from monte_carlo import TYPICAL_SPREAD
    base = base_symbol(symbol)
    price_spread = TYPICAL_SPREAD.get(base)
    if price_spread is None:
        return 5.0
    return max(0.1, price_to_pips(base, price_spread))


def spread_component(current_spreads: dict | None) -> dict:
    """100 when at/below instrument-typical spread, linear decay above."""
    if not current_spreads:
        return {"score": None, "detail": "no live spread stream"}
    ratios = []
    worst = None
    for sym, sp in current_spreads.items():
        try:
            typ = _typical_spread_pips(sym)
            r = float(sp) / typ
            ratios.append(r)
            if worst is None or r > worst[1]:
                worst = (base_symbol(sym), r, float(sp))
        except Exception:
            continue
    if not ratios:
        return {"score": None, "detail": "no parsable spreads"}
    avg_r = sum(ratios) / len(ratios)
    score = 100.0 if avg_r <= 1.0 else max(0.0, 100.0 - (avg_r - 1.0) * 80.0)
    return {"score": round(score, 1), "avg_ratio": round(avg_r, 2),
            "detail": f"avg {avg_r:.2f}× typical"
                      + (f" (worst {worst[0]} {worst[2]:.1f}p)" if worst else "")}


def slippage_component(trades: list) -> dict:
    slips = []
    for t in trades:
        req = t.get("requested_price")
        fill = t.get("entry_price")
        if not req or not fill:
            continue
        slips.append(abs(price_to_pips(t.get("symbol"), float(fill) - float(req))))
    if not slips:
        return {"score": None, "n": 0, "detail": "no measured fills (EA v1.40+ required)"}
    avg = sum(slips) / len(slips)
    typ = _typical_spread_pips(trades[0].get("symbol") or "XAUUSD")
    score = max(0.0, 100.0 - 100.0 * avg / (2.0 * typ))
    return {"score": round(score, 1), "avg_slippage_pips": round(avg, 2),
            "n": len(slips), "detail": f"{avg:.2f} pip avg over {len(slips)} fills"}


def fill_speed_component(trades: list) -> dict:
    secs = []
    for t in trades:
        d = _parse_iso(t.get("_dispatched_at"))
        a = _parse_iso(t.get("acknowledged_at"))
        if d and a and a >= d:
            secs.append(min((a - d).total_seconds(), 60.0))
    if not secs:
        return {"score": None, "n": 0, "detail": "no measured fills"}
    avg = sum(secs) / len(secs)
    score = max(0.0, min(100.0, 100.0 - (avg - 1.0) * (100.0 / 29.0)))
    return {"score": round(score, 1), "avg_fill_sec": round(avg, 2),
            "n": len(secs), "detail": f"{avg:.1f}s avg dispatch→fill"}


def reject_component(trades: list, failed: list) -> dict:
    total = len(trades) + len(failed)
    if total == 0:
        return {"score": None, "n": 0, "detail": "no submissions in window"}
    requotes = sum(1 for t in failed
                   if "10004" in str(t.get("error") or ""))
    rate = len(failed) / total
    score = max(0.0, 100.0 * (1.0 - rate * 4.0))
    return {"score": round(score, 1), "reject_rate": round(rate, 3),
            "rejects": len(failed), "requotes": requotes, "n": total,
            "detail": f"{len(failed)}/{total} rejected"
                      + (f" ({requotes} requotes)" if requotes else "")}


def freeze_component(symbol_specs: dict | None) -> dict:
    if not symbol_specs:
        return {"score": None, "detail": "no symbol specs (EA v1.48+ required)"}
    worst = 0.0
    for spec in symbol_specs.values():
        try:
            worst = max(worst, float(spec.get("stops_level") or 0),
                        float(spec.get("freeze_level") or 0))
        except Exception:
            continue
    score = max(0.0, 100.0 - worst * 1.5)
    return {"score": round(score, 1), "worst_level_points": worst,
            "detail": ("no stop/freeze restrictions" if worst == 0
                       else f"worst stops/freeze level {worst:.0f} points")}


async def score_account(db, account: dict) -> dict:
    acc_id = str(account["_id"])
    since = (_now() - timedelta(days=WINDOW_DAYS)).isoformat()
    filled = await db.trades.find(
        {"account_id": acc_id, "origin": "auto",
         "status": {"$in": ["open", "closed"]},
         "opened_at": {"$gte": since}},
        {"requested_price": 1, "entry_price": 1, "symbol": 1,
         "_dispatched_at": 1, "acknowledged_at": 1}).to_list(length=500)
    failed = await db.trades.find(
        {"account_id": acc_id, "origin": "auto", "status": "failed",
         "created_at": {"$gte": since}},
        {"error": 1}).to_list(length=200)

    components = {
        "spread": spread_component(account.get("current_spreads")),
        "slippage": slippage_component(filled),
        "fill_speed": fill_speed_component(filled),
        "rejects": reject_component(filled, failed),
        "freeze": freeze_component(account.get("symbol_specs")),
    }
    total_w, acc = 0.0, 0.0
    for key, comp in components.items():
        if comp["score"] is None:
            continue
        acc += WEIGHTS[key] * comp["score"]
        total_w += WEIGHTS[key]
    score = round(acc / total_w, 1) if total_w > 0 else None
    provisional = len(filled) < 3 or total_w < 0.5
    return {"account_id": acc_id, "label": account.get("label"),
            "broker": account.get("broker") or account.get("server"),
            "score": score, "provisional": provisional,
            "fills_measured": len(filled), "components": components,
            "suitability": style_suitability(components),
            "window_days": WINDOW_DAYS}


def style_suitability(components: dict) -> dict:
    """Tier 10 — which trading styles this broker's measured execution
    suits. Weighted blends of the measured components (None-safe)."""
    def blend(weights: dict) -> int | None:
        acc = tot = 0.0
        for key, w in weights.items():
            s = (components.get(key) or {}).get("score")
            if s is None:
                continue
            acc += w * s
            tot += w
        return round(acc / tot) if tot >= 0.3 else None
    return {
        "scalping": blend({"fill_speed": 0.35, "spread": 0.30,
                           "slippage": 0.25, "rejects": 0.10}),
        "swing": blend({"spread": 0.20, "slippage": 0.25, "rejects": 0.25,
                        "freeze": 0.15, "fill_speed": 0.15}),
        "gold": blend({"slippage": 0.40, "spread": 0.30, "fill_speed": 0.30}),
        "indices": blend({"fill_speed": 0.40, "rejects": 0.30,
                          "spread": 0.30}),
        "crypto": blend({"spread": 0.45, "slippage": 0.35, "rejects": 0.20}),
    }


async def execution_forecast(db, account: dict,
                             symbol: str | None = None) -> dict:
    """Tier 9 — predict execution quality BEFORE trading: expected slippage,
    latency, rejection probability and a fill-quality grade from measured
    history (not price prediction — execution prediction)."""
    from statistics import median
    acc_id = str(account["_id"])
    since = (_now() - timedelta(days=WINDOW_DAYS)).isoformat()
    q = {"account_id": acc_id, "origin": "auto",
         "opened_at": {"$gte": since}}
    if symbol:
        import re as _re
        q["symbol"] = {"$regex": f"^{_re.escape(symbol[:6])}", "$options": "i"}
    slips, lats = [], []
    async for t in db.trades.find(q, {"slippage_pips": 1, "_dispatched_at": 1,
                                      "acknowledged_at": 1}).limit(300):
        if t.get("slippage_pips") is not None:
            slips.append(abs(float(t["slippage_pips"])))
        d, a = _parse_iso(t.get("_dispatched_at")), _parse_iso(
            t.get("acknowledged_at"))
        if d and a:
            lats.append((a - d).total_seconds() * 1000)
    res = await score_account(db, account)
    rej = (res["components"].get("rejects") or {}).get("score")
    score = res.get("score")
    grade = (None if score is None else
             "A" if score >= 85 else "B" if score >= 70
             else "C" if score >= 55 else "D")
    return {
        "account_id": acc_id, "label": res.get("label"),
        "symbol": symbol,
        "expected_slippage_pips": round(median(slips), 2) if slips else None,
        "worst_case_slippage_pips": round(
            sorted(slips)[int(len(slips) * 0.9)], 2) if len(slips) >= 5 else None,
        "expected_latency_ms": round(median(lats)) if lats else None,
        "rejection_probability_pct": (round((100 - rej) * 0.1, 1)
                                      if rej is not None else None),
        "fill_quality_grade": grade,
        "broker_score": score,
        "sample_size": len(slips),
        "suitability": res.get("suitability"),
        "note": "execution forecast from measured fills — sometimes the best "
                "trade is not the best execution",
    }


async def detect_deterioration(db, account_id: str, score: float) -> dict | None:
    """Score ≥15 points below its own 1-7 day baseline → alert once/day."""
    since = _now() - timedelta(days=7)
    until = _now() - timedelta(hours=24)
    hist = await db.broker_intel_scores.find(
        {"account_id": account_id, "score": {"$ne": None},
         "at": {"$gte": since, "$lt": until}},
        {"score": 1}).to_list(length=800)
    if len(hist) < 4:
        return None
    baseline = sum(h["score"] for h in hist) / len(hist)
    drop = baseline - score
    if drop < DETERIORATION_DROP:
        return None
    return {"baseline": round(baseline, 1), "drop": round(drop, 1)}


async def sweep(db) -> int:
    """Score every account with a FRESH heartbeat (active broker link),
    persist a snapshot, alert on deterioration."""
    n = 0
    fresh_cutoff = (_now() - timedelta(hours=6)).isoformat()
    async for acc in db.accounts.find(
            {"status": {"$ne": "deleted"}, "dormant": {"$ne": True},
             "last_heartbeat": {"$gte": fresh_cutoff},
             "harness": {"$ne": True}}):
        try:
            res = await score_account(db, acc)
            await db.broker_intel_scores.insert_one(
                {"account_id": res["account_id"], "score": res["score"],
                 "components": {k: v.get("score") for k, v in res["components"].items()},
                 "at": _now()})
            n += 1
            if res["score"] is not None and not res["provisional"]:
                det = await detect_deterioration(db, res["account_id"], res["score"])
                if det:
                    from alerting import raise_alert
                    await raise_alert(
                        db, "broker_execution_deteriorating", "warning",
                        f"Broker '{acc.get('label')}' execution score fell to "
                        f"{res['score']} ({det['drop']} pts below its "
                        f"{det['baseline']} baseline) — check spread/slippage/rejects",
                        dedup_key=f"broker_deterioration:{res['account_id']}")
        except Exception:  # noqa: BLE001
            logger.exception("broker intel sweep failed for %s", acc.get("_id"))
    return n


async def _broker_intel_loop():
    from workers.base import record_progress
    interval = int(os.environ.get("BROKER_INTEL_INTERVAL_SEC", "900"))
    while True:
        try:
            await asyncio.sleep(interval)
            t0 = _now()
            db = get_db()
            n = await sweep(db)
            await db.broker_intel_scores.delete_many(
                {"at": {"$lt": _now() - timedelta(days=30)}})
            record_progress("_broker_intel_loop", processed=n,
                            started_at=t0, interval_sec=interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("broker intel loop error: %s", e)
