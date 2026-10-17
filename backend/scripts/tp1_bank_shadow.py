#!/usr/bin/env python3
"""v1.60.60 #72 — READ-ONLY shadow recompute: replay last week's closed intraday-gold trades under
"TP1 bank + protected runner" and compare with the fixed-TP result that actually happened.

Usage (inside the backend image, no writes, no orders):
    python scripts/tp1_bank_shadow.py --days 7 [--user <user_id>] [--fraction 0.5] [--offset 1.0] [--tp2 2.0] [--out /tmp/tp1_shadow.md]

Bars: M1 from db.candles (timeframe M1, symbol XAU*) between entry and close. Trades without bar coverage are reported
as "no data" and excluded from the comparison. Prints a Markdown table.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import exit_policy_tp1 as X  # noqa: E402

ENGINES = X.ENGINES


def simulate(trade: dict, bars: list[dict], fraction: float, offset: float, tp2_mult: float) -> dict | None:
    """Walk M1 bars: TP1 → bank fraction, SL → entry±offset, trail runner on rolling 5-bar M5-ish swing; close at TP2/SL/last bar."""
    action, entry = trade["action"], float(trade["entry_price"])
    sl, tp1 = float(trade.get("original_stop_loss") or trade["stop_loss"]), float(trade.get("original_take_profit") or trade["take_profit"])
    plan = X.plan_targets(entry, sl, tp1, action, tp2_mult)
    lot = float(trade.get("original_lot_size") or trade.get("lot_size") or 0.01)
    pv, pip = float(trade.get("pip_value_usd_per_lot") or 1.0), float(trade.get("pip_size") or 0.1)
    sign = 1 if action == "BUY" else -1

    def pnl(px, vol):
        return (px - entry) * sign / pip * pv * vol

    banked = 0.0
    keep, cur_sl, banked_done = lot, sl, False
    m5: list[dict] = []
    for i, b in enumerate(bars):
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
        if not banked_done:
            if (lo <= sl) if action == "BUY" else (hi >= sl):
                return {"policy_pnl": pnl(sl, lot), "banked": 0.0, "runner": pnl(sl, lot), "exit": "sl"}
            if (hi >= tp1) if action == "BUY" else (lo <= tp1):
                bp = X.bank_plan({**trade, "lot_size": lot, "stop_loss": sl}, tp1, close_fraction=fraction, offset_price=offset, tp2=plan["tp2"])
                banked, keep, cur_sl, banked_done = pnl(tp1, bp["close_volume"]), bp["keep_volume"], bp["new_sl"], True
                continue
        else:
            if (lo <= cur_sl) if action == "BUY" else (hi >= cur_sl):
                return {"policy_pnl": banked + pnl(cur_sl, keep), "banked": banked, "runner": pnl(cur_sl, keep), "exit": "runner_sl"}
            if (hi >= plan["tp2"]) if action == "BUY" else (lo <= plan["tp2"]):
                return {"policy_pnl": banked + pnl(plan["tp2"], keep), "banked": banked, "runner": pnl(plan["tp2"], keep), "exit": "tp2"}
            if i % 5 == 4:
                m5.append({"high": max(float(x["high"]) for x in bars[i - 4:i + 1]), "low": min(float(x["low"]) for x in bars[i - 4:i + 1]), "close": cl})
                new = X.trail_sl(action, cur_sl, cl, m5)
                if new is not None:
                    cur_sl = new
    last = float(bars[-1]["close"])
    return {"policy_pnl": banked + pnl(last, keep), "banked": banked, "runner": pnl(last, keep), "exit": "time_stop/last_bar"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--user")
    ap.add_argument("--fraction", type=float, default=0.5)
    ap.add_argument("--offset", type=float, default=1.0)
    ap.add_argument("--tp2", type=float, default=2.0)
    ap.add_argument("--out")
    a = ap.parse_args()
    from database import get_db
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat()
    q = {"status": "closed", "closed_at": {"$gte": since}, "symbol": {"$regex": "^XAU"}, "$or": [{"scope": {"$in": list(ENGINES)}}, {"engine": {"$in": list(ENGINES)}}]}
    if a.user:
        q["user_id"] = a.user
    trades = await db.trades.find(q).sort("closed_at", 1).to_list(length=500)
    lines = [f"# TP1 bank + runner — shadow recompute ({a.days} d, fraction {a.fraction}, offset ${a.offset}, TP2 ×{a.tp2})", "",
             "| closed | engine | side | fixed TP P&L | policy P&L | banked | runner | exit |", "|---|---|---|---|---|---|---|---|"]
    fixed_total = policy_total = 0.0
    n = nodata = 0
    for t in trades:
        try:
            t0, t1 = datetime.fromisoformat(str(t["opened_at"]).replace("Z", "+00:00")), datetime.fromisoformat(str(t["closed_at"]).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        bars = await db.candles.find({"symbol": t["symbol"], "timeframe": "M1", "time": {"$gte": t0.isoformat(), "$lte": (t1 + timedelta(hours=2)).isoformat()}}) \
            .sort("time", 1).to_list(length=5000)
        if len(bars) < 3 or not t.get("stop_loss") or not t.get("take_profit"):
            nodata += 1
            continue
        sim = simulate(t, bars, a.fraction, a.offset, a.tp2)
        if sim is None:
            nodata += 1
            continue
        fixed = float(t.get("pnl") or 0)
        fixed_total += fixed
        policy_total += sim["policy_pnl"]
        n += 1
        lines.append(f"| {str(t['closed_at'])[:16]} | {t.get('scope') or t.get('engine')} | {t['action']} | {fixed:+.2f} | {sim['policy_pnl']:+.2f} | "
                     f"{sim['banked']:+.2f} | {sim['runner']:+.2f} | {sim['exit']} |")
    lines += ["", f"**{n} trades compared** ({nodata} without M1 coverage excluded) — fixed TP {fixed_total:+.2f} vs tp1_bank_runner {policy_total:+.2f} "
              f"(Δ {policy_total - fixed_total:+.2f}). Read-only: nothing written, no orders."]
    text = "\n".join(lines)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
