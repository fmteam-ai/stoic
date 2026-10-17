"""Spec A — prediction track record: every BUY/SELL decision (executed, rejected, advisory) is a prediction
"from the entry, price reaches TP before SL"; scored against the M15 candles that followed. Pure functions +
one storage sync. Never modifies trade_decisions."""
from __future__ import annotations

import math
from datetime import datetime, timezone

COLLECTION = "prediction_outcomes"
HORIZON_BARS = 96
BAR_SEC = 900
FINAL = ("tp", "sl", "expired", "no_data")
CONF_EDGES = [0, 50, 60, 70, 80, 90, 100]
PROB_EDGES = [round(i / 10, 1) for i in range(11)]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _levels(decision: dict):
    sig = decision.get("signal") or {}
    action = str(sig.get("action") or "").upper()
    entry, sl, tp = _num(sig.get("entry_price")), _num(sig.get("stop_loss")), _num(sig.get("take_profit"))
    if action not in ("BUY", "SELL") or None in (entry, sl, tp) or min(entry, sl, tp) <= 0:
        return None
    if action == "BUY" and not (tp > entry > sl):
        return None
    if action == "SELL" and not (tp < entry < sl):
        return None
    return {"action": action, "entry": entry, "stop_loss": sl, "take_profit": tp}


def _ts(decision: dict):
    try:
        return datetime.fromisoformat(str(decision.get("ts")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def resolve(decision: dict, bars: list, horizon: int = HORIZON_BARS) -> dict:
    lv, ts = _levels(decision), _ts(decision)
    if lv is None or ts is None:
        return {"outcome": "no_data", "reason": "missing or inconsistent levels"}
    if not bars:
        return {"outcome": "pending", "bars": 0, "reason": "no candles stored yet"}
    if bars[0]["t"] > ts + BAR_SEC:
        return {"outcome": "no_data", "reason": "decision predates the stored candles"}
    after = [b for b in bars if b["t"] >= ts][:horizon]
    buy = lv["action"] == "BUY"
    for k, b in enumerate(after, start=1):
        sl_hit = (b["l"] <= lv["stop_loss"]) if buy else (b["h"] >= lv["stop_loss"])
        tp_hit = (b["h"] >= lv["take_profit"]) if buy else (b["l"] <= lv["take_profit"])
        if sl_hit:
            return {"outcome": "sl", "bars": k}
        if tp_hit:
            return {"outcome": "tp", "bars": k}
    if len(after) < horizon:
        return {"outcome": "pending", "bars": len(after)}
    d = 1.0 if buy else -1.0
    risk = abs(lv["entry"] - lv["stop_loss"]) or 1e-9
    return {"outcome": "expired", "bars": horizon, "drift_r": round(d * (after[-1]["c"] - lv["entry"]) / risk, 3)}


def wilson(hits: int, n: int, z: float = 1.96) -> list:
    if n <= 0:
        return [None, None]
    p = hits / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / den
    return [round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)]


def _buckets(rows, key, edges, scale_label):
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        last = hi == edges[-1]
        sel = [r for r in rows if r.get(key) is not None and (lo <= float(r[key]) < hi or (last and float(r[key]) == hi))]
        if not sel:
            continue
        hits = sum(1 for r in sel if r["outcome"] == "tp")
        out.append({"bucket": scale_label(lo, hi), "n": len(sel), "hits": hits, "hit_rate": round(hits / len(sel), 4),
                    "mean_predicted": round(sum(float(r[key]) for r in sel) / len(sel), 4), "ci95": wilson(hits, len(sel))})
    return out


def calibration(rows: list) -> dict:
    decided = [r for r in rows if r.get("outcome") in ("tp", "sl")]
    hits = sum(1 for r in decided if r["outcome"] == "tp")
    n = len(decided)
    probs = [r for r in decided if _num(r.get("p_tp_first")) is not None and 0 <= float(r["p_tp_first"]) <= 1]
    confs = [r for r in decided if _num(r.get("confidence")) is not None]
    by_status = {}
    for st in ("executed", "rejected", "advisory"):
        sel = [r for r in decided if r.get("status") == st]
        if sel:
            tp = sum(1 for r in sel if r["outcome"] == "tp")
            by_status[st] = {"n": len(sel), "tp": tp, "hit_rate": round(tp / len(sel), 4)}
    brier = round(sum((float(r["p_tp_first"]) - (1.0 if r["outcome"] == "tp" else 0.0)) ** 2 for r in probs) / len(probs), 4) if probs else None
    return {"decided": n, "hits": hits, "hit_rate": round(hits / n, 4) if n else None, "ci95": wilson(hits, n),
            "by_confidence": _buckets(confs, "confidence", CONF_EDGES, lambda lo, hi: f"{int(lo)}-{int(hi)}"),
            "by_probability": _buckets(probs, "p_tp_first", PROB_EDGES, lambda lo, hi: f"{lo:.1f}-{hi:.1f}"),
            "brier": brier, "by_status": by_status}


async def ensure_indexes(db) -> None:
    await db[COLLECTION].create_index([("user_id", 1), ("decision_id", 1)], unique=True)
    await db[COLLECTION].create_index([("user_id", 1), ("base", 1)])


async def sync_outcomes(db, user_id: str, symbol: str, scan_limit: int = 500) -> dict:
    from pip_utils import base_symbol
    base = base_symbol(symbol)
    decisions = await db.trade_decisions.find({"user_id": user_id, "status": {"$in": ["executed", "rejected", "advisory"]}},
                                              sort=[("ts", -1)]).to_list(length=scan_limit)
    decisions = [d for d in decisions if base_symbol(d.get("symbol")) == base]
    ids = [str(d["_id"]) for d in decisions]
    stored = {r["decision_id"] for r in await db[COLLECTION].find({"user_id": user_id, "decision_id": {"$in": ids}}, {"decision_id": 1}).to_list(length=len(ids) or 1)}
    cdoc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}) or {}
    bars = sorted([b for b in (cdoc.get("bars") or []) if all(k in b for k in ("t", "h", "l", "c"))], key=lambda b: b["t"])
    utc = cdoc.get("t_basis") == "utc"
    now = datetime.now(timezone.utc).isoformat()
    pending, final = [], 0
    for d in decisions:
        did = str(d["_id"])
        if did in stored:
            continue
        sig = d.get("signal") or {}
        lv = _levels(d) or {}
        res = resolve(d, bars) if utc else {"outcome": "pending", "bars": 0, "reason": "candle clock not UTC-aligned yet"}
        row = {"decision_id": did, "user_id": user_id, "base": base, "ts": d.get("ts"), "status": d.get("status"), "stage": d.get("stage"),
               "action": lv.get("action") or sig.get("action"), "entry": lv.get("entry"), "stop_loss": lv.get("stop_loss"), "take_profit": lv.get("take_profit"),
               "confidence": _num(sig.get("confidence")), "p_tp_first": _num((sig.get("mc") or {}).get("p_tp_first")), **res}
        if res["outcome"] in FINAL:
            row["resolved_at"] = now
            await db[COLLECTION].update_one({"user_id": user_id, "decision_id": did}, {"$set": row}, upsert=True)
            final += 1
        else:
            pending.append(row)
    last_bar = bars[-1]["t"] if bars else None
    as_of = datetime.fromtimestamp(float(last_bar), tz=timezone.utc).isoformat() if isinstance(last_bar, (int, float)) else (str(last_bar) if last_bar else None)
    return {"base": base, "scanned": len(decisions), "stored_new": final, "pending": pending,
            "as_of": as_of, "t_basis": cdoc.get("t_basis"), "bars_n": len(bars)}
