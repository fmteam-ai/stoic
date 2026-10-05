"""Tier 6 — Digital Twin: per-account shadow copy.

Every gate-rejected decision (recorded in the trade_decisions ledger with a
signal snapshot) is replayed against the M15 bars that actually followed.
The twin shows what would have happened WITHOUT the gates — per account —
so the system learns "what if" without risking capital.
"""
from datetime import datetime, timedelta, timezone

from ablation import replay_outcome


def _median(vals: list) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


async def twin_summary(db, user_id: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    live: dict = {}
    risk_samples: dict = {}
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed", "origin": "auto",
             "closed_at": {"$gte": since}, "pnl": {"$ne": None}},
            {"account_id": 1, "pnl": 1, "risk_amount": 1}):
        acc = str(t.get("account_id") or "unknown")
        e = live.setdefault(acc, {"trades": 0, "wins": 0, "losses": 0,
                                  "pnl": 0.0})
        pnl = float(t["pnl"])
        e["trades"] += 1
        e["pnl"] += pnl
        if pnl > 0:
            e["wins"] += 1
        elif pnl < 0:
            e["losses"] += 1
        r = float(t.get("risk_amount") or 0)
        if r > 0:
            risk_samples.setdefault(acc, []).append(r)

    user_risk = _median([r for v in risk_samples.values() for r in v])

    rejections = await db.trade_decisions.find(
        {"user_id": user_id, "status": "rejected", "ts": {"$gte": since},
         "signal.entry_price": {"$exists": True}}).to_list(2000)

    candle_cache: dict = {}

    async def bars_after(symbol: str, ts_iso: str) -> list:
        if symbol not in candle_cache:
            doc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": symbol, "timeframe": "M15"})  # fix plan A1
            candle_cache[symbol] = (doc or {}).get("bars") or []
        try:
            epoch = datetime.fromisoformat(ts_iso).timestamp()
        except ValueError:
            return []
        return [b for b in candle_cache[symbol]
                if float(b.get("t") or 0) > epoch]

    twin: dict = {}
    divergences = []
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
        acc = str(d.get("account_id") or "unknown")
        e = twin.setdefault(acc, {"replayed": 0, "would_win": 0,
                                  "would_lose": 0, "timeout": 0,
                                  "alt_r": 0.0})
        e["replayed"] += 1
        e["alt_r"] += oc["r"]
        if oc["outcome"] == "tp_first":
            e["would_win"] += 1
        elif oc["outcome"] == "sl_first":
            e["would_lose"] += 1
        else:
            e["timeout"] += 1
        divergences.append({"ts": d["ts"], "symbol": d["symbol"],
                            "action": action, "stage": d.get("stage"),
                            "r": oc["r"], "outcome": oc["outcome"],
                            "account_id": acc})
    divergences.sort(key=lambda x: -abs(x["r"]))

    labels = {}
    async for a in db.accounts.find({"user_id": user_id},
                                    {"label": 1, "broker": 1}):
        labels[str(a["_id"])] = a.get("label") or a.get("broker")

    accounts = []
    for acc in sorted(set(live) | set(twin)):
        lv = live.get(acc, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        tw = twin.get(acc, {"replayed": 0, "would_win": 0, "would_lose": 0,
                            "timeout": 0, "alt_r": 0.0})
        risk = _median(risk_samples.get(acc) or []) or user_risk
        alt_pnl = round(tw["alt_r"] * risk, 2) if risk else None
        verdict = ("NO INTERCEPTS" if not tw["replayed"]
                   else "GATES PROTECTING" if tw["alt_r"] < -0.5
                   else "GATES COSTING EDGE" if tw["alt_r"] > 0.5
                   else "NEUTRAL")
        accounts.append({
            "account_id": acc, "label": labels.get(acc),
            "live": {**lv, "pnl": round(lv["pnl"], 2),
                     "win_rate": (round(lv["wins"] / lv["trades"] * 100, 1)
                                  if lv["trades"] else None)},
            "twin": {**tw, "alt_r": round(tw["alt_r"], 2),
                     "alt_pnl_est": alt_pnl,
                     "twin_pnl_est": (round(lv["pnl"] + alt_pnl, 2)
                                      if alt_pnl is not None else None),
                     "risk_per_trade_est": risk},
            "verdict": verdict})

    return {"days": days, "accounts": accounts,
            "totals": {"live_pnl": round(sum(a["live"]["pnl"]
                                             for a in accounts), 2),
                       "alt_r": round(sum(a["twin"]["alt_r"]
                                          for a in accounts), 2),
                       "intercepts_replayed": sum(a["twin"]["replayed"]
                                                  for a in accounts)},
            "top_divergences": divergences[:8],
            "note": ("Alternative world replayed from the decision ledger "
                     "against actual subsequent M15 bars. alt_r < 0 means "
                     "the gates avoided losses; alt_r > 0 means intercepted "
                     "setups would have won.")}
