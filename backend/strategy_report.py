"""v1.60.36 — Strategy Report (Loss Lab / Analytics): evidence BEFORE anyone changes a setting.

Per engine, over closed exact-P&L auto trades of the window, with mirrored accounts collapsed to ONE decision per
signal (pnl = per-account average):
  * predicted Monte-Carlo chance (signal.monte_carlo.p_tp_first) vs the ACTUAL win rate, in probability bands;
  * win rate grouped by WHERE IN THE DAY'S RANGE the entry was (signal.session_feats.range_pos_pct):
    0–10 % · 10–20 % · 20–40 % · 40 %+  — the "sold too close to the low" check;
  * results WITH vs WITHOUT trend-ride (trade.trend_ride_applied / signal.trend_ride).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from strategy_engines import ENGINE_LABELS

MC_BANDS = [(0.0, 0.3, "<30%"), (0.3, 0.4, "30–40%"), (0.4, 0.5, "40–50%"), (0.5, 0.6, "50–60%"), (0.6, 1.01, "60%+")]
RANGE_BANDS = [(0.0, 10.0, "0–10%"), (10.0, 20.0, "10–20%"), (20.0, 40.0, "20–40%"), (40.0, 1e9, "40%+")]


def _band(value, bands):
    if value is None:
        return "n/a"
    v = float(value)
    for lo, hi, label in bands:
        if lo <= v < hi:
            return label
    return "n/a"


def _engine_of(trade: dict, sig: dict) -> str:
    return trade.get("engine") or trade.get("scope") or sig.get("strategy_engine") or sig.get("scope") or "unknown"


def decisions_from(trades: list, signals: dict) -> list:
    """One row per decision = symbol + direction + entry within ±2 min (mirrored accounts averaged). Each account
    writes its own signal doc, so signal_id is NOT the grouping key; it only supplies MC / range-position features."""
    from decision_groups import decision_key, group_decisions
    out = []
    for rows in group_decisions(trades):
        sig = {}
        for r in rows:                                   # first row that has a stored signal supplies the features
            sig = signals.get(str(r.get("signal_id") or "")) or {}
            if sig:
                break
        pnls = [float(r.get("pnl") or 0) for r in rows]
        pnl = sum(pnls) / len(pnls)
        mc = (sig.get("monte_carlo") or {}).get("p_tp_first")
        feats = sig.get("session_feats") or {}
        ride = any(r.get("trend_ride_applied") or r.get("trend_ride") for r in rows) or bool(sig.get("trend_ride"))
        out.append({"key": decision_key(rows), "engine": _engine_of(rows[0], sig), "symbol": rows[0].get("symbol"),
                    "pnl": round(pnl, 2), "win": pnl > 0, "mirrored_accounts": len(rows),
                    "mc_p": None if mc is None else float(mc), "range_pos_pct": feats.get("range_pos_pct"),
                    "trend_ride": ride, "closed_at": rows[0].get("closed_at")})
    return out


def _bucket_rows(decisions: list, key_fn, order: list) -> list:
    acc: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "pred": []})
    for d in decisions:
        b = acc[key_fn(d)]
        b["n"] += 1
        b["wins"] += d["win"]
        b["pnl"] += d["pnl"]
        if d["mc_p"] is not None:
            b["pred"].append(d["mc_p"])
    rows = []
    for label in order + [k for k in acc if k not in order]:
        if label not in acc:
            continue
        b = acc[label]
        rows.append({"band": label, "n": b["n"], "wins": b["wins"],
                     "win_rate": round(100.0 * b["wins"] / b["n"], 1),
                     "predicted_win_rate": round(100.0 * sum(b["pred"]) / len(b["pred"]), 1) if b["pred"] else None,
                     "pnl": round(b["pnl"], 2), "avg_pnl": round(b["pnl"] / b["n"], 2)})
    return rows


def build_report(decisions: list, window_days: int) -> dict:
    by_engine: dict = defaultdict(list)
    for d in decisions:
        by_engine[d["engine"]].append(d)
    engines = []
    mc_order = [b[2] for b in MC_BANDS]
    range_order = [b[2] for b in RANGE_BANDS]
    for eng, rows in sorted(by_engine.items(), key=lambda kv: -len(kv[1])):
        n = len(rows)
        wins = sum(r["win"] for r in rows)
        engines.append({
            "engine": eng, "label": ENGINE_LABELS.get(eng, eng), "decisions": n,
            "trades": sum(r["mirrored_accounts"] for r in rows),
            "win_rate": round(100.0 * wins / n, 1), "pnl": round(sum(r["pnl"] for r in rows), 2),
            "mc_bands": _bucket_rows(rows, lambda d: _band(d["mc_p"], MC_BANDS), mc_order),
            "range_position": _bucket_rows(rows, lambda d: _band(d["range_pos_pct"], RANGE_BANDS), range_order),
            "trend_ride": _bucket_rows(rows, lambda d: "with trend-ride" if d["trend_ride"] else "without",
                                       ["with trend-ride", "without"]),
        })
    return {"window_days": window_days, "decisions": len(decisions),
            "trades": sum(d["mirrored_accounts"] for d in decisions), "engines": engines,
            "dedupe": "one decision per symbol + direction + entry within ±2 min across mirrored accounts; $ = per-account average",
            "generated_at": datetime.now(timezone.utc).isoformat()}


async def strategy_report(db, user_id: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "origin": "auto", "stats_excluded": {"$ne": True},
        "closed_at": {"$gte": since}, "pnl": {"$ne": None},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }).to_list(length=5000)
    sig_ids = []
    for t in trades:
        try:
            if t.get("signal_id"):
                sig_ids.append(ObjectId(str(t["signal_id"])))
        except Exception:  # noqa: BLE001
            pass
    signals = {}
    if sig_ids:
        async for s in db.signals.find({"_id": {"$in": sig_ids}},
                                       {"monte_carlo.p_tp_first": 1, "session_feats.range_pos_pct": 1,
                                        "trend_ride": 1, "strategy_engine": 1, "scope": 1}):
            signals[str(s["_id"])] = s
    return build_report(decisions_from(trades, signals), days)
