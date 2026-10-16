"""Synthetic XAUUSD M1 generator for gold_trend_pulse tests / replay smoke (clearly NOT market data)."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone


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
