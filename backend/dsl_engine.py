"""Strategy DSL engine — executes the closed-vocab rule tree from
strategy_code_generator on candles, backtests it, and reads it back in
plain English.

The read-back is generated from the SAME tree the simulator runs (never by
the LLM), so what the user confirms is exactly what is tested.

Semantics (stated in every read-back):
  • A side (BUY / SELL) enters when ALL of its own rules AND all ANY rules
    are true at a bar's close. A side with no rules of its own never trades —
    ANY rules alone carry no direction. BUY is checked before SELL.
  • Fills happen at the NEXT bar's open (no same-bar look-ahead).
  • One position at a time per symbol; exits are checked intrabar on later
    bars: stop first when stop and target sit inside the same bar.
  • Break-even moves the stop to entry once price has gone `breakeven_after_pct`
    in favour; the trail follows the best close-to-date by `trail_pct`.
  • `cost_bps` is charged per side (spread + commission).
  • A rule whose field cannot be rebuilt from candles (`confidence` — a live
    model output — or `session` on daily bars) is NOT silently dropped: it
    is listed under `untestable` and the backtest is flagged as partial.
"""
from __future__ import annotations

from datetime import datetime, timezone

FIELD_LABELS = {
    "rsi_14": "RSI(14)",
    "atr_pct": "ATR(14) as % of price",
    "ma_20_vs_200": "MA(20) vs MA(200) gap %",
    "session": "trading session",
    "confidence": "AI signal confidence",
}
OP_WORDS = {"<": "is below", ">": "is above", "=": "is", "in": "is one of", "between": "is between"}
SESSIONS = {"london": (7, 16), "ny": (12, 21), "tokyo": (23, 8)}     # UTC hours, same as strategy_backtest
INTRADAY_ONLY = {"session"}
NEVER_FROM_CANDLES = {"confidence"}
MIN_TRADES_FOR_CONFIDENCE = 30


# ─────────────────────────── candles ───────────────────────────

def normalize_bars(rows) -> list:
    """Accept EA intraday bars {t,o,h,l,c} or daily history {date,open,high,low,close}."""
    out = []
    for r in rows or []:
        try:
            if "t" in r:
                ts = int(r["t"])
                o, h, lo, c = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
            else:
                ts = int(datetime.fromisoformat(str(r["date"])[:10]).replace(tzinfo=timezone.utc).timestamp())
                o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, lo, c) > 0 and h >= lo:
            out.append({"ts": ts, "o": o, "h": h, "l": lo, "c": c})
    out.sort(key=lambda b: b["ts"])
    return out


def _sessions_at(ts: int) -> set:
    hour = datetime.fromtimestamp(ts, timezone.utc).hour
    return {name for name, (a, b) in SESSIONS.items() if (a <= hour < b if a < b else hour >= a or hour < b)}


def feature_series(bars, intraday: bool) -> list:
    """Per-bar features known at that bar's close. None during warm-up."""
    feats = []
    gain = loss = 0.0
    trs: list = []
    closes: list = []
    s20 = s200 = 0.0
    for i, b in enumerate(bars):
        c = b["c"]
        closes.append(c)
        s20 += c - (closes[i - 20] if i >= 20 else 0.0)
        s200 += c - (closes[i - 200] if i >= 200 else 0.0)
        rsi = None
        if i >= 1:
            ch = c - closes[i - 1]
            g, lo = max(ch, 0.0), max(-ch, 0.0)
            if i <= 14:                                  # seed with a simple average
                gain += g / 14
                loss += lo / 14
            else:                                        # Wilder smoothing
                gain = (gain * 13 + g) / 14
                loss = (loss * 13 + lo) / 14
            if i >= 14:
                rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
            p = bars[i - 1]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
        w = trs[-14:]
        atr_pct = (sum(w) / len(w)) / c * 100 if len(w) == 14 else None
        ma_gap = ((s20 / 20) / (s200 / 200) - 1) * 100 if i >= 199 else None
        feats.append({"rsi_14": rsi, "atr_pct": atr_pct, "ma_20_vs_200": ma_gap,
                      "session": _sessions_at(b["ts"]) if intraday else None})
    return feats


# ─────────────────────────── rules ───────────────────────────

def _resolve(rule: dict, params: dict):
    ref = rule.get("value_ref")
    if isinstance(ref, str) and ref.startswith("params."):
        return params.get(ref.split(".", 1)[1])
    return rule.get("value")


def _ma_value(v):
    """MA rules may say 'above'/'bullish' instead of a number — map to the gap sign."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("above", "bullish", "golden", "up", "true"):
            return "above"
        if s in ("below", "bearish", "death", "down", "false"):
            return "below"
    return v


def rule_true(rule: dict, feat: dict, params: dict):
    """True / False, or None when the feature is still warming up."""
    field, op, val = rule.get("field"), rule.get("op"), _resolve(rule, params)
    x = feat.get(field)
    if x is None:
        return None
    if field == "session":
        wanted = {str(v).lower() for v in (val if isinstance(val, (list, tuple)) else [val])}
        return bool(x & wanted) if "any" not in wanted else True
    if field == "ma_20_vs_200":
        mv = _ma_value(val)
        if mv == "above":
            return x > 0
        if mv == "below":
            return x < 0
    try:
        if op == "<":
            return x < float(val)
        if op == ">":
            return x > float(val)
        if op == "=":
            return abs(x - float(val)) < 1e-9
        if op == "between":
            lo, hi = sorted(float(v) for v in val)
            return lo <= x <= hi
        if op == "in":
            return x in [float(v) for v in val]
    except (TypeError, ValueError):
        return False
    return False


def split_rules(dsl: dict, intraday: bool) -> dict:
    """Group rules per side and separate the ones candles cannot test."""
    testable, untestable = {"BUY": [], "SELL": [], "ANY": []}, []
    for r in dsl.get("entry_rules") or []:
        f = r.get("field")
        if f in NEVER_FROM_CANDLES or (f in INTRADAY_ONLY and not intraday):
            untestable.append(r)
        else:
            testable.setdefault(r.get("side", "ANY"), []).append(r)
    return {"rules": testable, "untestable": untestable}


def exits_of(dsl: dict) -> dict:
    out = {}
    for r in dsl.get("exit_rules") or []:
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[r.get("kind")] = v
    return out


# ─────────────────────────── read-back ───────────────────────────

def _fmt_val(rule, params):
    v = _resolve(rule, params)
    ref = rule.get("value_ref")
    if rule.get("field") == "ma_20_vs_200" and _ma_value(v) in ("above", "below"):
        return None
    if isinstance(v, (list, tuple)):
        txt = " and ".join(str(x) for x in v) if rule.get("op") == "between" else ", ".join(str(x) for x in v)
    else:
        txt = str(v)
    if isinstance(ref, str) and ref.startswith("params."):
        txt += f" ({ref.split('.', 1)[1]})"
    return txt


def rule_text(rule: dict, params: dict) -> str:
    f = rule.get("field")
    label = FIELD_LABELS.get(f, f)
    if f == "ma_20_vs_200" and _ma_value(_resolve(rule, params)) in ("above", "below"):
        return f"MA(20) is {_ma_value(_resolve(rule, params))} MA(200)"
    return f"{label} {OP_WORDS.get(rule.get('op'), rule.get('op'))} {_fmt_val(rule, params)}"


def _contradictions(rules, params) -> list:
    """Same field bounded so no value can satisfy every rule (e.g. RSI < 30 and RSI > 70)."""
    out = []
    by_field: dict = {}
    for r in rules:
        if r.get("op") in ("<", ">") and r.get("field") not in ("session",):
            try:
                by_field.setdefault(r["field"], []).append((r["op"], float(_resolve(r, params))))
            except (TypeError, ValueError):
                continue
    for f, bounds in by_field.items():
        lo = max((v for op, v in bounds if op == ">"), default=None)
        hi = min((v for op, v in bounds if op == "<"), default=None)
        if lo is not None and hi is not None and lo >= hi:
            out.append(f"{FIELD_LABELS.get(f, f)} must be above {lo:g} and below {hi:g} at once — this side can never enter.")
    return out


def explain(dsl: dict, intraday: bool = True) -> dict:
    """Deterministic plain-English restatement of exactly what will run."""
    params = dsl.get("params") or {}
    parts = split_rules(dsl, intraday)
    rules, untestable = parts["rules"], parts["untestable"]
    lines, warnings = [], []
    for side, verb in (("BUY", "Buy"), ("SELL", "Sell")):
        own = rules.get(side, [])
        if not own:
            lines.append(f"{verb}: never — no {side} rule (ANY rules alone carry no direction).")
            continue
        conds = [rule_text(r, params) for r in own + rules.get("ANY", [])]
        lines.append(f"{verb} at the next bar's open when " + " AND ".join(conds) + ".")
        warnings += _contradictions(own + rules.get("ANY", []), params)
    if untestable:
        lines.append("Live-only conditions (also required, but not in the backtest): "
                     + " AND ".join(rule_text(r, params) for r in untestable) + ".")
    ex = exits_of(dsl)
    exit_bits = []
    if "take_profit_pct" in ex:
        exit_bits.append(f"take profit at +{ex['take_profit_pct']:g}%")
    if "stop_loss_pct" in ex:
        exit_bits.append(f"stop loss at −{ex['stop_loss_pct']:g}%")
    if "breakeven_after_pct" in ex:
        exit_bits.append(f"move the stop to entry after +{ex['breakeven_after_pct']:g}%")
    if "trail_pct" in ex:
        exit_bits.append(f"trail the stop {ex['trail_pct']:g}% behind the best close")
    lines.append("Exit: " + (", ".join(exit_bits) if exit_bits else "no exit rule") + ".")
    if "stop_loss_pct" not in ex:
        warnings.append("No stop loss — losses are unbounded.")
    if "take_profit_pct" in ex and "stop_loss_pct" in ex and ex["take_profit_pct"] < ex["stop_loss_pct"]:
        warnings.append("Take profit is smaller than the stop loss — the win rate must exceed "
                        f"{ex['stop_loss_pct'] / (ex['take_profit_pct'] + ex['stop_loss_pct']):.0%} to break even before costs.")
    sess = dsl.get("session_preference")
    if sess and sess != "any":
        lines.append(f"Session preference: {sess} (only enforced through a session rule).")
    for r in untestable:
        why = ("it is a live model output that cannot be rebuilt from past candles"
               if r.get("field") in NEVER_FROM_CANDLES else "daily bars have no session")
        warnings.append(f"Not backtested: '{rule_text(r, params)}' — {why}. Results are partial.")
    return {"text": "\n".join(lines), "lines": lines, "warnings": warnings,
            "untestable": untestable, "symbols": dsl.get("symbols") or []}


# ─────────────────────────── backtest ───────────────────────────

def _entry_side(rules, feat, params):
    for side in ("BUY", "SELL"):
        own = rules.get(side, [])
        if not own:
            continue
        if all(rule_true(r, feat, params) is True for r in own + rules.get("ANY", [])):
            return side
    return None


def simulate(dsl: dict, bars: list, intraday: bool = True, cost_bps: float = 2.0) -> dict:
    """Run the rule tree over normalized bars. Pure; no I/O."""
    params = dsl.get("params") or {}
    rules = split_rules(dsl, intraday)["rules"]
    ex = exits_of(dsl)
    tp, sl = ex.get("take_profit_pct"), ex.get("stop_loss_pct")
    be, trail = ex.get("breakeven_after_pct"), ex.get("trail_pct")
    cost = cost_bps / 10_000
    feats = feature_series(bars, intraday)
    trades, pos, pending = [], None, None
    for i, b in enumerate(bars):
        if pending and pos is None:                       # fill at this bar's open
            pos = {"side": pending, "entry": b["o"], "i": i, "best": b["o"], "stop": None, "stop_kind": None}
            d = 1 if pending == "BUY" else -1
            if sl:
                pos["stop"], pos["stop_kind"] = pos["entry"] * (1 - d * sl / 100), "stop_loss"
            pending = None
        if pos:
            d = 1 if pos["side"] == "BUY" else -1
            target = pos["entry"] * (1 + d * tp / 100) if tp else None
            hit_stop = pos["stop"] is not None and (b["l"] <= pos["stop"] if d > 0 else b["h"] >= pos["stop"])
            hit_tgt = target is not None and (b["h"] >= target if d > 0 else b["l"] <= target)
            exit_px = reason = None
            if hit_stop:
                gap = b["o"] < pos["stop"] if d > 0 else b["o"] > pos["stop"]
                exit_px, reason = (b["o"] if gap else pos["stop"]), pos["stop_kind"]
            elif hit_tgt:
                gap = b["o"] > target if d > 0 else b["o"] < target
                exit_px, reason = (b["o"] if gap else target), "take_profit"
            if exit_px is not None:
                gross = d * (exit_px / pos["entry"] - 1)
                trades.append({"side": pos["side"], "entry_ts": bars[pos["i"]]["ts"], "exit_ts": b["ts"],
                               "entry": pos["entry"], "exit": exit_px, "bars": i - pos["i"] + 1,
                               "reason": reason, "return_pct": (gross - 2 * cost) * 100})
                pos = None
            else:                                         # ratchet stops after the bar
                pos["best"] = max(pos["best"], b["c"]) if d > 0 else min(pos["best"], b["c"])
                fav = d * (pos["best"] / pos["entry"] - 1) * 100
                cands = [(pos["stop"], pos["stop_kind"])] if pos["stop"] is not None else []
                if be and fav >= be:
                    cands.append((pos["entry"], "breakeven"))
                if trail:
                    cands.append((pos["best"] * (1 - d * trail / 100), "trailing_stop"))
                if cands:
                    best = max(cands, key=lambda c: d * c[0])         # tightest stop wins
                    if best[0] != pos["stop"]:
                        pos["stop"], pos["stop_kind"] = best
        if pos is None and pending is None and i < len(bars) - 1:
            pending = _entry_side(rules, feats[i], params)
    open_at_end = None
    if pos:
        last = bars[-1]
        d = 1 if pos["side"] == "BUY" else -1
        open_at_end = {"side": pos["side"], "entry": pos["entry"], "mark": last["c"],
                       "return_pct": (d * (last["c"] / pos["entry"] - 1) - cost) * 100}
    return {**_metrics(trades, bars), "trades": trades[-200:], "open_at_end": open_at_end}


def _metrics(trades, bars) -> dict:
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= 1 + r / 100
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    reasons: dict = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    gross_loss = -sum(losses)
    n = len(rets)
    return {
        "bars": len(bars),
        "from": datetime.fromtimestamp(bars[0]["ts"], timezone.utc).isoformat() if bars else None,
        "to": datetime.fromtimestamp(bars[-1]["ts"], timezone.utc).isoformat() if bars else None,
        "trades_n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "total_return_pct": round((equity - 1) * 100, 3),
        "avg_return_pct": round(sum(rets) / n, 4) if n else None,
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_pct": round(max_dd * 100, 3),
        "avg_bars_held": round(sum(t["bars"] for t in trades) / n, 2) if n else None,
        "buy_and_hold_pct": round((bars[-1]["c"] / bars[0]["o"] - 1) * 100, 3) if bars else None,
        "exit_reasons": reasons,
        "enough_trades": n >= MIN_TRADES_FOR_CONFIDENCE,
    }


def backtest(dsl: dict, rows: list, timeframe: str, cost_bps: float = 2.0) -> dict:
    """Read-back + simulation in one payload — the user sees what ran next to how it did."""
    intraday = timeframe != "D1"
    bars = normalize_bars(rows)
    explanation = explain(dsl, intraday)
    if len(bars) < 30:
        return {"explanation": explanation, "timeframe": timeframe, "ready": False,
                "reason": f"only {len(bars)} usable bars"}
    res = simulate(dsl, bars, intraday, cost_bps)
    notes = []
    if res["trades_n"] < MIN_TRADES_FOR_CONFIDENCE:
        notes.append(f"Only {res['trades_n']} trades — too few to trust the win rate "
                     f"(aim for ≥ {MIN_TRADES_FOR_CONFIDENCE}).")
    if any(r.get("field") == "ma_20_vs_200" for r in dsl.get("entry_rules") or []) and len(bars) < 260:
        notes.append("MA(200) needs 200 bars to warm up — most of this window could not enter.")
    if explanation["untestable"]:
        notes.append("Partial backtest: some entry rules were not simulated (see warnings).")
    return {"explanation": explanation, "timeframe": timeframe, "ready": True,
            "cost_bps": cost_bps, "result": res, "notes": notes}
