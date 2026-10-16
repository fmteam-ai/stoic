"""gold_trend_pulse replay — last N trading days of XAUUSD M1 with a costs model.

  python scripts/gtp_replay.py --csv XAUUSD_M1.csv [--spread 0.25] [--commission 3.5] [--equity 10000]
  python scripts/gtp_replay.py --user <user_id> [--days 30]          # DB: EA M1 feed, else scalp tick store
Prints a markdown table for the PR description. Read-only."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _from_db(user_id: str, days: int) -> tuple[list, int]:
    from datetime import datetime, timedelta, timezone
    from database import get_db
    from deferred_shadow import m1_bars_from_ticks
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": "XAUUSD", "timeframe": "M1"}, {"bars": 1})
    bars = list((doc or {}).get("bars") or [])
    if bars:
        return bars, 0
    start = (now - timedelta(days=days)).timestamp()
    buckets: dict = {}
    cur = db.scalp_ticks.find({"user_id": user_id, "symbol": {"$regex": "^XAUUSD"}, "last_ms": {"$gt": int(start * 1000)}},
                              {"ticks": 1, "broker_offset_sec": 1}).sort("first_ms", 1)
    async for b in cur:
        m1_bars_from_ticks(b.get("ticks") or [], start, now.timestamp(), int(b.get("broker_offset_sec") or 0), buckets=buckets)
    return [buckets[t] for t in sorted(buckets)], 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv"); ap.add_argument("--user"); ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--spread", type=float, default=0.25); ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--equity", type=float, default=10_000.0); ap.add_argument("--offset-sec", type=int, default=0)
    ap.add_argument("--layering", action="store_true")
    a = ap.parse_args()
    from gold_trend_pulse.replay import load_csv, markdown, replay
    if a.csv:
        bars = load_csv(a.csv)
    elif a.user:
        bars, _ = asyncio.run(_from_db(a.user, a.days))
    else:
        ap.error("--csv or --user required")
    if not bars:
        print("no XAUUSD M1 coverage — export M1 from MT5 (History Center) and pass --csv"); return 2
    if a.days:
        cutoff = bars[-1]["t"] - a.days * 86400
        bars = [b for b in bars if b["t"] >= cutoff]
    rep = replay(bars, spread=a.spread, commission_usd_per_lot_side=a.commission, equity=a.equity, broker_offset_sec=a.offset_sec, layering=a.layering)
    print(markdown(rep, f"gold_trend_pulse replay — spread {a.spread}, commission ${a.commission}/lot/side, equity ${a.equity:,.0f}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
