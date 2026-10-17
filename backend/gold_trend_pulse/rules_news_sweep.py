"""gold_trend_pulse · items 13 (sweep & reclaim) and 14 (news mode) — pure rules, price units, no I/O.
Imported into rules.py so callers keep using `R.<name>`."""
from __future__ import annotations

from datetime import datetime, timezone

from gold_trend_pulse.rules import SL_BUFFER, SL_MAX_TP_MULT, TOUCH_TOL, TP_MAX, TP_MIN, TP_ATR_MULT, atr, ema

# ------------------------------------------------------------------------------------------- 13. sweep & reclaim
SWEEP_DROP_ATR, SWEEP_DROP_BARS, SWEEP_PRIOR_BARS, SWEEP_RECLAIM_BARS, SWEEP_BODY_FRAC = 1.5, 6, 24, 3, 0.5
SWEEP_SL_ATR, SWEEP_SL_MAX_ATR, RUNNER_TIME_STOP_MIN = 0.25, 3.5, 90


def detect_sweep_reclaim(m5: list, bias: str) -> dict | None:
    """Bias direction ONLY (UP shown; DOWN mirrored). A fast drop ≥ 1.5×ATR14 within ≤ 6 M5 bars takes price BELOW the
    lowest low of the 2 h (24 bars) before the drop (stop sweep); in the same or the next 3 bars an M5 bar CLOSES back
    above that swept level with a body ≥ 50 % of its range → BUY at the close. Returns the setup or None."""
    if bias not in ("UP", "DOWN") or len(m5) < SWEEP_PRIOR_BARS + SWEEP_DROP_BARS + 2:
        return None
    up, n = bias == "UP", len(m5)
    sgn = 1 if up else -1
    last = m5[-1]
    rng, body = last["h"] - last["l"], (last["c"] - last["o"]) * sgn
    if rng <= 0 or body <= 0 or body / rng < SWEEP_BODY_FRAC:
        return None
    a14 = atr(m5, 14)
    if a14 <= 0:
        return None
    ext = (lambda b: b["l"]) if up else (lambda b: b["h"])        # the swept extreme
    opp = (lambda b: b["h"]) if up else (lambda b: b["l"])        # where the drop started
    for j in range(n - 1 - SWEEP_RECLAIM_BARS, n):              # oldest first → the deepest extreme is the sweep bar
        sweep_bar = m5[j]
        sweep_px = ext(sweep_bar)
        if any((ext(b) - sweep_px) * sgn < 0 for b in m5[j:]):     # a deeper extreme after it → not the sweep bar
            continue
        window = m5[max(0, j - SWEEP_DROP_BARS + 1):j + 1]
        k_rel = max(range(len(window)), key=lambda i: opp(window[i]) * sgn)
        pre_drop = opp(window[k_rel])
        if (pre_drop - sweep_px) * sgn < SWEEP_DROP_ATR * a14:
            continue
        k = j - (len(window) - 1) + k_rel
        prior = m5[max(0, k - SWEEP_PRIOR_BARS):k]
        if len(prior) < SWEEP_PRIOR_BARS // 2:
            continue
        swept_level = min(b["l"] for b in prior) if up else max(b["h"] for b in prior)
        if (swept_level - sweep_px) * sgn <= 0:                  # must trade BEYOND the prior 2 h extreme
            continue
        if (last["c"] - swept_level) * sgn <= 0:                 # must CLOSE back on the right side
            continue
        return {"trigger": "sweep", "direction": "BUY" if up else "SELL", "entry": last["c"], "sweep_low": round(sweep_px, 2),
                "swept_level": round(swept_level, 2), "pre_drop_high": round(pre_drop, 2), "atr": a14, "swing": sweep_px,
                "drop": round((pre_drop - sweep_px) * sgn, 2), "sweep_t": int(sweep_bar["t"]),
                "reason": f"sweep of the 2 h {'low' if up else 'high'} {swept_level:.2f} ({(pre_drop - sweep_px) * sgn:.2f} = "
                          f"{(pre_drop - sweep_px) * sgn / a14:.1f}×ATR in ≤{SWEEP_DROP_BARS} bars) and M5 close back {'above' if up else 'below'} it"}
    return None


def plan_sweep_exit(direction: str, entry: float, sweep_low: float, pre_drop_high: float, atr14: float, runner: bool = True) -> dict:
    """SL beyond the sweep extreme − max(0.30, 0.25×ATR). Scalp TP clamp(0.7×ATR, 2.00, 4.50); optional ½-size runner to
    the pre-drop high (only when it lies beyond the scalp TP), moved to BE once the scalp TP is hit. The SL of a sweep is
    structurally wider than the scalp TP, so the 1.3×TP rule of item 3 is applied against the setup's target (runner
    target when present) with a floor of 3.5×ATR14 — a reclaim that already ran too far from the sweep low is skipped.
    #68: the SCALP leg must satisfy SL ≤ 1.3×scalp TP (else the setup is skipped); the runner may use its own cap."""
    sgn = 1 if direction == "BUY" else -1
    tp = max(TP_MIN, min(TP_MAX, TP_ATR_MULT * atr14))
    sl = (entry - sweep_low) * sgn + max(SL_BUFFER, SWEEP_SL_ATR * atr14)
    runner_dist = (pre_drop_high - entry) * sgn
    has_runner = bool(runner and runner_dist > tp)
    if sl > SL_MAX_TP_MULT * tp:                                   # #68 — scalp leg obeys the engine's 1.3×TP cap
        return {"ok": False, "skip": f"SL too wide for target (SL {sl:.2f} > {SL_MAX_TP_MULT}×TP {tp:.2f})", "tp": tp, "sl": sl}
    if has_runner and sl > max(SL_MAX_TP_MULT * runner_dist, SWEEP_SL_MAX_ATR * atr14):
        has_runner = False                                          # runner has its own cap; total risk stays 0.25 % either way
    out = {"ok": True, "tp": round(tp, 2), "sl": round(sl, 2), "take_profit": round(entry + sgn * tp, 2), "stop_loss": round(entry - sgn * sl, 2),
           "runner": None}
    if has_runner:
        out["runner"] = {"tp": round(runner_dist, 2), "take_profit": round(pre_drop_high, 2), "be_trigger_price": out["take_profit"],
                         "time_stop_min": RUNNER_TIME_STOP_MIN}
    return out


def split_runner_lots(lots: float, min_lot: float = 0.01) -> tuple[float, float | None]:
    """Total fixed-risk lots → (scalp, runner). Runner only when both legs stay ≥ min lot."""
    half = round(lots / 2, 2)
    if half < min_lot or lots - half < min_lot:
        return round(lots, 2), None
    return round(lots - half, 2), half


# ------------------------------------------------------------------------------------------- 14. news mode
NEWS_EVENTS_DEFAULT = ["CPI", "Core CPI", "Nonfarm Payrolls", "FOMC", "Fed Interest Rate Decision", "Fed Chair",
                       "PCE", "GDP", "Retail Sales", "PPI", "ISM"]
NEWS_ALIASES = {"Nonfarm Payrolls": ("nonfarm", "non-farm", "nfp", "payrolls"), "FOMC": ("fomc",),
                "Fed Interest Rate Decision": ("interest rate decision", "fed funds", "rate decision"), "Fed Chair": ("fed chair", "powell"),
                "Core CPI": ("core cpi",), "CPI": ("cpi", "consumer price"), "PCE": ("pce",), "GDP": ("gdp",),
                "Retail Sales": ("retail sales",), "PPI": ("ppi", "producer price"), "ISM": ("ism",)}
NEWS_RATE_EVENTS = ("FOMC", "Fed Interest Rate Decision", "Fed Chair")      # direction from the first 15-min candle
NEWS_SURPRISE_THRESHOLDS = {"CPI": 0.1, "Core CPI": 0.1, "Nonfarm Payrolls": 25.0, "PCE": 0.1, "GDP": 0.3, "Retail Sales": 0.2,
                            "PPI": 0.1, "ISM": 1.0}                            # raw units: pp / thousands / index points
NEWS_SURPRISE_PCT_FALLBACK = 0.05
NEWS_PRE_MIN, NEWS_RELEASE_MIN_DEFAULT, NEWS_POST_MIN, NEWS_CONFIRM_MIN, NEWS_PRE_RANGE_MIN = 15, 5, 90, 15, 30
NEWS_TP_ATR, NEWS_TP_MIN, NEWS_TP_MAX, NEWS_TIME_STOP_MIN, NEWS_MAX_TRADES_PER_EVENT = 0.7, 3.00, 8.00, 60, 2
NEWS_PROTECT_R, NEWS_TIGHTEN_R = 0.5, 0.5
NEWS_PRE_ACTIONS = ("protect", "close", "tighten")


def news_event_kind(title: str, event_list: list | None = None) -> str | None:
    """Longest alias match wins ('Core CPI' before 'CPI')."""
    import re
    low = (title or "").lower()
    best = None
    for kind in (event_list or NEWS_EVENTS_DEFAULT):
        for alias in NEWS_ALIASES.get(kind, (kind.lower(),)):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", low) and (best is None or len(alias) > best[0]):   # #70 — whole word, no "ism" in "optimism"
                best = (len(alias), kind)
    return best[1] if best else None


def _news_candidate(e: dict) -> bool:
    return str(e.get("country") or "USD").upper() == "USD" and e.get("when_ts") is not None and str(e.get("impact") or "high").lower() == "high"


def news_phase(events: list, now: datetime, event_list: list | None = None, release_min: int = NEWS_RELEASE_MIN_DEFAULT) -> dict | None:
    """The USD event of the configured kinds whose window T−15 → T+90 contains `now`: {phase: PRE|RELEASE|POST, event,
    kind, t, key}. The nearest release wins when windows overlap."""
    best = None
    for e in events or []:
        if not _news_candidate(e):
            continue
        kind = news_event_kind(e.get("title") or "", event_list)
        if not kind:
            continue
        t = float(e["when_ts"])
        dt = now.timestamp() - t
        if dt < -NEWS_PRE_MIN * 60 or dt > NEWS_POST_MIN * 60:
            continue
        if best is None or abs(dt) < abs(best["dt"]):
            phase = "PRE" if dt < 0 else ("RELEASE" if dt < release_min * 60 else "POST")
            best = {"phase": phase, "event": e, "kind": kind, "t": t, "dt": dt, "key": f"{kind}@{int(t)}",
                    "title": e.get("title") or kind}
    return best


def news_surprise_bias(event: dict, kind: str) -> dict:
    """Data prints: surprise = actual − forecast (raw) and / |forecast| (pct). Inflation / jobs / growth beats → USD
    strong → XAU DOWN; misses → UP; |surprise| under the per-event threshold → NONE (normal daily bias applies)."""
    if kind in NEWS_RATE_EVENTS:
        return {"bias": None, "surprise": None, "reason": "rate event — direction from the first 15-min candle"}
    try:
        a, f = float(str(event.get("actual")).replace("%", "").replace("K", "")), float(str(event.get("forecast")).replace("%", "").replace("K", ""))
    except (TypeError, ValueError):
        return {"bias": None, "surprise": None, "reason": "actual / forecast not published yet"}
    raw = a - f
    pct = raw / abs(f) if f else None
    thr = NEWS_SURPRISE_THRESHOLDS.get(kind)
    hit = abs(raw) >= thr if thr is not None else (pct is not None and abs(pct) >= NEWS_SURPRISE_PCT_FALLBACK)
    if not hit:
        return {"bias": "NONE", "surprise": round(raw, 4), "surprise_pct": None if pct is None else round(pct, 4),
                "reason": f"{kind} actual {a} vs forecast {f}: surprise {raw:+.3g} below threshold {thr if thr is not None else f'{NEWS_SURPRISE_PCT_FALLBACK:.0%}'} — no news bias"}
    bias = "DOWN" if raw > 0 else "UP"
    return {"bias": bias, "surprise": round(raw, 4), "surprise_pct": None if pct is None else round(pct, 4),
            "reason": f"{kind} actual {a} vs forecast {f}: {'positive' if raw > 0 else 'negative'} surprise → USD {'strong' if raw > 0 else 'weak'} → XAU {bias}"}


def fomc_bias(m5_post: list) -> dict:
    """First 15 min after the statement (3 completed M5 bars): close of the 3rd vs open of the 1st."""
    if len(m5_post) < 3:
        return {"bias": None, "reason": f"waiting for the first 15-min candle ({len(m5_post)}/3 M5 bars)"}
    move = m5_post[2]["c"] - m5_post[0]["o"]
    if move == 0:
        return {"bias": "NONE", "reason": "first 15-min candle flat — no news bias"}
    b = "UP" if move > 0 else "DOWN"
    return {"bias": b, "surprise": round(move, 2), "reason": f"first 15-min candle {move:+.2f} → XAU {b}"}


def pre_release_range(m5: list, t_release: float, minutes: int = NEWS_PRE_RANGE_MIN) -> tuple[float, float] | None:
    bars = [b for b in m5 if t_release - minutes * 60 <= int(b["t"]) < t_release]
    if not bars:
        return None
    return max(b["h"] for b in bars), min(b["l"] for b in bars)


def news_confirmation(bias: str, pre_range: tuple | None, m5_post: list, t_release: float) -> dict:
    """The first M5 close OUTSIDE the pre-release range within 15 min of the release must be in the bias direction.
    confirmed: True | False (opposite side first, or nothing within 15 min) | None (still waiting)."""
    if not pre_range:
        return {"confirmed": False, "reason": "no pre-release range"}
    hi, lo = pre_range
    for b in m5_post:
        if int(b["t"]) >= t_release + NEWS_CONFIRM_MIN * 60:
            break
        if b["c"] > hi:
            return {"confirmed": bias == "UP", "first_move": "UP", "reason": f"first M5 close {b['c']:.2f} above pre-release high {hi:.2f}"}
        if b["c"] < lo:
            return {"confirmed": bias == "DOWN", "first_move": "DOWN", "reason": f"first M5 close {b['c']:.2f} below pre-release low {lo:.2f}"}
    done = bool(m5_post) and int(m5_post[-1]["t"]) + 300 >= t_release + NEWS_CONFIRM_MIN * 60
    return {"confirmed": False if done else None, "first_move": None,
            "reason": "no M5 close beyond the pre-release range in the first 15 min" if done else "waiting for the first move"}


def detect_news_entry(m5: list, m5_post: list, bias: str, pre_close: float) -> dict | None:
    """First pullback in the bias direction: the last completed bar touched EMA20(M5) or the 38–50 % retrace of the
    initial post-release move and CLOSED with the bias. Returns {trigger: 'news_pullback', ...} or None."""
    if bias not in ("UP", "DOWN") or len(m5_post) < 2 or len(m5) < 21:
        return None
    up, sgn = bias == "UP", 1 if bias == "UP" else -1
    last = m5_post[-1]
    if (last["c"] - last["o"]) * sgn <= 0:
        return None
    ext = max(b["h"] for b in m5_post[:-1]) if up else min(b["l"] for b in m5_post[:-1])
    move = (ext - pre_close) * sgn
    if move <= 0:
        return None
    r38, r50 = ext - sgn * 0.382 * move, ext - sgn * 0.5 * move
    e20 = ema([b["c"] for b in m5], 20)[-1]
    touch = last["l"] if up else last["h"]
    in_zone = (r50 - TOUCH_TOL <= touch <= r38 + TOUCH_TOL) if up else (r38 - TOUCH_TOL <= touch <= r50 + TOUCH_TOL)
    on_ema = abs(touch - e20) <= TOUCH_TOL
    if not (in_zone or on_ema):
        return None
    swing = min(b["l"] for b in m5_post[-3:]) if up else max(b["h"] for b in m5_post[-3:])
    return {"trigger": "news_pullback", "direction": "BUY" if up else "SELL", "entry": last["c"], "swing": swing, "extreme": round(ext, 2),
            "retrace": {"r38": round(r38, 2), "r50": round(r50, 2), "ema20": round(e20, 2)},
            "reason": f"first pullback to {'the 38–50 % retrace' if in_zone else 'EMA20(M5)'} of the {move:.2f} post-release move, M5 closed with the news bias"}


def plan_news_exit(direction: str, entry: float, swing: float, atr_post: float) -> dict:
    """TP clamp(0.7×ATR14 post-release, 3.00, 8.00); SL beyond the pullback / sweep extreme (+0.30), max 1.3×TP; 60-min time-stop."""
    tp = max(NEWS_TP_MIN, min(NEWS_TP_MAX, NEWS_TP_ATR * atr_post))
    sl = abs(entry - swing) + SL_BUFFER
    if sl > SL_MAX_TP_MULT * tp:
        return {"ok": False, "skip": f"SL too wide for target (SL {sl:.2f} > {SL_MAX_TP_MULT}×TP {tp:.2f})", "tp": tp, "sl": sl}
    sgn = 1 if direction == "BUY" else -1
    return {"ok": True, "tp": round(tp, 2), "sl": round(sl, 2), "take_profit": round(entry + sgn * tp, 2), "stop_loss": round(entry - sgn * sl, 2),
            "time_stop_min": NEWS_TIME_STOP_MIN}


def news_pre_action(direction: str, entry: float, sl_dist: float, price: float, policy: str = "protect") -> dict:
    """T−15 → T: ≥ +0.5R → stop to breakeven+costs. Otherwise: protect → keep the stop; close → close now;
    tighten → stop at −0.5R."""
    r_now = ((price - entry) * (1 if direction == "BUY" else -1)) / sl_dist if sl_dist else 0.0
    if r_now >= NEWS_PROTECT_R:
        return {"action": "breakeven", "r_now": round(r_now, 2)}
    pol = policy if policy in NEWS_PRE_ACTIONS else "protect"
    if pol == "close":
        return {"action": "close", "r_now": round(r_now, 2)}
    if pol == "tighten":
        sgn = 1 if direction == "BUY" else -1
        return {"action": "tighten", "r_now": round(r_now, 2), "new_sl": round(entry - sgn * NEWS_TIGHTEN_R * sl_dist, 2)}
    return {"action": "keep", "r_now": round(r_now, 2)}


def news_plan_text(event: dict, kind: str, release_min: int = NEWS_RELEASE_MIN_DEFAULT, now: datetime | None = None) -> str:
    """v1.60.62 #82 — the weekday + date is shown whenever the event is not today (UTC), so Saturday's card reads
    "FOMC Wed 28 Oct 18:00 UTC — …" instead of looking like an event due in a few hours."""
    t = datetime.fromtimestamp(float(event["when_ts"]), tz=timezone.utc)
    pre = datetime.fromtimestamp(float(event["when_ts"]) - NEWS_PRE_MIN * 60, tz=timezone.utc)
    post = datetime.fromtimestamp(float(event["when_ts"]) + release_min * 60, tz=timezone.utc)
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    when = f"{t:%H:%M}" if t.date() == today else f"{t:%a} {t.day} {t:%b} {t:%H:%M}"
    return f"{kind} {when} UTC — protect at {pre:%H:%M}, news trades from {post:%H:%M}"


def next_news_event(events: list, now: datetime, event_list: list | None = None, release_min: int = NEWS_RELEASE_MIN_DEFAULT) -> dict | None:
    """The next configured USD event (or the one in progress) for the Market Intel card."""
    cands = []
    for e in events or []:
        if not _news_candidate(e):
            continue
        kind = news_event_kind(e.get("title") or "", event_list)
        if kind and float(e["when_ts"]) >= now.timestamp() - NEWS_POST_MIN * 60:
            cands.append((float(e["when_ts"]), kind, e))
    if not cands:
        return None
    t, kind, e = min(cands, key=lambda x: x[0])
    ph = news_phase([e], now, event_list, release_min)
    return {"kind": kind, "title": e.get("title"), "time_utc": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
            "forecast": e.get("forecast"), "previous": e.get("previous"), "actual": e.get("actual") or None,
            "phase": ph["phase"] if ph else None, "plan": news_plan_text(e, kind, release_min, now)}
