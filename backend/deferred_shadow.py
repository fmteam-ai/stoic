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
LABEL_VERSION = 2                           # v1.60.49 — #33 direction + #34 broker-offset fix; v1 labels are invalid


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


MAX_BROKER_OFFSET_SEC = 50400


def m1_bars_from_ticks(ticks: list, start_epoch: float, end_epoch: float, broker_offset_sec: int = 0,
                       buckets: dict | None = None) -> list:
    """Mid-price ticks ({tm ms, b, a}) → M1 OHLC bars [{t,o,h,l,c,n}] inside (start, end], sorted by t.
    v1.60.49 (#34) — `tm` is BROKER time (MT5 server clock, e.g. UTC+3): `broker_offset_sec` is subtracted so the
    bars sit on the UTC axis the decision `ts` lives on (labels were using prices from 3 h earlier)."""
    buckets = {} if buckets is None else buckets
    off = int(broker_offset_sec or 0)
    for tk in ticks:
        try:
            tm = int(tk["tm"]) / 1000.0 - off
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


async def _account_offsets(db, user_id: str) -> dict:
    """account_id → seconds to subtract from broker time (routes.bridge_routes.broker_offset_sec)."""
    from routes.bridge_routes import broker_offset_sec
    out: dict = {}
    async for acc in db.accounts.find({"user_id": user_id},
                                      {"broker_time_info": 1, "broker_utc_offset_sec": 1}):
        out[str(acc["_id"])] = broker_offset_sec(acc)[0]
    return out


async def m1_bars_after(db, user_id: str, base: str, epoch: float, offsets: dict | None = None) -> list:
    """M1 bars after `epoch` (≤ 24 h): EA M1 feed first, then the scalp tick store. [] when neither covers it."""
    end = epoch + HORIZON_SEC
    doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M1"}, {"bars": 1})
    bars = [b for b in ((doc or {}).get("bars") or []) if epoch < int(b.get("t") or 0) <= end]
    if bars:
        return bars
    if offsets is None:
        offsets = await _account_offsets(db, user_id)
    # first_ms/last_ms are broker time too — widen the window by the largest plausible offset, filter exactly below
    cur = db.scalp_ticks.find({"user_id": user_id, "symbol": {"$regex": f"^{re.escape(base)}"},
                               "last_ms": {"$gt": int((epoch - MAX_BROKER_OFFSET_SEC) * 1000)},
                               "first_ms": {"$lte": int((end + MAX_BROKER_OFFSET_SEC) * 1000)}},
                              {"ticks": 1, "account_id": 1, "broker_offset_sec": 1}).sort("first_ms", 1)
    buckets: dict = {}
    async for batch in cur:
        # v1.60.50 — offset recorded with the batch (DST-correct) beats the account's current offset
        off = batch.get("broker_offset_sec")
        if off is None:
            off = offsets.get(str(batch.get("account_id") or ""), 0)
        m1_bars_from_ticks(batch.get("ticks") or [], epoch, end, off, buckets)
    return [buckets[t] for t in sorted(buckets)]


def deferred_direction(sig: dict) -> str:
    """v1.60.49 (#33) — the Execution Optimizer rewrites `action` to HOLD when it defers; the pre-defer direction is
    kept in `deferred_action`. Older rows (233/233 stored HOLD) fall back to the geometry: TP above entry = BUY."""
    for k in ("deferred_action", "pre_defer_action"):
        a = str(sig.get(k) or "").upper()
        if a in ("BUY", "SELL"):
            return a
    a = str(sig.get("action") or "").upper()
    if a in ("BUY", "SELL"):
        return a
    try:
        entry = float(sig.get("entry_price") or 0)
        tp = float(sig.get("take_profit") or sig.get("tp3") or sig.get("tp1") or 0)
        sl = float(sig.get("stop_loss") or 0)
        if entry and tp and tp != entry:
            return "BUY" if tp > entry else "SELL"
        if entry and sl and sl != entry:
            return "BUY" if sl < entry else "SELL"
    except (TypeError, ValueError):
        pass
    return "HOLD"


def _coverage_sec(bars: list, epoch: float) -> float:
    return (int(bars[-1]["t"]) - epoch) if bars else 0.0


async def resolve_deferred_outcomes(db, user_id: str, limit: int = 300) -> dict:
    rows = await db.trade_decisions.find(
        {"user_id": user_id, "status": "deferred", "shadow": {"$exists": False},
         "signal.entry_price": {"$exists": True}, "signal.stop_loss": {"$exists": True}},
        {"symbol": 1, "ts": 1, "signal": 1}).sort("ts", 1).to_list(length=limit)
    m15_cache: dict = {}
    offsets = await _account_offsets(db, user_id)
    resolved = pending = skipped = 0
    by_tf: dict = defaultdict(int)
    for d in rows:
        sig = d.get("signal") or {}
        base = base_symbol(d.get("symbol") or sig.get("symbol") or "")
        epoch = _epoch(d.get("ts"))
        if epoch is None:
            skipped += 1
            continue
        action = deferred_direction(sig)
        entry, sl = float(sig.get("entry_price") or 0), float(sig.get("stop_loss") or 0)
        tp = float(sig.get("take_profit") or sig.get("tp3") or sig.get("tp1") or 0)

        sim, tf = None, None
        m1 = await m1_bars_after(db, user_id, base, epoch, offsets)
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
            {"$set": {"shadow": {**sim, "timeframe": tf, "direction": action, "label_version": LABEL_VERSION,
                                 "resolved_at": datetime.now(timezone.utc).isoformat(),
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
        key = (base_symbol(d.get("symbol") or sig.get("symbol") or ""), deferred_direction(sig))
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


def _legacy_q(user_id: str) -> dict:
    """Shadows that predate LABEL_VERSION (no timeframe, or no label_version / older one)."""
    return {"user_id": user_id, "status": "deferred", "shadow": {"$exists": True},
            "$or": [{"shadow.timeframe": {"$exists": False}}, {"shadow.label_version": {"$exists": False}},
                    {"shadow.label_version": {"$lt": LABEL_VERSION}}]}


async def _m15_after(db, user_id: str, base: str, epoch: float, cache: dict) -> list:
    if base not in cache:
        doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}, {"bars": 1})
        cache[base] = (doc or {}).get("bars") or []
    return [b for b in cache[base] if int(b.get("t") or 0) > epoch]


async def rescore_legacy_shadows(db, user_id: str, limit: int = 200, *, rescore_all: bool = False,
                                 after_id=None) -> dict:
    """v1.60.39 — shadows stamped before the M1 replay existed are re-run on M1 bars.
    v1.60.49 — every label below LABEL_VERSION (HOLD replayed as SELL #33, broker-time ticks #34) is re-scored.
    v1.60.50 (PR #100 review) — paginates by `_id` (`after_id` → `next_after_id`), so a run visits every row once;
    an existing `rescored_from` is never overwritten (the ORIGINAL label is kept); a valid v2 label is never
    downgraded to invalid; rows without M1 coverage are re-scored on M15 with the corrected direction (flagged
    `coarse`) and only marked invalid when no bars exist at all."""
    q = {"user_id": user_id, "status": "deferred", "shadow": {"$exists": True}} if rescore_all else _legacy_q(user_id)
    if after_id is not None:
        q["_id"] = {"$gt": after_id}
    rows = await db.trade_decisions.find(q, {"symbol": 1, "ts": 1, "signal": 1, "shadow": 1}).sort("_id", 1).to_list(length=limit)
    offsets = await _account_offsets(db, user_id)
    m15_cache: dict = {}
    rescored = tagged = kept = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for d in rows:
        sig = d.get("signal") or {}
        base = base_symbol(d.get("symbol") or sig.get("symbol") or "")
        epoch = _epoch(d.get("ts"))
        action = deferred_direction(sig)
        old = d.get("shadow") or {}
        origin = old.get("rescored_from") or {"timeframe": old.get("timeframe") or "M15", "outcome": old.get("outcome"),
                                              "r": old.get("r"), "label_version": old.get("label_version") or 1,
                                              "direction": old.get("direction")}
        entry, sl = float(sig.get("entry_price") or 0), float(sig.get("stop_loss") or 0)
        tp = float(sig.get("take_profit") or sig.get("tp3") or sig.get("tp1") or 0)
        sim, tf = None, None
        if epoch is not None and action in ("BUY", "SELL"):
            m1 = await m1_bars_after(db, user_id, base, epoch, offsets)
            if m1:
                sim, tf = replay_outcome(action, entry, sl, tp, m1, horizon_bars=M1_HORIZON_BARS), "M1"
                if sim and sim["outcome"] == "timeout" and _coverage_sec(m1, epoch) < HORIZON_SEC - 900:
                    sim = None
            if sim is None:
                after = await _m15_after(db, user_id, base, epoch, m15_cache)
                if after:
                    sim, tf = replay_outcome(action, entry, sl, tp, after), "M15"
                    if sim and sim["outcome"] == "timeout" and len(after) < HORIZON_BARS:
                        sim = None
        if sim:
            new = {**sim, "timeframe": tf, "direction": action, "label_version": LABEL_VERSION,
                   "resolved_at": now_iso, "utc_band": old.get("utc_band") or utc_band(d.get("ts")),
                   "rescored_from": origin}
            if tf == "M15":
                new["coarse"] = True             # corrected direction, but M15 resolution — reported separately
            await db.trade_decisions.update_one({"_id": d["_id"]}, {"$set": {"shadow": new}})
            rescored += 1
        elif int(old.get("label_version") or 1) >= LABEL_VERSION and not old.get("invalid"):
            kept += 1                            # valid v2 label whose tape has since expired — never downgrade
        else:
            await db.trade_decisions.update_one({"_id": d["_id"]}, {"$set": {
                "shadow.timeframe": old.get("timeframe") or "M15", "shadow.label_version": LABEL_VERSION,
                "shadow.invalid": True, "shadow.invalid_reason": "no bars after the decision — pre-v2 label unverifiable",
                "shadow.rescored_from": origin}})
            tagged += 1
    return {"rescored": rescored, "tagged_m15": tagged, "kept": kept, "visited": len(rows),
            "next_after_id": rows[-1]["_id"] if rows else None}


async def sweep_deferred_shadows(db) -> int:
    users = await db.trade_decisions.distinct("user_id", {"status": "deferred", "shadow": {"$exists": False}})
    total = 0
    for uid in users:
        try:
            total += (await resolve_deferred_outcomes(db, uid))["resolved"]
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred shadow sweep failed user=%s: %s", uid, e)
    legacy_users = await db.trade_decisions.distinct("user_id", {k: v for k, v in _legacy_q("").items() if k != "user_id"})
    for uid in legacy_users:
        try:
            total += (await rescore_legacy_shadows(db, uid))["rescored"]
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred shadow rescore failed user=%s: %s", uid, e)
    return total
