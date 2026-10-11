"""Prediction track record — "show previous predictions" + calibration.

Every BUY/SELL candidate in `trade_decisions` (executed AND rejected) is a
prediction: "from entry, price reaches take-profit before stop-loss". This
module scores each one against the M15 candles that FOLLOWED it:

  tp       take-profit reached first
  sl       stop-loss reached first (also when both sit inside one bar —
           the pessimistic call, same rule as the backtesters)
  expired  neither within HORIZON_BARS (24h of M15)
  pending  not enough candles yet — never stored, re-checked next time
  no_data  the decision predates the stored candle window or lacks levels

Final outcomes go to `prediction_outcomes` (one per decision; the decision
ledger itself stays immutable), so calibration keeps its history after the
~8-day candle window has rolled past.

Calibration answers "when the bot says X, how often is it right?":
  • by `confidence` bucket (a 0-100 score — the chart shows whether it
    behaves like a probability at all);
  • by Monte-Carlo `p_tp_first` (a real probability) with Brier score.
Rejected decisions are scored too, so the record also shows whether the
gates blocked losers or winners.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pip_utils import base_symbol

logger = logging.getLogger(__name__)

COLLECTION = "prediction_outcomes"
HORIZON_BARS = 96
BAR_SEC = 900
FINAL = ("tp", "sl", "expired", "no_data")


def _ts(iso: str) -> float | None:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def _levels(decision: dict):
    s = decision.get("signal") or {}
    try:
        action = s.get("action")
        entry, sl, tp = float(s["entry_price"]), float(s["stop_loss"]), float(s["take_profit"])
    except (KeyError, TypeError, ValueError):
        return None
    if action not in ("BUY", "SELL") or min(entry, sl, tp) <= 0:
        return None
    d = 1 if action == "BUY" else -1
    if not (d * (tp - entry) > 0 and d * (entry - sl) > 0):   # levels on the wrong side
        return None
    return action, entry, sl, tp


def resolve(decision: dict, bars: list, horizon: int = HORIZON_BARS) -> dict:
    """Score one decision on bars that open at or after its timestamp."""
    lv, ts = _levels(decision), _ts(decision.get("ts"))
    if lv is None or ts is None:
        return {"outcome": "no_data", "reason": "missing or inconsistent levels"}
    action, entry, sl, tp = lv
    if not bars:
        return {"outcome": "pending", "reason": "no candles stored yet"}
    if bars[0]["t"] > ts + BAR_SEC:
        return {"outcome": "no_data", "reason": "decision predates the stored candles"}
    after = [b for b in bars if b["t"] >= ts][:horizon]
    d = 1 if action == "BUY" else -1
    for k, b in enumerate(after, 1):
        hit_sl = b["l"] <= sl if d > 0 else b["h"] >= sl
        hit_tp = b["h"] >= tp if d > 0 else b["l"] <= tp
        if hit_sl:
            return {"outcome": "sl", "bars": k}
        if hit_tp:
            return {"outcome": "tp", "bars": k}
    if len(after) < horizon:
        return {"outcome": "pending", "bars": len(after)}
    last = after[-1]["c"]
    return {"outcome": "expired", "bars": horizon, "drift_r": round(d * (last - entry) / abs(entry - sl), 3)}


def wilson(hits: int, n: int, z: float = 1.96) -> tuple:
    if n <= 0:
        return (None, None)
    p = hits / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / den
    return (round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4))


def _bucket_rows(items, edges, fmt):
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [it for it in items if lo <= it[0] < hi or (hi == edges[-1] and it[0] == hi)]
        n = len(sel)
        hits = sum(1 for _, hit in sel if hit)
        lo_ci, hi_ci = wilson(hits, n)
        rows.append({"bucket": fmt(lo, hi), "n": n, "hits": hits,
                     "hit_rate": round(hits / n, 4) if n else None,
                     "mean_predicted": round(sum(p for p, _ in sel) / n, 4) if n else None,
                     "ci95": [lo_ci, hi_ci]})
    return rows


def calibration(outcomes: list) -> dict:
    """outcomes: stored rows with outcome ∈ {tp, sl} (expired / no_data are excluded)."""
    decided = [o for o in outcomes if o.get("outcome") in ("tp", "sl")]
    conf = [(float(o["confidence"]), o["outcome"] == "tp") for o in decided
            if isinstance(o.get("confidence"), (int, float))]
    prob = [(float(o["p_tp_first"]), o["outcome"] == "tp") for o in decided
            if isinstance(o.get("p_tp_first"), (int, float)) and 0 <= o["p_tp_first"] <= 1]
    brier = round(sum((p - hit) ** 2 for p, hit in prob) / len(prob), 4) if prob else None
    by_status: dict = {}
    for o in decided:
        s = by_status.setdefault(o.get("status") or "unknown", {"n": 0, "tp": 0})
        s["n"] += 1
        s["tp"] += o["outcome"] == "tp"
    for s in by_status.values():
        s["hit_rate"] = round(s["tp"] / s["n"], 4) if s["n"] else None
    hits = sum(1 for o in decided if o["outcome"] == "tp")
    return {
        "decided": len(decided), "hits": hits,
        "hit_rate": round(hits / len(decided), 4) if decided else None,
        "ci95": list(wilson(hits, len(decided))),
        "by_confidence": _bucket_rows(conf, [0, 50, 60, 70, 80, 90, 100], lambda a, b: f"{a}-{b}"),
        "by_probability": _bucket_rows(prob, [i / 10 for i in range(11)], lambda a, b: f"{a:.1f}-{b:.1f}"),
        "brier": brier,
        # rejected rows hitting TP often = the gates are blocking winners
        "by_status": by_status,
    }


async def sync_outcomes(db, user_id: str, symbol: str, scan_limit: int = 500) -> dict:
    """Resolve recent decisions for this symbol; persist the final ones. Returns {decision_id: result}."""
    base = base_symbol(symbol)
    decisions = await db.trade_decisions.find(
        {"user_id": user_id, "status": {"$in": ["executed", "rejected", "advisory"]}}
    ).sort("_id", -1).to_list(scan_limit)
    decisions = [d for d in decisions if base_symbol(d.get("symbol") or "") == base]
    ids = [str(d["_id"]) for d in decisions]
    stored = {o["decision_id"]: o for o in await db[COLLECTION].find(
        {"user_id": user_id, "decision_id": {"$in": ids}}).to_list(len(ids) or 1)}
    cdoc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base, "timeframe": "M15"}) or {}
    bars = sorted(({"t": int(b["t"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])}
                   for b in cdoc.get("bars") or [] if all(k in b for k in ("t", "h", "l", "c"))),
                  key=lambda b: b["t"])
    # a broker-clock (not yet UTC-aligned) feed would misplace every decision in time
    clock_ok = cdoc.get("t_basis") == "utc"
    out = {}
    now = datetime.now(timezone.utc).isoformat()
    for d in decisions:
        did = str(d["_id"])
        if did in stored:
            out[did] = stored[did]
            continue
        res = resolve(d, bars) if clock_ok else {"outcome": "pending", "reason": "candle clock not UTC-aligned yet"}
        sig = d.get("signal") or {}
        row = {"decision_id": did, "user_id": user_id, "base": base, "ts": d.get("ts"),
               "status": d.get("status"), "stage": d.get("stage"), "action": sig.get("action"),
               "entry": sig.get("entry_price"), "stop_loss": sig.get("stop_loss"),
               "take_profit": sig.get("take_profit"), "confidence": sig.get("confidence"),
               "p_tp_first": (sig.get("mc") or {}).get("p_tp_first"), **res}
        if res["outcome"] in FINAL:
            row["resolved_at"] = now
            await db[COLLECTION].update_one({"user_id": user_id, "decision_id": did},
                                            {"$set": row}, upsert=True)
        out[did] = row
    return out


async def track_record(db, user_id: str, symbol: str, limit: int = 20) -> dict:
    """Last `limit` predictions with their outcomes, plus calibration over all stored history."""
    base = base_symbol(symbol)
    recent = await sync_outcomes(db, user_id, base)
    rows = sorted(recent.values(), key=lambda r: r.get("ts") or "", reverse=True)
    history = await db[COLLECTION].find({"user_id": user_id, "base": base}).to_list(5000)
    for r in rows + history:
        r.pop("_id", None)
    return {"symbol": base, "horizon_bars": HORIZON_BARS,
            "recent": rows[:max(1, min(limit, 200))],
            "calibration": calibration(history),
            "pending": sum(1 for r in rows if r.get("outcome") == "pending")}
