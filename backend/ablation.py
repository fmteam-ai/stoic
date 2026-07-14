"""iter-136 · Gate ablation study (quant roadmap #16).

For every gate rejection recorded in the decision ledger WITH a signal
snapshot (entry/SL/TP), replay the subsequent M15 bars and determine the
counterfactual outcome: would the vetoed trade have hit TP first, SL first,
or timed out? Aggregated per gate, this measures each component's actual
incremental value — a gate that mostly blocks would-be winners is costing
edge and should be re-examined.

Read-only: consumes trade_decisions + intraday_candles, changes nothing.
"""
from datetime import datetime, timedelta, timezone

HORIZON_BARS = 96  # 24h of M15 — same as the Monte Carlo horizon


def replay_outcome(action: str, entry: float, sl: float, tp: float,
                   bars: list) -> dict | None:
    """Walk bars after the veto: TP-first / SL-first / timeout (+R at end)."""
    if not bars or not entry or not sl:
        return None
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None
    is_buy = action == "BUY"
    for i, b in enumerate(bars[:HORIZON_BARS]):
        hi, lo = float(b["h"]), float(b["l"])
        sl_hit = (lo <= sl) if is_buy else (hi >= sl)
        tp_hit = bool(tp) and ((hi >= tp) if is_buy else (lo <= tp))
        if sl_hit and tp_hit:
            # both touched in one bar — conservative: assume SL first
            return {"outcome": "sl_first", "r": -1.0, "bars": i + 1}
        if sl_hit:
            return {"outcome": "sl_first", "r": -1.0, "bars": i + 1}
        if tp_hit:
            return {"outcome": "tp_first",
                    "r": round(abs(tp - entry) / sl_dist, 2), "bars": i + 1}
    last = float(bars[min(len(bars), HORIZON_BARS) - 1]["c"])
    r = ((last - entry) if is_buy else (entry - last)) / sl_dist
    return {"outcome": "timeout", "r": round(r, 2),
            "bars": min(len(bars), HORIZON_BARS)}


async def run_gate_ablation(db, user_id: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {"user_id": user_id, "status": "rejected", "ts": {"$gte": since},
         "signal.entry_price": {"$exists": True}}
    rejections = await db.trade_decisions.find(q).to_list(2000)

    # candle cache per symbol (one fetch each)
    candle_cache: dict = {}

    async def bars_after(symbol: str, ts_iso: str) -> list:
        if symbol not in candle_cache:
            doc = await db.intraday_candles.find_one(
                {"symbol": symbol, "timeframe": "M15"}) or \
                await db.intraday_candles.find_one({"symbol": symbol})
            candle_cache[symbol] = (doc or {}).get("bars") or []
        try:
            epoch = datetime.fromisoformat(ts_iso).timestamp()
        except ValueError:
            return []
        return [b for b in candle_cache[symbol] if float(b.get("t") or 0) > epoch]

    gates: dict = {}
    for d in rejections:
        sig = d.get("signal") or {}
        entry, sl = sig.get("entry_price"), sig.get("stop_loss")
        action = sig.get("action")
        if not entry or not sl or action not in ("BUY", "SELL"):
            continue
        bars = await bars_after(d["symbol"], d["ts"])
        oc = replay_outcome(action, float(entry), float(sl),
                            float(sig.get("take_profit") or 0), bars)
        if oc is None:
            continue
        g = gates.setdefault(d["stage"], {
            "gate": d["stage"], "replayed": 0, "would_win": 0, "would_lose": 0,
            "timeout": 0, "saved_r": 0.0, "blocked_r": 0.0, "net_r": 0.0})
        g["replayed"] += 1
        if oc["outcome"] == "sl_first":
            g["would_lose"] += 1
            g["saved_r"] += 1.0                # veto avoided a -1R loss
            g["net_r"] += 1.0
        elif oc["outcome"] == "tp_first":
            g["would_win"] += 1
            g["blocked_r"] += oc["r"]          # veto cost a winner
            g["net_r"] -= oc["r"]
        else:
            g["timeout"] += 1
            g["net_r"] -= oc["r"]              # sign of drift the veto skipped

    out = []
    for g in gates.values():
        for k in ("saved_r", "blocked_r", "net_r"):
            g[k] = round(g[k], 2)
        g["verdict"] = ("ADDS VALUE" if g["net_r"] > 0.5
                        else "COSTS EDGE" if g["net_r"] < -0.5
                        else "NEUTRAL")
        out.append(g)
    out.sort(key=lambda x: -x["net_r"])
    total_with_snapshot = len(rejections)
    return {"days": days, "gates": out,
            "rejections_with_snapshot": total_with_snapshot,
            "note": ("Counterfactual replay of vetoed setups against actual "
                     "subsequent M15 bars. net_r > 0 means the gate's vetoes "
                     "avoided more loss than they blocked in wins. Snapshot "
                     "coverage grows as the ledger accrues (started iter-134).")}
