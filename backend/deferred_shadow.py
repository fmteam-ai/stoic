"""v1.60.38 — shadow outcome for every DEFERRED signal (execution optimizer: gold off-hours / spread).

Would the deferred entry have hit TP or SL first? Walks the bars that followed the decision (same replay as the
gate ablation — no lookahead, SL-first when both touch in one bar) and stamps the decision row:
    shadow: {outcome: tp_first|sl_first|timeout, r, bars, ambiguous, timeframe, resolved_at, utc_band}

v1.60.39 — M15 bars are far too coarse for 6–9 pt scalp stops ("both touched in one bar → SL first" decided
211/211 on 15 Oct, all on bar 1). Replay now runs on M1 bars when available — the EA's M1 feed if it ever
streams one, else M1 bars aggregated from the scalp tick store — and falls back to M15 only when no M1
coverage exists. Ideas are counted once per decision cluster (same symbol + direction within 15 min) in the
Strategy Report, not once per re-evaluation."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from ablation import HORIZON_BARS, replay_outcome
from pip_utils import base_symbol

logger = logging.getLogger("deferred-shadow")

UTC_BANDS = [(0 * 60 + 30, 6 * 60, "Asian hours (00:30–06:00 UTC)"),
             (6 * 60, 13 * 60, "London (06:00–13:00 UTC)"),
             (13 * 60, 21 * 60, "New York (13:00–21:00 UTC)")]
ROLLOVER_BAND = "Rollover (21:00–00:30 UTC)"
HORIZON_SEC = HORIZON_BARS * 900            # 24 h — same span whichever timeframe replays it
M1_HORIZON_BARS = HORIZON_SEC // 60         # 1440
CLUSTER_WINDOW_SEC = 15 * 60                # same symbol + direction within 15 min = one idea


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


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def m1_bars_from_ticks(ticks: list, start_epoch: float, end_epoch: float) -> list:
    """Mid-price ticks ({tm ms, b, a}) → M1 OHLC bars [{t,o,h,l,c,n}] inside (start, end], sorted by t."""
    buckets: dict = {}
    for tk in ticks:
        try:
            tm = int(tk["tm"]) / 1000.0
            mid = (float(tk["b"]) + float(tk["a"])) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
        if tm <= start_epoch or tm > end_epoch:
            continue
        t = int(tm // 60) * 60
        b = buckets.get(t)
        if b is None:
            buckets[t] = {"t": t, "o": mid, "h": mid, "l": mid, "c": mid, "n": 1}
        else:
            b["h"] = max(b["h"], mid)
            b["l"] = min(b["l"], mid)
            b["c"] = mid
            b["n"] += 1
    return [buckets[t] for t in sorted(buckets)]


async def m1_bars_after(db, user_id: str, base: str, epoch: float) -> list:
    """M1 bars after `epoch` (≤ 24 h): EA M1 feed first, then the scalp tick store. [] when neither covers it."""
    end = epoch + HORIZON_SEC
    doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M1"}, {"bars": 1})
    bars = [b for b in ((doc or {}).get("bars") or []) if epoch < int(b.get("t") or 0) <= end]
    if bars:
        return bars
    ticks: list = []
    cur = db.scalp_ticks.find({"user_id": user_id, "symbol": {"$regex": f"^{re.escape(base)}"},
                               "last_ms": {"$gt": int(epoch * 1000)}, "first_ms": {"$lte": int(end * 1000)}},
                              {"ticks": 1}).sort("first_ms", 1)
    async for batch in cur:
        ticks.extend(batch.get("ticks") or [])
    return m1_bars_from_ticks(ticks, epoch, end)


def _coverage_sec(bars: list, epoch: float) -> float:
    return (int(bars[-1]["t"]) - epoch) if bars else 0.0


async def resolve_deferred_outcomes(db, user_id: str, limit: int = 300) -> dict:
    rows = await db.trade_decisions.find(
        {"user_id": user_id, "status": "deferred", "shadow": {"$exists": False},
         "signal.entry_price": {"$exists": True}, "signal.stop_loss": {"$exists": True}},
        {"symbol": 1, "ts": 1, "signal": 1}).sort("ts", 1).to_list(length=limit)
    m15_cache: dict = {}
    resolved = pending = skipped = 0
    by_tf: dict = defaultdict(int)
    for d in rows:
        sig = d.get("signal") or {}
        base = base_symbol(d.get("symbol") or sig.get("symbol") or "")
        epoch = _epoch(d.get("ts"))
        if epoch is None:
            skipped += 1
            continue
        action = str(sig.get("action") or "").upper()
        entry, sl = float(sig.get("entry_price") or 0), float(sig.get("stop_loss") or 0)
        tp = float(sig.get("take_profit") or sig.get("tp3") or sig.get("tp1") or 0)

        sim, tf = None, None
        m1 = await m1_bars_after(db, user_id, base, epoch)
        if m1:
            sim = replay_outcome(action, entry, sl, tp, m1, horizon_bars=M1_HORIZON_BARS)
            tf = "M1"
            # M1 feed ran out before the horizon with no verdict → let M15 finish the walk
            if sim and sim["outcome"] == "timeout" and _coverage_sec(m1, epoch) < HORIZON_SEC - 900:
                sim = None
        if sim is None:
            if base not in m15_cache:
                doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}, {"bars": 1})
                m15_cache[base] = (doc or {}).get("bars") or []
            after = [b for b in m15_cache[base] if int(b.get("t") or 0) > epoch]
            if not after:
                pending += 1
                continue
            sim = replay_outcome(action, entry, sl, tp, after)
            tf = "M15"
            if sim is None:
                skipped += 1
                continue
            if sim["outcome"] == "timeout" and len(after) < HORIZON_BARS:
                pending += 1                          # not enough bars yet — try again next sweep
                continue
        await db.trade_decisions.update_one(
            {"_id": d["_id"]},
            {"$set": {"shadow": {**sim, "timeframe": tf, "resolved_at": datetime.now(timezone.utc).isoformat(),
                                 "utc_band": utc_band(d.get("ts"))}}})
        resolved += 1
        by_tf[tf] += 1
    return {"resolved": resolved, "pending": pending, "skipped": skipped, "by_timeframe": dict(by_tf)}


def cluster_deferred(deferred: list) -> list:
    """One row per idea: same symbol + direction within 15 min of the cluster's first decision. The representative
    is the earliest decision; its shadow is the earliest resolved shadow in the cluster. Adds `re_evaluations`."""
    rows = sorted((d for d in deferred if _epoch(d.get("ts")) is not None), key=lambda d: _epoch(d["ts"]))
    open_clusters: dict = {}
    out: list = []
    for d in rows:
        sig = d.get("signal") or {}
        key = (base_symbol(d.get("symbol") or sig.get("symbol") or ""), str(sig.get("action") or "").upper())
        ts = _epoch(d["ts"])
        c = open_clusters.get(key)
        if c is None or ts - c["start"] > CLUSTER_WINDOW_SEC:
            c = {"start": ts, "row": {**d, "re_evaluations": 1}}
            open_clusters[key] = c
            out.append(c["row"])
            continue
        c["row"]["re_evaluations"] += 1
        if not (c["row"].get("shadow") or {}).get("outcome") and (d.get("shadow") or {}).get("outcome"):
            c["row"]["shadow"] = d["shadow"]
    return out


async def rescore_legacy_shadows(db, user_id: str, limit: int = 200) -> dict:
    """v1.60.39 — shadows stamped before the M1 replay existed (no `timeframe`) are re-run on M1 bars when the
    tick store covers them (15 Oct: 211 sl_first-on-bar-1 verdicts); otherwise just tagged M15."""
    rows = await db.trade_decisions.find(
        {"user_id": user_id, "status": "deferred", "shadow": {"$exists": True}, "shadow.timeframe": {"$exists": False}},
        {"symbol": 1, "ts": 1, "signal": 1, "shadow": 1}).sort("ts", 1).to_list(length=limit)
    rescored = tagged = 0
    for d in rows:
        sig = d.get("signal") or {}
        base = base_symbol(d.get("symbol") or sig.get("symbol") or "")
        epoch = _epoch(d.get("ts"))
        sim = None
        if epoch is not None:
            m1 = await m1_bars_after(db, user_id, base, epoch)
            if m1:
                sim = replay_outcome(str(sig.get("action") or "").upper(), float(sig.get("entry_price") or 0),
                                     float(sig.get("stop_loss") or 0),
                                     float(sig.get("take_profit") or sig.get("tp3") or sig.get("tp1") or 0),
                                     m1, horizon_bars=M1_HORIZON_BARS)
                if sim and sim["outcome"] == "timeout" and _coverage_sec(m1, epoch) < HORIZON_SEC - 900:
                    sim = None
        if sim:
            old = d.get("shadow") or {}
            await db.trade_decisions.update_one({"_id": d["_id"]}, {"$set": {"shadow": {
                **sim, "timeframe": "M1", "resolved_at": datetime.now(timezone.utc).isoformat(),
                "utc_band": old.get("utc_band") or utc_band(d.get("ts")),
                "rescored_from": {"timeframe": "M15", "outcome": old.get("outcome"), "r": old.get("r")}}}})
            rescored += 1
        else:
            await db.trade_decisions.update_one({"_id": d["_id"]}, {"$set": {"shadow.timeframe": "M15"}})
            tagged += 1
    return {"rescored": rescored, "tagged_m15": tagged}


async def sweep_deferred_shadows(db) -> int:
    users = await db.trade_decisions.distinct("user_id", {"status": "deferred", "shadow": {"$exists": False}})
    total = 0
    for uid in users:
        try:
            total += (await resolve_deferred_outcomes(db, uid))["resolved"]
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred shadow sweep failed user=%s: %s", uid, e)
    legacy_users = await db.trade_decisions.distinct(
        "user_id", {"status": "deferred", "shadow": {"$exists": True}, "shadow.timeframe": {"$exists": False}})
    for uid in legacy_users:
        try:
            total += (await rescore_legacy_shadows(db, uid))["rescored"]
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred shadow rescore failed user=%s: %s", uid, e)
    return total
