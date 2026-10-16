"""Export the USD news events gold_trend_pulse NEWS MODE trades (CPI, Core CPI, NFP, FOMC, rate decision, Fed Chair,
PCE, GDP, Retail Sales, PPI, ISM) from the Finnhub economic calendar (actual / forecast) for the replay:

  FINNHUB_API_KEY=… python scripts/gtp_news_events.py --months 6 --out news_events.json
  python scripts/gtp_replay.py --csv XAUUSD_M1.csv --days 180 --news-events news_events.json

Read-only. Without a key the script explains how to build the JSON by hand ([{title, when, actual, forecast}])."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _fetch(months: int) -> list | None:
    from providers import finnhub
    if not finnhub.configured():
        return None
    return await finnhub.fetch_calendar(days_back=months * 31, days_ahead=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6); ap.add_argument("--out", default="news_events.json")
    ap.add_argument("--kinds", nargs="*", help="subset of the default event kinds")
    a = ap.parse_args()
    from gold_trend_pulse import rules as R
    rows = asyncio.run(_fetch(a.months))
    if rows is None:
        print("FINNHUB_API_KEY not configured — write the JSON by hand: [{\"title\": \"CPI m/m\", \"when\": \"2026-05-13T12:30:00+00:00\", "
              "\"actual\": \"0.3\", \"forecast\": \"0.2\"}, …]")
        return 2
    out = []
    for e in rows:
        if e.get("country") != "USD":
            continue
        kind = R.news_event_kind(e.get("title") or "", a.kinds)
        if not kind:
            continue
        out.append({"title": e["title"], "kind": kind, "when": e["when"], "when_ts": e["when_ts"], "actual": e.get("actual") or None,
                    "forecast": e.get("forecast") or None, "country": "USD", "impact": e.get("impact")})
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"{len(out)} USD events ({', '.join(sorted({x['kind'] for x in out}))}) → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
