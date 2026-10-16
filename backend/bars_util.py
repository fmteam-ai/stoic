"""Shared M1/M5 bar helpers — resample, MT5 History-export loader, synthetic generator. Used by the #55 live-feature
engines / replay and by gold_trend_pulse. Pure functions, no I/O beyond reading the CSV path given."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone


def resample(bars: list, seconds: int) -> list:
    """OHLC bars [{t,o,h,l,c,(v)}] (any lower TF, UTC epoch t) → bars of `seconds`."""
    out: dict = {}
    for b in bars:
        key = int(b["t"]) // seconds * seconds
        g = out.get(key)
        if g is None:
            out[key] = {"t": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or b.get("n") or 0, "n": 1}
        else:
            g["h"] = max(g["h"], b["h"]); g["l"] = min(g["l"], b["l"]); g["c"] = b["c"]; g["v"] += b.get("v") or b.get("n") or 0; g["n"] += 1
    return [out[k] for k in sorted(out)]


def m5_from_m1(m1: list) -> list:
    return resample(m1, 300)


def load_csv(path: str) -> list:
    """XAUUSD M1 bars from (a) an MT5 History export — tab- or comma-separated, header `<DATE> <TIME> <OPEN> <HIGH> <LOW>
    <CLOSE> <TICKVOL> <VOL> <SPREAD>` (header skipped, `2026.10.16` or `2026-10-16` dates, `13:15` / `13:15:00`) — or (b)
    `iso_ts,o,h,l,c[,v]`. Times are taken verbatim (broker clock) — shift with replay(broker_offset_sec=…) /
    `--tz-offset`. Spread (points) is kept as `s` when the column exists."""
    bars = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or not line[:1].isdigit():
                continue
            row = line.split("\t") if "\t" in line else [x.strip() for x in line.split(",")]
            try:
                if len(row) >= 6 and ":" in row[1]:
                    ts = datetime.strptime(f"{row[0].replace('-', '.')} {row[1]}", "%Y.%m.%d %H:%M:%S" if row[1].count(":") == 2 else "%Y.%m.%d %H:%M")
                    o, h, l, c = map(float, row[2:6])
                    v = float(row[6] or 0) if len(row) > 6 else 0.0
                    s = float(row[8]) if len(row) > 8 and row[8] else None
                else:
                    ts = datetime.fromisoformat(row[0]); o, h, l, c = map(float, row[1:5]); v = float(row[5] or 0) if len(row) > 5 else 0.0; s = None
            except (ValueError, IndexError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            b = {"t": int(ts.timestamp()), "o": o, "h": h, "l": l, "c": c, "v": v}
            if s is not None:
                b["s"] = s
            bars.append(b)
    return bars


def synthetic_m1(days: int = 30, seed: int = 7, start_price: float = 4000.0) -> list:
    rnd = random.Random(seed)
    bars = []
    px = start_price
    t0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    for d in range(days):
        day = t0 + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        mode = ["up", "down", "range"][d % 3]
        drift = {"up": 0.012, "down": -0.012, "range": 0.0}[mode]
        center = px
        for m in range(1440):
            t = day + timedelta(minutes=m)
            hour = t.hour + t.minute / 60
            vol = 0.18 if 7 <= hour < 20 else 0.06
            if mode == "range":
                pull = (center - px) * 0.02
                step = pull + rnd.gauss(0, vol) + 0.6 * math.sin(m / 23.0) * vol
            else:
                wave = 0.9 * math.sin(m / 37.0) * vol            # pullbacks inside the trend
                step = drift + wave + rnd.gauss(0, vol)
            o = px
            c = px + step
            h = max(o, c) + abs(rnd.gauss(0, vol * 0.6))
            l = min(o, c) - abs(rnd.gauss(0, vol * 0.6))
            bars.append({"t": int(t.timestamp()), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 1 + rnd.randint(0, 9)})
            px = c
    return bars
