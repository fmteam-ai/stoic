"""iter-60 · Quant Agent range forecast — ATR-projected daily range.

Blocks trades whose TP1 sits beyond the room the day statistically has left
(projected ATR14 range minus the range already used today)."""

MIN_REMAINING_FRACTION = 0.15  # the day can always stretch a bit further


def _atr14(daily_history: list) -> float | None:
    bars = [b for b in (daily_history or [])
            if b.get("high") is not None and b.get("low") is not None
            and b.get("close") is not None]
    if len(bars) < 15:
        return None
    trs = []
    for i in range(len(bars) - 14, len(bars)):
        h, l = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def build_range_forecast(daily_history: list, intraday_bars: list | None = None) -> dict | None:
    atr = _atr14(daily_history)
    if not atr:
        return None
    out = {"atr14": round(atr, 5), "projected_range": round(atr, 5)}
    bars = intraday_bars or []
    if bars:
        # bars of the current broker day = same epoch-day as the latest bar
        last_day = int(bars[-1].get("t") or 0) // 86400
        today = [b for b in bars if int(b.get("t") or 0) // 86400 == last_day]
        if today:
            used = max(b["h"] for b in today) - min(b["l"] for b in today)
            remaining = max(atr - used, atr * MIN_REMAINING_FRACTION)
            out["used_range"] = round(used, 5)
            out["remaining_range"] = round(remaining, 5)
            out["used_pct"] = round(100 * used / atr, 1)
    return out


def range_gate(action: str, entry, tp1, forecast: dict | None) -> str | None:
    if action not in ("BUY", "SELL") or not forecast:
        return None
    remaining = forecast.get("remaining_range")
    if remaining is None or not entry or not tp1:
        return None
    tp_dist = abs(float(tp1) - float(entry))
    if tp_dist <= remaining:
        return None
    return (f"Range gate: TP1 is {tp_dist:.1f} away but the day has only "
            f"~{remaining:.1f} of its ATR14 range left "
            f"({forecast.get('used_pct', '?')}% already used) — target "
            f"statistically unreachable today. Vetoed.")
