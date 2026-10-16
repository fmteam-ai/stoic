"""gold_trend_pulse · replay / backtest on XAUUSD M1 bars with a costs model (spread + commission).
Sources: --csv (MT5 History export: date,time,open,high,low,close[,tickvol]) or the DB (EA M1 feed, else M1 built
from the scalp tick store). Bias is recomputed every 15 min from M15-resampled bars exactly as the engine does."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from gold_trend_pulse import rules as R

PIP_PRICE = 0.10


def load_csv(path: str) -> list:
    bars = []
    with open(path, newline="") as f:
        for row in csv.reader(f, delimiter="\t" if path.endswith(".tsv") else ","):
            if not row or not row[0][:1].isdigit():
                continue
            try:
                if len(row) >= 7 and ":" in row[1]:
                    ts = datetime.strptime(f"{row[0]} {row[1]}", "%Y.%m.%d %H:%M:%S" if row[1].count(":") == 2 else "%Y.%m.%d %H:%M")
                    o, h, l, c = map(float, row[2:6]); v = float(row[6] or 0)
                else:
                    ts = datetime.fromisoformat(row[0]); o, h, l, c = map(float, row[1:5]); v = float(row[5] or 0) if len(row) > 5 else 0
            except (ValueError, IndexError):
                continue
            bars.append({"t": int(ts.replace(tzinfo=timezone.utc).timestamp()), "o": o, "h": h, "l": l, "c": c, "v": v})
    return bars


def replay(m1: list, *, spread: float = 0.25, commission_usd_per_lot_side: float = 0.0, equity: float = 10_000.0,
           pip_usd_per_lot: float = 10.0, broker_offset_sec: int = 0, layering: bool = False) -> dict:
    """Walk M1 bars (UTC after offset) and emulate the engine: bias every 15 min with hysteresis, regime every M5
    (TREND / RANGE / NO_TRADE), TREND entries on M5 closes (single or layered basket), RANGE entries on M1 closes,
    cooldowns, daily stop, sessions, spike filter, TP/SL clamps, BE, time-stops, breakout hand-over."""
    if broker_offset_sec:
        m1 = [{**b, "t": int(b["t"]) - broker_offset_sec} for b in m1]
    m1 = sorted(m1, key=lambda b: b["t"])
    m15 = R.resample(m1, 900)
    m5_all = R.resample(m1, 300)
    comm_price = 2 * commission_usd_per_lot_side / pip_usd_per_lot * PIP_PRICE
    tracker = R.BiasTracker()
    trades: list = []
    positions: list = []                      # open legs / trades (several only when layering)
    basket: dict | None = None
    baskets: list = []
    i15 = 0
    skips = defaultdict(int)
    regime_name, rng = "NO_TRADE", None
    regime_minutes = defaultdict(lambda: defaultdict(int))
    day_key = None
    day_rows: list = []

    def _close_all(bar, now, reason, only=None):
        nonlocal positions
        keep = []
        for pos in positions:
            if only and not only(pos["direction"]):
                keep.append(pos); continue
            lab = R_label_step(pos, {**bar, "h": bar["c"], "l": bar["c"]}, now, force=reason)
            trades.append(lab); day_rows.append(lab)
            if pos.get("basket"):
                pos["basket"]["closed"].append(lab)
        positions = keep

    for bar in m1:
        t = int(bar["t"])
        now = datetime.fromtimestamp(t + 60, tz=timezone.utc)
        if day_key != now.date():
            day_key, day_rows = now.date(), []
        regime_minutes[str(now.date())][regime_name] += 1
        # --- manage open positions on this M1 bar
        still = []
        for pos in positions:
            lab = R_label_step(pos, bar, now)
            if lab:
                trades.append(lab); day_rows.append(lab)
                if pos.get("basket"):
                    pos["basket"]["closed"].append(lab)
            else:
                still.append(pos)
        positions = still
        if basket and not positions and not basket.get("pending"):
            baskets.append({"legs": basket["closed"]}); basket = None
        m5_close = (t + 60) % 300 == 0
        while i15 < len(m15) and m15[i15]["t"] + 900 <= t + 60:
            i15 += 1
        hist15 = m15[max(0, i15 - 800):i15]
        if m5_close:
            m5 = [b for b in m5_all if b["t"] + 300 <= t + 60][-60:]
            if tracker.due(now) and len(hist15) >= 200:
                d0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                today = [b for b in hist15 if b["t"] >= d0]
                prior = [b for b in hist15 if d0 - 86400 <= b["t"] < d0]
                tracker.update(R.daily_bias(R.resample(hist15, 14400), R.resample(hist15, 3600), bar["c"],
                                            today[0]["o"] if today else None,
                                            (max(b["h"] for b in prior) + min(b["l"] for b in prior)) / 2 if prior else None), now)
            prev_regime, prev_rng = regime_name, rng
            reg = R.regime(tracker.bias, m5, hist15, t + 60) if len(m5) >= 25 else {"regime": "NO_TRADE", "range": None}
            regime_name, rng = reg["regime"], reg["range"]
            if prev_regime == "RANGE" and regime_name != "RANGE" and prev_rng and m5:
                brk = R.range_breakout(m5[-1], prev_rng)
                if brk:
                    _close_all(bar, now, "range_breakout", only=lambda d: R.wrong_side(d, brk))
            if basket:
                inval = R.basket_invalidated(basket["direction"], tracker.bias, hist15[-1]["c"] if hist15 else None, basket["shared_stop"])
                if inval:
                    _close_all(bar, now, "basket_invalidated")
                    baskets.append({"legs": basket["closed"]}); basket = None
        else:
            m5 = None
        if regime_name == "NO_TRADE":
            skips["no_trade"] += 1 if m5_close else 0; continue
        if positions and not (layering and regime_name == "TREND" and basket):
            continue
        ok, _ = R.session_ok(now)
        if not ok:
            continue
        consec = 0
        for r in reversed(day_rows):
            if r["r"] <= 0:
                consec += 1
            else:
                break
        if R.daily_stop(consec, sum(r["pnl_usd"] for r in day_rows) / equity * 100, len(day_rows)):
            skips["daily_stop"] += 1 if m5_close else 0; continue
        if day_rows:
            until = R.cooldown_until("loss" if day_rows[-1]["r"] <= 0 else "win", datetime.fromisoformat(day_rows[-1]["exit_at"]))
            if until and now < until:
                continue
        d0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        # ---------------- RANGE: M1 edge rejection
        if regime_name == "RANGE":
            cand = R.detect_range_entry(bar, rng)
            if not cand:
                continue
            plan = R.plan_range_exit(cand["direction"], cand["entry"], rng)
            if not plan["ok"]:
                skips["range_sl_too_wide"] += 1; continue
            if not R.spread_ok(spread, plan["tp"])[0]:
                skips["spread"] += 1; continue
            sz = R.size_lots(equity, plan["sl"], pip_usd_per_lot, PIP_PRICE)
            if not sz["ok"]:
                skips["sizing"] += 1; continue
            positions.append(_pos(cand, plan, spread, comm_price, sz["lots"], now, "RANGE", "range", "NONE", plan["time_stop_min"]))
            continue
        # ---------------- TREND: M5 close
        if not m5_close or not m5 or len(m5) < 25:
            continue
        vwap = R.session_vwap(m5, d0)
        if layering:
            direction = "BUY" if tracker.bias == "UP" else "SELL"
            ema20, a14 = R.ema([b["c"] for b in m5], 20)[-1], R.atr(m5, 14)
            if basket is None:
                stop = R.basket_shared_stop(direction, [b for b in hist15 if b["t"] >= d0])
                if stop is None:
                    continue
                basket = {"direction": direction, "shared_stop": stop, "levels": R.layer_levels(direction, ema20, vwap, a14),
                          "done": set(), "closed": [], "risk_used": 0.0, "pending": True}
            pending = [(leg, lvl) for leg, lvl in basket["levels"] if leg not in basket["done"]]
            hit = next(((leg, lvl) for leg, lvl in pending if R.leg_touched(direction, m5[-1], lvl)), None)
            if hit is None:
                if not basket["closed"] and not positions:
                    basket = None                                # nothing touched yet → re-evaluate next bar
                continue
            leg, lvl = hit
            basket["done"].add(leg)
            basket["pending"] = len(basket["done"]) < len(basket["levels"])
            if len(day_rows) + len(positions) >= R.DAILY_MAX_LEGS:
                skips["daily_legs"] += 1; continue
            entry = m5[-1]["c"]
            tp = max(R.TP_MIN, min(R.TP_MAX, R.TP_ATR_MULT * a14))
            sgn = 1 if direction == "BUY" else -1
            plan = {"ok": True, "tp": round(tp, 2), "sl": round(abs(entry - basket["shared_stop"]), 2), "take_profit": round(entry + sgn * tp, 2),
                    "stop_loss": basket["shared_stop"], "time_stop_min": R.TIME_STOP_MIN}
            if not R.spread_ok(spread, plan["tp"])[0]:
                skips["spread"] += 1; continue
            sz = R.size_basket_leg(equity, entry, basket["shared_stop"], pip_usd_per_lot, PIP_PRICE, basket["risk_used"])
            if not sz["ok"]:
                skips["basket_risk"] += 1; continue
            basket["risk_used"] += sz["risk_pct"]
            pos = _pos({"direction": direction, "entry": entry}, plan, spread, comm_price, sz["lots"], now, "TREND", leg, tracker.bias, R.TIME_STOP_MIN)
            pos["basket"] = basket
            positions.append(pos)
            continue
        cand = R.detect_entry(m5, tracker.bias, vwap)
        if not cand:
            continue
        if cand.get("blocked"):
            skips["spike"] += 1; continue
        plan = R.plan_exit(cand["direction"], cand["entry"], cand["swing"], cand["atr"])
        if not plan["ok"]:
            skips["sl_too_wide"] += 1; continue
        if not R.spread_ok(spread, plan["tp"])[0]:
            skips["spread"] += 1; continue
        sz = R.size_lots(equity, plan["sl"], pip_usd_per_lot, PIP_PRICE)
        if not sz["ok"]:
            skips["sizing"] += 1; continue
        positions.append(_pos(cand, plan, spread, comm_price, sz["lots"], now, "TREND", cand["trigger"], tracker.bias, R.TIME_STOP_MIN))
    if basket:
        baskets.append({"legs": basket["closed"]})
    rep = summarize(trades, skips, m1)
    rep["by_mode"] = {k: R.outcome_stats([x for x in trades if x["regime"] == k]) for k in ("TREND", "RANGE")}
    rep["regime_minutes_per_day"] = {d: dict(v) for d, v in sorted(regime_minutes.items())}
    rep["baskets"] = R.basket_stats([b for b in baskets if b["legs"]], equity) if layering else None
    return rep


def _pos(cand, plan, spread, comm_price, lots, now, regime_name, trigger, bias, ts_min):
    return {"direction": cand["direction"], "entry": cand["entry"], "take_profit": plan["take_profit"], "stop_loss": plan["stop_loss"],
            "tp_dist": plan["tp"], "sl_dist": plan["sl"], "spread_at_entry": spread, "commission_price": comm_price, "lots": lots,
            "created_at": now.isoformat(), "trigger": trigger, "bias": bias, "regime": regime_name, "time_stop_min": ts_min,
            "mfe": 0.0, "mae": 0.0, "sl_live": plan["stop_loss"]}


def R_label_step(pos: dict, bar: dict, now: datetime, force: str | None = None) -> dict | None:
    """One M1 bar of position management (same rules as engine.label_with_bars)."""
    sgn = 1 if pos["direction"] == "BUY" else -1
    entry, tp = pos["entry"], pos["take_profit"]
    be = R.breakeven_level(pos["direction"], entry, pos["spread_at_entry"], pos["commission_price"])
    hi, lo = (bar["h"] - entry) * sgn, (bar["l"] - entry) * sgn
    pos["mfe"], pos["mae"] = max(pos["mfe"], hi), min(pos["mae"], lo)
    sl = pos["sl_live"]
    opened = datetime.fromisoformat(pos["created_at"])
    hit_sl = (bar["l"] <= sl) if sgn > 0 else (bar["h"] >= sl)
    hit_tp = (bar["h"] >= tp) if sgn > 0 else (bar["l"] <= tp)
    exit_px = reason = None
    if force:
        exit_px, reason = bar["c"], force
    elif hit_sl:
        exit_px, reason = sl, ("breakeven" if sl == be else "stop_loss")
    elif hit_tp:
        exit_px, reason = tp, "take_profit"
    elif now - opened >= timedelta(minutes=int(pos.get("time_stop_min") or R.TIME_STOP_MIN)):
        exit_px, reason = bar["c"], "time_stop"
    if exit_px is None:
        if R.breakeven_due(pos["direction"], entry, pos["tp_dist"], bar["h"] if sgn > 0 else bar["l"]) and sl != be:
            pos["sl_live"] = be
        return None
    net = (exit_px - entry) * sgn - pos["spread_at_entry"] - pos["commission_price"]
    return {"r": round(net / pos["sl_dist"], 3), "pnl_usd": round(net / PIP_PRICE * 10.0 * pos["lots"], 2), "exit_reason": reason,
            "exit_at": now.isoformat(), "entry_at": pos["created_at"], "trigger": pos["trigger"], "bias": pos["bias"], "regime": pos.get("regime", "TREND"),
            "hour": opened.hour, "mfe": round(pos["mfe"], 2), "mae": round(pos["mae"], 2), "lots": pos["lots"]}


def _session(hour: int) -> str:
    return "London 07–13" if hour < 13 else ("NY overlap 13–16" if hour < 16 else "NY 16–20")


def summarize(trades: list, skips: dict, m1: list) -> dict:
    days = sorted({datetime.fromisoformat(t["entry_at"]).date() for t in trades})
    span_days = max(1, len({datetime.fromtimestamp(int(b["t"]), tz=timezone.utc).date() for b in m1 if datetime.fromtimestamp(int(b["t"]), tz=timezone.utc).weekday() < 5}))
    out = {"bars_m1": len(m1), "trading_days": span_days, "trades": len(trades), "trades_per_day": round(len(trades) / span_days, 2),
           "overall": R.outcome_stats(trades), "skips": dict(skips), "by_trigger": {}, "by_session": {}, "by_hour": {}, "by_exit": {}}
    for key, fn in (("by_trigger", lambda t: t["trigger"]), ("by_session", lambda t: _session(t["hour"])),
                    ("by_hour", lambda t: f"{t['hour']:02d}"), ("by_exit", lambda t: t["exit_reason"])):
        groups = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        out[key] = {k: R.outcome_stats(v) for k, v in sorted(groups.items())}
    out["active_days"] = len(days)
    return out


def markdown(rep: dict, title: str = "gold_trend_pulse replay") -> str:
    o = rep["overall"]
    rows = [f"### {title}", "",
            f"M1 bars: {rep['bars_m1']} · trading days: {rep['trading_days']} · trades: {rep['trades']} ({rep['trades_per_day']}/day)", "",
            "| metric | value |", "|---|---|",
            f"| win rate | {o['win_rate']} |", f"| profit factor | {o['profit_factor']} |", f"| expectancy after costs | {o['expectancy_r']:+.3f} R |",
            f"| max drawdown | {o['max_dd_r']} R |", f"| avg win / loss $ | {o['avg_win_usd']} / {o['avg_loss_usd']} |",
            f"| skips | {rep['skips']} |", ""]
    if rep.get("by_mode"):
        rows += ["| mode | n | win rate | PF | expectancy R | max DD R |", "|---|---|---|---|---|---|"]
        rows += [f"| {k} | {v['n']} | {v['win_rate']} | {v['profit_factor']} | {v['expectancy_r']:+.3f} | {v['max_dd_r']} |" for k, v in rep["by_mode"].items()]
        rows.append("")
    if rep.get("regime_minutes_per_day"):
        rows += ["| day | TREND min | RANGE min | NO_TRADE min |", "|---|---|---|---|"]
        rows += [f"| {d} | {v.get('TREND', 0)} | {v.get('RANGE', 0)} | {v.get('NO_TRADE', 0)} |" for d, v in rep["regime_minutes_per_day"].items()]
        rows.append("")
    if rep.get("baskets"):
        b = rep["baskets"]
        rows += [f"baskets: {b.get('baskets')} · legs/basket {b.get('legs_per_basket')} · basket win rate {b.get('basket_win_rate')} · "
                 f"worst basket loss ${b.get('worst_basket_loss_usd')} ({b.get('worst_basket_loss_pct')}%)", ""]
    for key, label in (("by_trigger", "trigger"), ("by_session", "session"), ("by_hour", "hour UTC"), ("by_exit", "exit")):
        rows += [f"| {label} | n | win rate | PF | expectancy R |", "|---|---|---|---|---|"]
        rows += [f"| {k} | {v['n']} | {v['win_rate']} | {v['profit_factor']} | {v['expectancy_r']:+.3f} |" for k, v in rep[key].items()]
        rows.append("")
    return "\n".join(rows)
