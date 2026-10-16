"""Export the USD news events gold_trend_pulse NEWS MODE trades (CPI, Core CPI, NFP, FOMC, rate decision, Fed Chair,
PCE, GDP, Retail Sales, PPI, ISM) from the LIVE Finnhub economic calendar (actual / forecast / previous) for the replay.
READ-ONLY: Finnhub GET requests only — no DB access, no orders. Runs inside the deployed backend image, where the key
is staged as FINNHUB_API_KEY or FINNHUB_API_KEY_FILE (the entrypoint stages the Docker secret):

  docker compose exec backend python scripts/gtp_news_events.py --months 6 --out /tmp/news_events.json [--csv-out /tmp/news_events.csv]
  docker compose exec backend python scripts/gtp_replay.py --csv /tmp/XAUUSD_M1.csv --tz-offset 3 --days 186 --news-events /tmp/news_events.json

History is fetched in ≤ 31-day chunks (Finnhub caps long ranges). Without a key the script explains the manual JSON
shape: [{"title": "CPI m/m", "when": "2026-05-13T12:30:00+00:00", "actual": "0.3", "forecast": "0.2"}, …]."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHUNK_DAYS = 31


async def fetch_range(start: date, end: date) -> list | None:
    """Finnhub calendar rows between start and end (inclusive), chunked. None when the key is missing / endpoint unavailable."""
    from providers import finnhub
    if not finnhub.configured():
        return None
    out: list = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=CHUNK_DAYS - 1))
        payload = await finnhub._get("calendar/economic", {"from": cur.isoformat(), "to": stop.isoformat()})  # noqa: SLF001 — read-only GET
        if payload is None:
            if finnhub.status().get("unavailable"):
                return None
        else:
            out += finnhub.parse_calendar(payload)
        cur = stop + timedelta(days=1)
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: e["when_ts"]):
        key = (e["title"], int(e["when_ts"]))
        if key not in seen:
            seen.add(key); uniq.append(e)
    return uniq


def select_events(rows: list, kinds: list | None = None) -> list:
    """USD rows of the configured kinds → replay event shape (+ kind)."""
    from gold_trend_pulse import rules as R
    out = []
    for e in rows:
        if e.get("country") != "USD":
            continue
        kind = R.news_event_kind(e.get("title") or "", kinds)
        if not kind:
            continue
        out.append({"title": e["title"], "kind": kind, "when": e["when"], "when_ts": e["when_ts"], "actual": e.get("actual") or None,
                    "forecast": e.get("forecast") or None, "previous": e.get("previous") or None, "country": "USD", "impact": e.get("impact")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=6); ap.add_argument("--from", dest="start"); ap.add_argument("--to", dest="end")
    ap.add_argument("--out", default="news_events.json"); ap.add_argument("--csv-out")
    ap.add_argument("--kinds", nargs="*", help="subset of the default event kinds (e.g. CPI 'Nonfarm Payrolls' FOMC)")
    a = ap.parse_args()
    end = date.fromisoformat(a.end) if a.end else date.today() + timedelta(days=1)
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=a.months * 31)
    rows = asyncio.run(fetch_range(start, end))
    if rows is None:
        print("FINNHUB_API_KEY / FINNHUB_API_KEY_FILE not configured or calendar endpoint unavailable — write the JSON by hand: "
              "[{\"title\": \"CPI m/m\", \"when\": \"2026-05-13T12:30:00+00:00\", \"actual\": \"0.3\", \"forecast\": \"0.2\"}, …]")
        return 2
    out = select_events(rows, a.kinds)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    if a.csv_out:
        with open(a.csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["when", "kind", "title", "actual", "forecast", "previous", "impact"], extrasaction="ignore")
            w.writeheader(); w.writerows(out)
    kinds = sorted({x["kind"] for x in out})
    released = sum(1 for x in out if x["actual"] is not None)
    print(f"{len(out)} USD events {start} → {end} ({', '.join(kinds)}), {released} with an actual → {a.out}" + (f" + {a.csv_out}" if a.csv_out else ""))
    print("READ-ONLY: Finnhub GET only, nothing written to the DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
