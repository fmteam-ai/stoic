"""v1.60.36 — Strategy Report (Loss Lab / Analytics): evidence BEFORE anyone changes a setting.

Per engine, over closed exact-P&L auto trades of the window, with mirrored accounts collapsed to ONE decision per
signal (pnl = per-account average):
  * predicted Monte-Carlo chance (signal.monte_carlo.p_tp_first) vs the ACTUAL win rate, in probability bands;
  * win rate grouped by WHERE IN THE DAY'S RANGE the entry was (signal.session_feats.range_pos_pct):
    0–10 % · 10–20 % · 20–40 % · 40 %+  — the "sold too close to the low" check;
  * results WITH vs WITHOUT trend-ride (trade.trend_ride_applied / signal.trend_ride).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from deferred_shadow import LABEL_VERSION, ROLLOVER_BAND, UTC_BANDS, cluster_deferred, utc_band
from strategy_engines import ENGINE_LABELS

SESSION_ORDER = [b[2] for b in UTC_BANDS] + [ROLLOVER_BAND, "Weekend"]

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
                    "trend_ride": ride, "closed_at": rows[0].get("closed_at"),
                    "utc_band": utc_band(rows[0].get("opened_at"))})
    return out


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Two-sided 95 % Wilson score interval for a win rate, in percent."""
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (round(100.0 * max(0.0, (centre - margin) / denom), 1), round(100.0 * min(1.0, (centre + margin) / denom), 1))


def _bucket_rows(decisions: list, key_fn, order: list) -> list:
    acc: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "pred": [], "accounts": 0})
    for d in decisions:
        b = acc[key_fn(d)]
        b["n"] += 1
        b["wins"] += d["win"]
        b["pnl"] += d["pnl"]
        b["accounts"] += d["mirrored_accounts"]
        if d["mc_p"] is not None:
            b["pred"].append(d["mc_p"])
    rows = []
    for label in order + [k for k in acc if k not in order]:
        if label not in acc:
            continue
        b = acc[label]
        lo, hi = wilson_interval(b["wins"], b["n"])
        rows.append({"band": label, "n": b["n"], "wins": b["wins"], "trades": b["accounts"],
                     "win_rate": round(100.0 * b["wins"] / b["n"], 1), "win_rate_ci": [lo, hi],
                     "predicted_win_rate": round(100.0 * sum(b["pred"]) / len(b["pred"]), 1) if b["pred"] else None,
                     "pnl": round(b["pnl"], 2), "avg_pnl": round(b["pnl"] / b["n"], 2)})
    return rows


def deferred_rows(deferred: list) -> list:
    """Shadow outcomes of DEFERRED signals per UTC band: n ideas (one per symbol+direction cluster within 15 min —
    v1.60.39, not every re-evaluation), re-evaluations, n labelled, shadow win rate (tp_first) + CI.
    v1.60.40 — only M1-based verdicts count as labelled; M15 verdicts (too coarse for scalp stops) are reported
    separately as `awaiting_m1` and never enter the win rate."""
    acc: dict = defaultdict(lambda: {"n": 0, "re_evaluations": 0, "labelled": 0, "tp": 0, "sl": 0, "r": 0.0,
                                     "awaiting_m1": 0, "ambiguous": 0, "invalid": 0})
    for d in cluster_deferred(deferred):
        band = (d.get("shadow") or {}).get("utc_band") or utc_band(d.get("ts"))
        b = acc[band]
        b["n"] += 1
        b["re_evaluations"] += int(d.get("re_evaluations") or 1)
        sh = d.get("shadow") or {}
        if sh.get("outcome") not in ("tp_first", "sl_first", "timeout"):
            continue
        # v1.60.49 (#36) — labels below LABEL_VERSION were produced with HOLD replayed as SELL (#33) and broker-time
        # ticks bucketed as UTC (#34): every one of them is invalid. They are counted, never scored.
        if sh.get("invalid") or int(sh.get("label_version") or 1) < LABEL_VERSION:
            b["invalid"] += 1
            continue
        if sh.get("timeframe") != "M1" or sh.get("coarse"):
            b["awaiting_m1"] += 1            # M15 verdicts (incl. v1.60.50 direction-corrected re-scores) never enter the rate
            continue
        b["labelled"] += 1
        b["tp"] += sh["outcome"] == "tp_first"
        b["sl"] += sh["outcome"] == "sl_first"
        b["r"] += float(sh.get("r") or 0)
        b["ambiguous"] += bool(sh.get("ambiguous"))
    rows = []
    for label in SESSION_ORDER + [k for k in acc if k not in SESSION_ORDER]:
        if label not in acc:
            continue
        b = acc[label]
        lo, hi = wilson_interval(b["tp"], b["labelled"])
        rows.append({"band": label, "n": b["n"], "re_evaluations": b["re_evaluations"], "labelled": b["labelled"],
                     "tp_first": b["tp"], "sl_first": b["sl"],
                     "win_rate": round(100.0 * b["tp"] / b["labelled"], 1) if b["labelled"] else None,
                     "win_rate_ci": [lo, hi] if b["labelled"] else None,
                     "avg_r": round(b["r"] / b["labelled"], 2) if b["labelled"] else None,
                     "awaiting_m1": b["awaiting_m1"],
                     "ambiguous": b["ambiguous"], "invalid": b["invalid"]})
    return rows


def build_report(decisions: list, window_days: int, deferred: list | None = None) -> dict:
    by_engine: dict = defaultdict(list)
    for d in decisions:
        by_engine[d["engine"]].append(d)
    by_engine_deferred: dict = defaultdict(list)
    for d in deferred or []:
        eng = (d.get("signal") or {}).get("scope") or "unknown"
        by_engine_deferred[eng].append(d)
        by_engine.setdefault(eng, [])
    engines = []
    mc_order = [b[2] for b in MC_BANDS]
    range_order = [b[2] for b in RANGE_BANDS]
    for eng, rows in sorted(by_engine.items(), key=lambda kv: -(len(kv[1]) + len(by_engine_deferred.get(kv[0], [])))):
        n = len(rows)
        wins = sum(r["win"] for r in rows)
        lo, hi = wilson_interval(wins, n)
        engines.append({
            "engine": eng, "label": ENGINE_LABELS.get(eng, eng), "decisions": n,
            "trades": sum(r["mirrored_accounts"] for r in rows),
            "win_rate": round(100.0 * wins / n, 1) if n else None, "win_rate_ci": [lo, hi] if n else None,
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            # v1.60.38 — executed decisions by entry UTC band (Asian hours is its own band) + deferred shadow outcomes
            "sessions": _bucket_rows(rows, lambda d: d.get("utc_band") or "n/a", SESSION_ORDER),
            "deferred_shadow": deferred_rows(by_engine_deferred.get(eng, [])),
            "mc_bands": _bucket_rows(rows, lambda d: _band(d["mc_p"], MC_BANDS), mc_order),
            "range_position": _bucket_rows(rows, lambda d: _band(d["range_pos_pct"], RANGE_BANDS), range_order),
            "trend_ride": _bucket_rows(rows, lambda d: "with trend-ride" if d["trend_ride"] else "without",
                                       ["with trend-ride", "without"]),
        })
    return {"window_days": window_days, "decisions": len(decisions), "deferred": len(deferred or []),
            "deferred_ideas": len(cluster_deferred(deferred or [])),
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
    deferred = await db.trade_decisions.find(
        {"user_id": user_id, "status": "deferred", "ts": {"$gte": since}},
        {"ts": 1, "symbol": 1, "signal.scope": 1, "signal.action": 1, "signal.symbol": 1, "shadow": 1}).to_list(length=5000)
    report = build_report(decisions_from(trades, signals), days, deferred)
    from risk_regime import bucket_evaluation
    report["risk_off_buckets"] = bucket_evaluation(trades)      # v1.60.53 (B6) — shadow evaluation, advisory
    return report
