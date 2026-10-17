"""gold_trend_pulse replay — XAUUSD M1 with a costs model. READ-ONLY: no DB writes, no orders, no EA commands.
Runs inside the deployed backend image (WORKDIR /app/backend):

  docker compose exec backend python scripts/gtp_replay.py --csv /tmp/XAUUSD_M1.csv --tz-offset 3 --days 30 \\
      --spread 0.25 --commission 3.5 --out /tmp/gtp_30d.md --trades-csv /tmp/gtp_30d_trades.csv
  … --window 2026-10-16T13:15 2026-10-16T14:30                       # item 13: per-M5-close trace (UTC)
  … --days 186 --news-events /tmp/news_events.json                    # item 14 (see scripts/gtp_news_events.py)
  python scripts/gtp_replay.py --user <user_id> [--days 30]           # DB READ (EA M1 feed, else scalp tick store)

--csv accepts the MT5 History export (`<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`, tab- or
comma-separated, broker time) or `iso_ts,o,h,l,c[,v]`. --tz-offset is the broker clock offset in hours (UTC+3 → 3) and is
SUBTRACTED so sessions / news times are evaluated in UTC. Output: markdown (stdout or --out) and optionally a trades CSV."""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="MT5 M1 export (broker time) or iso_ts,o,h,l,c[,v]"); ap.add_argument("--user", help="DB read: user_id")
    ap.add_argument("--days", type=int, default=30, help="keep the last N days of bars (0 = all)")
    ap.add_argument("--tz-offset", type=float, default=0.0, help="broker clock offset in HOURS, subtracted (UTC+3 → 3)")
    ap.add_argument("--offset-sec", type=int, default=0, help="additional offset in seconds (legacy)")
    ap.add_argument("--spread", type=float, default=0.25, help="spread in PRICE units (0.25 = 25 points on a 2-digit feed)")
    ap.add_argument("--commission", type=float, default=0.0, help="USD per lot per side")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--layering", action="store_true"); ap.add_argument("--no-runner", action="store_true")
    ap.add_argument("--news-events", help="JSON [{title, when|when_ts, actual, forecast}] — item 14 replay (scripts/gtp_news_events.py)")
    ap.add_argument("--news-pre-action", default="protect", choices=["protect", "close", "tighten"])
    ap.add_argument("--news-release-min", type=int, default=5)
    ap.add_argument("--window", nargs=2, metavar=("START", "END"), help="ISO UTC, e.g. 2026-10-16T13:15 2026-10-16T14:30 — per-M5-close trace")
    ap.add_argument("--out", help="write the markdown report here (default stdout)")
    ap.add_argument("--trades-csv", help="write every replayed trade as CSV")
    a = ap.parse_args()
    from datetime import datetime, timezone
    from gold_trend_pulse.replay import load_csv, load_news_events, markdown, replay, write_trades_csv
    if a.csv:
        bars = load_csv(a.csv)
        src = f"CSV {os.path.basename(a.csv)}"
    elif a.user:
        bars, _ = asyncio.run(_from_db(a.user, a.days))
        src = f"DB user {a.user}"
    else:
        ap.error("--csv or --user required")
    if not bars:
        print("no XAUUSD M1 coverage — export M1 from MT5 (History Center / Export) and pass --csv"); return 2
    offset = (int(a.tz_offset * 3600) + a.offset_sec) if a.csv else 0        # #70 — DB bars are already UTC
    if a.days:
        cutoff = bars[-1]["t"] - a.days * 86400
        bars = [b for b in bars if b["t"] >= cutoff]
    win = None
    if a.window:
        win = tuple(datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp() for x in a.window)
    news = load_news_events(a.news_events) if a.news_events else None
    rep = replay(bars, spread=a.spread, commission_usd_per_lot_side=a.commission, equity=a.equity, broker_offset_sec=offset, layering=a.layering,
                 runner=not a.no_runner, news_events=news, news_pre_action=a.news_pre_action, news_release_min=a.news_release_min, trace_window=win)
    first = datetime.fromtimestamp(bars[0]["t"] - offset, tz=timezone.utc)
    last = datetime.fromtimestamp(bars[-1]["t"] - offset, tz=timezone.utc)
    spreads = [b["s"] for b in bars if b.get("s") is not None]
    head = [f"source: {src} · {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC (tz-offset −{a.tz_offset:g} h) · spread {a.spread} · "
            f"commission ${a.commission}/lot/side · equity ${a.equity:,.0f}" + (" · layering" if a.layering else "") + ("" if a.no_runner else " · sweep runner ON")
            + (f" · news events {len(news)}" if news else "")]
    if spreads:
        head.append(f"CSV spread column: median {statistics.median(spreads):g} points, p90 {sorted(spreads)[int(len(spreads) * 0.9)]:g} points "
                    f"(2-digit feed: 25 points = 0.25 price) — set --spread accordingly")
    head.append("READ-ONLY replay: no DB writes, no orders.")
    md = markdown(rep, "gold_trend_pulse replay") + "\n" + "\n".join(head) + "\n"
    if a.out:
        with open(a.out, "w") as f:
            f.write(md)
        print(f"report → {a.out}")
    else:
        print(md)
    if a.trades_csv:
        print(f"{write_trades_csv(rep, a.trades_csv)} trades → {a.trades_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
