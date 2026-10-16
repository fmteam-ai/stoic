"""gold_trend_pulse · replay / backtest on XAUUSD M1 bars with a costs model (spread + commission).
Sources: --csv (MT5 History export: date,time,open,high,low,close[,tickvol]) or the DB (EA M1 feed, else M1 built
from the scalp tick store). Bias is recomputed every 15 min from M15-resampled bars exactly as the engine does.
Items 13/14: every sweep-and-reclaim is MARKED (both directions) and compared with the ones the rule traded; news
events ({title, when_ts, actual, forecast}) drive PRE / RELEASE / POST exactly as the engine and are reported separately."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from gold_trend_pulse import rules as R

PIP_PRICE = 0.10


from bars_util import load_csv  # noqa: E402,F401


def load_news_events(path: str) -> list:
    """JSON list [{title, when (ISO) | when_ts, actual, forecast, country?}] → engine event shape."""
    with open(path) as f:
        rows = json.load(f)
    out = []
    for r in rows:
        ts = r.get("when_ts")
        if ts is None and r.get("when"):
            ts = datetime.fromisoformat(str(r["when"]).replace("Z", "+00:00")).timestamp()
        if ts is None:
            continue
        out.append({"title": r.get("title") or r.get("event") or "", "country": r.get("country") or "USD", "impact": r.get("impact") or "high",
                    "when_ts": float(ts), "actual": r.get("actual"), "forecast": r.get("forecast") or r.get("estimate")})
    return sorted(out, key=lambda e: e["when_ts"])


def replay(m1: list, *, spread: float = 0.25, commission_usd_per_lot_side: float = 0.0, equity: float = 10_000.0,
           pip_usd_per_lot: float = 10.0, broker_offset_sec: int = 0, layering: bool = False, runner: bool = True,
           news_events: list | None = None, news_pre_action: str = "protect", news_release_min: int = R.NEWS_RELEASE_MIN_DEFAULT,
           trace_window: tuple | None = None) -> dict:
    """Walk M1 bars (UTC after offset) and emulate the engine: bias every 15 min with hysteresis, regime every M5
    (TREND / RANGE / NO_TRADE), TREND entries on M5 closes (sweep & reclaim first, then pullback / micro-breakout,
    or a layered basket), RANGE entries on M1 closes, news PRE/RELEASE/POST, cooldowns, daily stop, sessions, spike
    filter, TP/SL clamps, BE, time-stops, breakout hand-over. `trace_window=(start_ts, end_ts)` records every M5-close
    evaluation inside the window (e.g. 16 Oct 13:15–14:30)."""
    if broker_offset_sec:
        m1 = [{**b, "t": int(b["t"]) - broker_offset_sec} for b in m1]
    m1 = sorted(m1, key=lambda b: b["t"])
    m15 = R.resample(m1, 900)
    m5_all = R.resample(m1, 300)
    comm_price = 2 * commission_usd_per_lot_side / pip_usd_per_lot * PIP_PRICE
    tracker = R.BiasTracker()
    trades: list = []
    positions: list = []                      # open legs / trades (several only when layering or sweep runner)
    basket: dict | None = None
    baskets: list = []
    i15 = 0
    skips = defaultdict(int)
    regime_name, rng = "NO_TRADE", None
    regime_minutes = defaultdict(lambda: defaultdict(int))
    day_key = None
    day_rows: list = []
    sweeps_marked: dict = {}
    news_state: dict = {}
    trace: list = []

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

    def _trace(now, note, **kw):
        if trace_window and trace_window[0] <= now.timestamp() <= trace_window[1]:
            trace.append({"time": now.strftime("%H:%M"), "bias": tracker.bias, "regime": regime_name, "note": note, **kw})

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
            m5 = [b for b in m5_all if b["t"] + 300 <= t + 60 and int(b.get("n") or 5) >= 5][-80:]     # #67 — complete M5 bars only
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
            for b_dir in ("UP", "DOWN"):                       # item 13 — mark EVERY sweep & reclaim, bias or not
                s = R.detect_sweep_reclaim(m5, b_dir)
                if s and s["sweep_t"] not in sweeps_marked:
                    sweeps_marked[s["sweep_t"]] = {"time": now.isoformat(), "direction": s["direction"], "swept_level": s["swept_level"],
                                                   "entry": s["entry"], "bias": tracker.bias, "with_bias": tracker.bias == b_dir, "traded": False}
        else:
            m5 = None
        # --- news mode (item 14)
        ph = R.news_phase(news_events, now, None, news_release_min) if news_events else None
        news_bias = None
        if ph:
            ns = news_state.setdefault(ph["key"], {"kind": ph["kind"], "title": ph["title"], "t": ph["t"], "protected": False, "bias": None, "surprise": None,
                                                   "first_move": None, "confirmed": None, "trades": 0, "skipped": [], "pre_actions": []})
            if ph["phase"] == "PRE":
                if not ns["protected"]:
                    for pos in positions:
                        act = R.news_pre_action(pos["direction"], pos["entry"], pos["sl_dist"], bar["c"], news_pre_action)
                        ns["pre_actions"].append(act["action"])
                        if act["action"] == "breakeven":
                            pos["sl_live"] = R.breakeven_level(pos["direction"], pos["entry"], pos["spread_at_entry"], pos["commission_price"])
                        elif act["action"] == "tighten":
                            pos["sl_live"] = act["new_sl"]
                    if any(a == "close" for a in ns["pre_actions"]):
                        _close_all(bar, now, "news_pre_close")
                    ns["protected"] = True
                if m5_close:
                    _trace(now, f"NEWS PRE {ph['kind']}")
                continue
            if ph["phase"] == "RELEASE":
                if m5_close:
                    _trace(now, f"NEWS RELEASE {ph['kind']}")
                continue
            if not m5_close or not m5:
                continue                                       # POST: act on M5 closes only
            m5_post = [b for b in m5 if b["t"] >= ph["t"]]
            nb = R.fomc_bias(m5_post) if ph["kind"] in R.NEWS_RATE_EVENTS else R.news_surprise_bias(ph["event"], ph["kind"])
            ns["bias"], ns["surprise"] = nb.get("bias"), nb.get("surprise")
            if nb["bias"] not in (None, "NONE"):
                conf = R.news_confirmation(nb["bias"], R.pre_release_range(m5, ph["t"]), m5_post, ph["t"])
                ns["first_move"], ns["confirmed"] = conf.get("first_move"), conf["confirmed"]
                if conf["confirmed"]:
                    news_bias = nb["bias"]
                elif conf["confirmed"] is False and "not confirmed" not in ns["skipped"]:
                    ns["skipped"].append("not confirmed")
                if not news_bias:
                    _trace(now, f"NEWS POST {ph['kind']} bias {nb['bias']}: {conf['reason']}")
                    continue                                   # waiting / disagreed → no trade in this window
            if nb["bias"] is None:
                continue                                       # waiting for actual / first candle
        if regime_name == "NO_TRADE" and not news_bias:
            skips["no_trade"] += 1 if m5_close else 0
            _trace(now, "NO_TRADE") if m5_close else None
            continue
        if positions and not (layering and regime_name == "TREND" and basket):
            _trace(now, "position open") if m5_close else None
            continue
        ok, _ = R.session_ok(now)
        if not ok:
            continue
        consec = R.consecutive_losses(day_rows)                     # #69 — scratch exits do not count
        if R.daily_stop(consec, sum(r["pnl_usd"] for r in day_rows) / equity * 100, len(day_rows) + len(positions)):   # #70 — opened trades count
            skips["daily_stop"] += 1 if m5_close else 0; continue
        if day_rows:
            until = R.cooldown_until("loss" if R.is_loss(day_rows[-1]) else "win", datetime.fromisoformat(day_rows[-1]["exit_at"]))
            if until and now < until:
                continue
        d0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        # ---------------- NEWS POST: pullback or sweep in the confirmed news bias, max 2 per event
        if news_bias:
            if not m5_close or not m5:
                continue
            ns = news_state[ph["key"]]
            if ns["trades"] >= R.NEWS_MAX_TRADES_PER_EVENT:
                continue
            m5_post = [b for b in m5 if b["t"] >= ph["t"]]
            before = [b for b in m5 if b["t"] < ph["t"]]
            cand = R.detect_news_entry(m5, m5_post, news_bias, before[-1]["c"] if before else bar["c"]) or R.detect_sweep_reclaim(m5, news_bias)
            if not cand:
                _trace(now, f"NEWS POST {ph['kind']} bias {news_bias}: no entry")
                continue
            atr_post = R.atr(m5_post, 14) if len(m5_post) >= 3 else R.atr(m5, 14)
            plan = R.plan_news_exit(cand["direction"], cand["entry"], cand["swing"], atr_post)
            if not plan["ok"]:
                ns["skipped"].append(plan["skip"]); skips["news_sl_too_wide"] += 1; continue
            if not R.spread_ok(spread, plan["tp"])[0]:
                ns["skipped"].append("spread"); skips["spread"] += 1; continue
            sz = R.size_lots(equity, plan["sl"], pip_usd_per_lot, PIP_PRICE)
            if not sz["ok"]:
                ns["skipped"].append(sz["reason"]); skips["sizing"] += 1; continue
            trig = "news_sweep" if cand["trigger"] == "sweep" else cand["trigger"]
            pos = _pos({"direction": cand["direction"], "entry": cand["entry"]}, plan, spread, comm_price, sz["lots"], now, "NEWS", trig, news_bias, plan["time_stop_min"])
            pos["event"] = ph["key"]
            positions.append(pos); ns["trades"] += 1
            _trace(now, f"NEWS {cand['direction']} {trig}", entry=cand["entry"])
            continue
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
        cand = R.detect_sweep_reclaim(m5, tracker.bias) or R.detect_entry(m5, tracker.bias, vwap)
        if not cand:
            _trace(now, "no setup", price=bar["c"])
            continue
        if cand.get("blocked"):
            skips["spike"] += 1; _trace(now, cand["blocked"]); continue
        if cand["trigger"] == "sweep":
            plan = R.plan_sweep_exit(cand["direction"], cand["entry"], cand["sweep_low"], cand["pre_drop_high"], cand["atr"], runner=runner)
        else:
            plan = R.plan_exit(cand["direction"], cand["entry"], cand["swing"], cand["atr"])
        if not plan["ok"]:
            skips["sl_too_wide"] += 1; _trace(now, plan["skip"], trigger=cand["trigger"]); continue
        if not R.spread_ok(spread, plan["tp"])[0]:
            skips["spread"] += 1; continue
        sz = R.size_lots(equity, plan["sl"], pip_usd_per_lot, PIP_PRICE)
        if not sz["ok"]:
            skips["sizing"] += 1; continue
        lots, runner_lots = sz["lots"], None
        if cand["trigger"] == "sweep":
            sweeps_marked.get(cand["sweep_t"], {}).update({"traded": True})
            if plan.get("runner"):
                lots, runner_lots = R.split_runner_lots(sz["lots"])
        positions.append(_pos(cand, plan, spread, comm_price, lots, now, "TREND", cand["trigger"], tracker.bias, R.TIME_STOP_MIN))
        _trace(now, f"{cand['direction']} {cand['trigger']}", entry=cand["entry"], sl=plan["stop_loss"], tp=plan["take_profit"])
        if runner_lots:
            rp = plan["runner"]
            rplan = {"tp": rp["tp"], "sl": plan["sl"], "take_profit": rp["take_profit"], "stop_loss": plan["stop_loss"]}
            rpos = _pos(cand, rplan, spread, comm_price, runner_lots, now, "TREND", "sweep_runner", tracker.bias, rp["time_stop_min"])
            rpos["be_trigger_price"] = rp["be_trigger_price"]
            positions.append(rpos)
    if basket:
        baskets.append({"legs": basket["closed"]})
    normal = [x for x in trades if x["regime"] != "NEWS"]
    rep = summarize(normal, skips, m1)
    rep["by_mode"] = {k: R.outcome_stats([x for x in trades if x["regime"] == k]) for k in ("TREND", "RANGE", "NEWS")}
    rep["regime_minutes_per_day"] = {d: dict(v) for d, v in sorted(regime_minutes.items())}
    rep["baskets"] = R.basket_stats([b for b in baskets if b["legs"]], equity) if layering else None
    sw = [x for x in normal if x["trigger"] in ("sweep", "sweep_runner")]
    rep["sweeps"] = {"marked": len(sweeps_marked), "with_bias": sum(1 for s in sweeps_marked.values() if s["with_bias"]),
                     "caught": sum(1 for s in sweeps_marked.values() if s["traded"]), "stats": R.outcome_stats([x for x in sw if x["trigger"] == "sweep"]),
                     "runner_stats": R.outcome_stats([x for x in sw if x["trigger"] == "sweep_runner"]),
                     "list": [sweeps_marked[k] for k in sorted(sweeps_marked)]}
    news_tr = [x for x in trades if x["regime"] == "NEWS"]
    rep["news"] = {"events": [{**v, "pnl_usd": round(sum(x["pnl_usd"] for x in news_tr if x.get("event") == k), 2)} for k, v in sorted(news_state.items(), key=lambda kv: kv[1]["t"])],
                   "stats": R.outcome_stats(news_tr), "worst_trade_usd": min([x["pnl_usd"] for x in news_tr], default=None),
                   "by_kind": {k: R.outcome_stats([x for x in news_tr if news_state.get(x.get("event"), {}).get("kind") == k])
                               for k in sorted({v["kind"] for v in news_state.values()})}} if news_events else None
    rep["trace"] = trace if trace_window else None
    rep["trade_rows"] = trades
    return rep


def write_trades_csv(rep: dict, path: str) -> int:
    """Every replayed trade (normal sessions + NEWS) as CSV — for the VPS report, nothing else is written."""
    cols = ["entry_at", "exit_at", "regime", "trigger", "bias", "direction", "lots", "r", "pnl_usd", "exit_reason", "mfe", "mae", "hour", "event"]
    rows = rep.get("trade_rows") or []
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in rows:
            w.writerow(t)
    return len(rows)


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
        be_px = pos.get("be_trigger_price")
        reached = ((bar["h"] >= be_px) if sgn > 0 else (bar["l"] <= be_px)) if be_px else \
            R.breakeven_due(pos["direction"], entry, pos["tp_dist"], bar["h"] if sgn > 0 else bar["l"])
        if reached and sl != be:
            pos["sl_live"] = be
        return None
    net = (exit_px - entry) * sgn - pos["spread_at_entry"] - pos["commission_price"]
    costs = pos["spread_at_entry"] + pos["commission_price"]
    result = "scratch" if abs(net) <= costs + 1e-9 or reason == "breakeven" else ("win" if net > 0 else "loss")    # #69
    return {"r": round(net / pos["sl_dist"], 3), "result": result, "net_price": round(net, 3), "costs_price": round(costs, 3), "pnl_usd": round(net / PIP_PRICE * 10.0 * pos["lots"], 2), "exit_reason": reason, "direction": pos["direction"],
            "exit_at": now.isoformat(), "entry_at": pos["created_at"], "trigger": pos["trigger"], "bias": pos["bias"], "regime": pos.get("regime", "TREND"),
            "hour": opened.hour, "mfe": round(pos["mfe"], 2), "mae": round(pos["mae"], 2), "lots": pos["lots"], "event": pos.get("event")}


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


def _stat_row(label: str, v: dict) -> str:
    return f"| {label} | {v['n']} | {v['win_rate']} | {v['profit_factor']} | {v['expectancy_r']:+.3f} | {v['avg_win_usd']} / {v['avg_loss_usd']} |"


def markdown(rep: dict, title: str = "gold_trend_pulse replay") -> str:
    o = rep["overall"]
    rows = [f"### {title}", "",
            f"M1 bars: {rep['bars_m1']} · trading days: {rep['trading_days']} · trades (normal sessions): {rep['trades']} ({rep['trades_per_day']}/day)", "",
            "| metric | value |", "|---|---|",
            f"| win rate | {o['win_rate']} |", f"| profit factor | {o['profit_factor']} |", f"| expectancy after costs | {o['expectancy_r']:+.3f} R |",
            f"| max drawdown | {o['max_dd_r']} R |", f"| avg win / loss $ | {o['avg_win_usd']} / {o['avg_loss_usd']} |",
            f"| skips | {rep['skips']} |", ""]
    if rep.get("by_mode"):
        rows += ["| mode | n | win rate | PF | expectancy R | max DD R |", "|---|---|---|---|---|---|"]
        rows += [f"| {k} | {v['n']} | {v['win_rate']} | {v['profit_factor']} | {v['expectancy_r']:+.3f} | {v['max_dd_r']} |" for k, v in rep["by_mode"].items()]
        rows.append("")
    if rep.get("sweeps"):
        s = rep["sweeps"]
        rows += [f"**Sweep & reclaim (item 13)** — marked {s['marked']} (with bias {s['with_bias']}) · caught by the rule {s['caught']}", "",
                 "| leg | n | win rate | PF | expectancy R | avg win / loss $ |", "|---|---|---|---|---|---|",
                 _stat_row("scalp", s["stats"]), _stat_row("runner", s["runner_stats"]), ""]
    if rep.get("news"):
        n = rep["news"]
        rows += [f"**News mode (item 14)** — separate from normal sessions · worst trade ${n['worst_trade_usd']}", "",
                 "| kind | n | win rate | PF | expectancy R | avg win / loss $ |", "|---|---|---|---|---|---|",
                 _stat_row("ALL", n["stats"])] + [_stat_row(k, v) for k, v in n["by_kind"].items()] + [""]
        rows += ["| event | time UTC | surprise | bias | first move | confirmed | trades | skipped | P&L $ |", "|---|---|---|---|---|---|---|---|---|"]
        rows += [f"| {e['title']} | {datetime.fromtimestamp(e['t'], tz=timezone.utc):%Y-%m-%d %H:%M} | {e['surprise']} | {e['bias']} | {e['first_move']} | {e['confirmed']} | {e['trades']} | {'; '.join(e['skipped'][:3])} | {e['pnl_usd']} |"
                 for e in n["events"]]
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
    if rep.get("trace") is not None:
        rows += ["**Window trace (every M5 close)**", "", "| time | bias | regime | note |", "|---|---|---|---|"]
        rows += [f"| {r['time']} | {r['bias']} | {r['regime']} | {r['note']}{' @ ' + str(r['entry']) if r.get('entry') else ''} |" for r in rep["trace"]]
        rows.append("")
    return "\n".join(rows)
