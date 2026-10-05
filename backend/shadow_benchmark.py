"""Phase 1.2 — Continuous Shadow Benchmark.

Compares strategy variants automatically over the same window:
  current_production   — executed trades under the current strategy versions
  previous_production  — executed trades under legacy strategy versions
  experimental_ai      — production + high-conviction intercepted setups
                         (confidence ≥ 70) replayed against actual bars
  rule_baseline        — every deterministic proposal executed (production
                         + ALL intercepted setups replayed): pure rule-based
Metrics: EV (avg R), win rate, profit factor, max drawdown (R), plus
false-positive / false-negative rates for the production decision policy.
"""
from datetime import datetime, timedelta, timezone

from ablation import replay_outcome
from versioning import STRATEGY_VERSIONS


def variant_metrics(rs: list) -> dict | None:
    rs = [r for r in rs if r is not None]
    if not rs:
        return None
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    equity = peak = max_dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {"n": len(rs), "ev_r": round(sum(rs) / len(rs), 3),
            "net_r": round(sum(rs), 2),
            "win_rate": round(len(wins) / len(rs) * 100, 1),
            "profit_factor": (round(gross_win / gross_loss, 2)
                              if gross_loss else None),
            "max_drawdown_r": round(max_dd, 2)}


async def _executed_rs(db, user_id: str, since: str) -> list:
    """Realized R per executed trade. Uses risk_amount when stamped;
    otherwise normalizes by the median absolute losing P&L (≈1R proxy)."""
    raw = []
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed", "origin": "auto",
             "closed_at": {"$gte": since}, "pnl": {"$ne": None}},
            {"pnl": 1, "risk_amount": 1, "versions": 1,
             "closed_at": 1}).sort("closed_at", 1):
        raw.append({"pnl": float(t["pnl"]),
                    "risk": float(t.get("risk_amount") or 0),
                    "version": (t.get("versions") or {}).get(
                        "strategy_version")})
    losses = sorted(abs(t["pnl"]) for t in raw
                    if t["pnl"] < 0 and t["risk"] <= 0)
    proxy = losses[len(losses) // 2] if losses else None
    out = []
    for t in raw:
        risk = t["risk"] if t["risk"] > 0 else proxy
        if not risk:
            continue
        out.append({"r": t["pnl"] / risk, "version": t["version"]})
    return out


async def replay_rejections(db, user_id: str, since: str,
                            limit: int = 1000) -> list:
    rejections = await db.trade_decisions.find(
        {"user_id": user_id, "status": "rejected", "ts": {"$gte": since},
         "signal.entry_price": {"$exists": True}}).to_list(limit)
    cache: dict = {}
    out = []
    for d in rejections:
        sig = d.get("signal") or {}
        entry, sl = sig.get("entry_price"), sig.get("stop_loss")
        action = sig.get("action")
        if not entry or not sl or action not in ("BUY", "SELL"):
            continue
        sym = d["symbol"]
        if sym not in cache:
            doc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": sym, "timeframe": "M15"})  # fix plan A1
            cache[sym] = (doc or {}).get("bars") or []
        try:
            epoch = datetime.fromisoformat(d["ts"]).timestamp()
        except ValueError:
            continue
        bars = [b for b in cache[sym] if float(b.get("t") or 0) > epoch]
        oc = replay_outcome(action, float(entry), float(sl),
                            float(sig.get("take_profit") or 0), bars)
        if oc is None:
            continue
        out.append({"r": oc["r"], "outcome": oc["outcome"],
                    "confidence": sig.get("confidence"),
                    "stage": d.get("stage")})
    return out


async def benchmark(db, user_id: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    executed = await _executed_rs(db, user_id, since)
    rejected = await replay_rejections(db, user_id, since)

    current_versions = set(STRATEGY_VERSIONS.values())
    cur = [t["r"] for t in executed
           if t["version"] is None or t["version"] in current_versions]
    prev = [t["r"] for t in executed
            if t["version"] and t["version"] not in current_versions]
    high_conv = [d["r"] for d in rejected
                 if (d.get("confidence") or 0) >= 70]
    all_rej = [d["r"] for d in rejected]

    fp = len([r for r in cur if r < 0])
    fn = len([d for d in rejected if d["outcome"] == "tp_first"])
    variants = {
        "current_production": variant_metrics(cur),
        "previous_production": variant_metrics(prev),
        "experimental_ai": variant_metrics(cur + high_conv),
        "rule_baseline": variant_metrics(cur + all_rej),
    }
    return {"days": days, "variants": variants,
            "decision_quality": {
                "false_positive_rate": (round(fp / len(cur) * 100, 1)
                                        if cur else None),
                "false_negative_rate": (round(fn / len(rejected) * 100, 1)
                                        if rejected else None),
                "executed": len(cur), "intercepted_replayed": len(rejected)},
            "definitions": {
                "experimental_ai": "production + intercepted setups with "
                                   "confidence ≥ 70 replayed on actual bars",
                "rule_baseline": "every deterministic proposal executed "
                                 "(production + ALL intercepts replayed)"}}
