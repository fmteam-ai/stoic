"""gold_trend_pulse · pure rules (no I/O). XAUUSD only; every distance is in PRICE units so 2- and 3-decimal
brokers behave identically. 1 retail pip = 0.10 price units ($0.10 on 0.01 lot); TP band 2.00–4.50 = 20–45 pips."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

ENGINE = "gold_trend_pulse"
TOUCH_TOL = 0.30            # pullback touch tolerance to EMA20(M5) / session VWAP
SPIKE_ATR_MULT = 2.5        # last M5 range > 2.5 × ATR14 → don't chase
TP_ATR_MULT, TP_MIN, TP_MAX = 0.7, 2.00, 4.50
SL_BUFFER, SL_MAX_TP_MULT = 0.30, 1.3
BE_TRIGGER_FRAC = 0.60
TIME_STOP_MIN = 45
SPREAD_MAX_FRAC_OF_TP = 0.25
COOLDOWN_WIN_MIN, COOLDOWN_LOSS_MIN = 3, 15
DAILY_MAX_CONSEC_LOSSES, DAILY_MAX_LOSS_PCT, DAILY_MAX_TRADES = 3, 1.5, 15
RISK_PCT = 0.25
BIAS_REFRESH_MIN, BIAS_CONFIRMATIONS = 15, 2
NEWS_BLACKOUT_MIN = 15
GATE_MIN_TRADES, GATE_MIN_PF, GATE_MAX_DD_R = 100, 1.2, 5.0


# ------------------------------------------------------------------------------------------- indicators
def ema(values: list, period: int) -> list:
    out, k, prev = [], 2.0 / (period + 1), None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: list, period: int = 7) -> list:
    out = [50.0] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars: list, period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else 0.0


def resample(bars: list, seconds: int) -> list:
    """OHLC bars [{t,o,h,l,c,(v)}] (any lower TF, UTC epoch t) → bars of `seconds`."""
    out: dict = {}
    for b in bars:
        key = int(b["t"]) // seconds * seconds
        g = out.get(key)
        if g is None:
            out[key] = {"t": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or b.get("n") or 0}
        else:
            g["h"] = max(g["h"], b["h"]); g["l"] = min(g["l"], b["l"]); g["c"] = b["c"]; g["v"] += b.get("v") or b.get("n") or 0
    return [out[k] for k in sorted(out)]


def m5_from_m1(m1: list) -> list:
    return resample(m1, 300)


def completed(bars: list, seconds: int, now_epoch: float) -> list:
    """Drop the forming bar: only bars whose window has fully elapsed."""
    return [b for b in bars if int(b["t"]) + seconds <= now_epoch]


def session_vwap(m5: list, day_start_epoch: int) -> float | None:
    num = den = 0.0
    for b in m5:
        if int(b["t"]) < day_start_epoch:
            continue
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        w = float(b.get("v") or 1.0) or 1.0
        num += tp * w; den += w
    return num / den if den else None


# ------------------------------------------------------------------------------------------- 1. daily bias
def _rising(series: list, lookback: int = 3) -> bool:
    return len(series) > lookback and series[-1] > series[-1 - lookback]


def daily_bias(h4: list, h1: list, price: float, today_open: float | None, prior_day_mid: float | None) -> dict:
    """UP / DOWN / NONE with the four reasons (each {ok, text}). ALL four must agree for a direction."""
    if len(h4) < 20 or len(h1) < 20 or not today_open or not prior_day_mid:
        return {"bias": "NONE", "reasons": [{"ok": False, "text": "insufficient H4/H1/daily data"}]}
    e4, e1 = ema([b["c"] for b in h4], 50), ema([b["c"] for b in h1], 50)
    c4, c1 = h4[-1]["c"], h1[-1]["c"]
    checks_up = [(c4 > e4[-1] and _rising(e4), f"H4 close {c4:.2f} vs EMA50 {e4[-1]:.2f} ({'rising' if _rising(e4) else 'falling'})"),
                 (c1 > e1[-1] and _rising(e1), f"H1 close {c1:.2f} vs EMA50 {e1[-1]:.2f} ({'rising' if _rising(e1) else 'falling'})"),
                 (price > today_open, f"price {price:.2f} vs today's open {today_open:.2f}"),
                 (price > prior_day_mid, f"price {price:.2f} vs prior-day mid {prior_day_mid:.2f}")]
    checks_dn = [(c4 < e4[-1] and not _rising(e4) and len(e4) > 3 and e4[-1] < e4[-4], checks_up[0][1]),
                 (c1 < e1[-1] and len(e1) > 3 and e1[-1] < e1[-4], checks_up[1][1]),
                 (price < today_open, checks_up[2][1]), (price < prior_day_mid, checks_up[3][1])]
    if all(ok for ok, _ in checks_up):
        return {"bias": "UP", "reasons": [{"ok": True, "text": t} for _, t in checks_up]}
    if all(ok for ok, _ in checks_dn):
        return {"bias": "DOWN", "reasons": [{"ok": True, "text": t} for _, t in checks_dn]}
    up_ok, dn_ok = [ok for ok, _ in checks_up], [ok for ok, _ in checks_dn]
    lean = up_ok if sum(up_ok) >= sum(dn_ok) else dn_ok          # reasons are reported against the side that leans
    return {"bias": "NONE", "reasons": [{"ok": lean[i], "text": checks_up[i][1]} for i in range(4)]}


@dataclass
class BiasTracker:
    """Hysteresis: the published bias flips only after BIAS_CONFIRMATIONS consecutive 15-min recomputes agree."""
    bias: str = "NONE"
    candidate: str = "NONE"
    confirmations: int = 0
    computed_at: str | None = None
    reasons: list = field(default_factory=list)

    def due(self, now: datetime) -> bool:
        if not self.computed_at:
            return True
        return now - datetime.fromisoformat(self.computed_at) >= timedelta(minutes=BIAS_REFRESH_MIN)

    def update(self, raw: dict, now: datetime) -> str:
        self.computed_at = now.isoformat()
        self.reasons = raw["reasons"]
        b = raw["bias"]
        if b == self.bias:
            self.candidate, self.confirmations = b, 0
            return self.bias
        if b == self.candidate:
            self.confirmations += 1
        else:
            self.candidate, self.confirmations = b, 1
        if self.confirmations >= BIAS_CONFIRMATIONS:
            self.bias, self.confirmations = b, 0
        return self.bias

    def to_dict(self) -> dict:
        return {"bias": self.bias, "candidate": self.candidate, "confirmations": self.confirmations,
                "computed_at": self.computed_at, "reasons": self.reasons}

    @classmethod
    def from_dict(cls, d: dict | None) -> "BiasTracker":
        d = d or {}
        return cls(bias=d.get("bias") or "NONE", candidate=d.get("candidate") or "NONE",
                   confirmations=int(d.get("confirmations") or 0), computed_at=d.get("computed_at"), reasons=d.get("reasons") or [])


# ------------------------------------------------------------------------------------------- 2. entries
def detect_entry(m5: list, bias: str, vwap: float | None) -> dict | None:
    """Completed M5 bars only. Returns {trigger: 'a'|'b', direction, entry, swing, reason} or None.
    (a) pullback: touch of EMA20(M5) or VWAP within TOUCH_TOL in the last 3 bars, RSI(7) dipped < 40 (> 60 for DOWN)
        and turned back, and the last bar CLOSED in the trend direction;
    (b) micro-breakout: same pullback, and the last close breaks the high (low) of the 3 bars before it."""
    if bias not in ("UP", "DOWN") or len(m5) < 25:
        return None
    closes = [b["c"] for b in m5]
    e20, r7, a14 = ema(closes, 20), rsi(closes, 7), atr(m5, 14)
    last = m5[-1]
    if a14 > 0 and (last["h"] - last["l"]) > SPIKE_ATR_MULT * a14:
        return {"blocked": f"spike: last M5 range {last['h'] - last['l']:.2f} > {SPIKE_ATR_MULT}×ATR14 {a14:.2f}"}
    recent = m5[-4:-1]                             # the 3 bars before the signal bar = the pullback
    up = bias == "UP"
    touched = any(abs((b["l"] if up else b["h"]) - e20[i]) <= TOUCH_TOL or
                  (vwap is not None and abs((b["l"] if up else b["h"]) - vwap) <= TOUCH_TOL)
                  for i, b in zip(range(len(m5) - 4, len(m5) - 1), recent))
    if not touched:
        return None
    dipped = any(r < 40 for r in r7[-4:-1]) if up else any(r > 60 for r in r7[-4:-1])
    turned = (r7[-1] > r7[-2]) if up else (r7[-1] < r7[-2])
    if not (dipped and turned):
        return None
    closed_with_trend = last["c"] > last["o"] if up else last["c"] < last["o"]
    if not closed_with_trend:
        return None
    swing = min(b["l"] for b in recent + [last]) if up else max(b["h"] for b in recent + [last])
    brk = (last["c"] > max(b["h"] for b in recent)) if up else (last["c"] < min(b["l"] for b in recent))
    return {"trigger": "b" if brk else "a", "direction": "BUY" if up else "SELL", "entry": last["c"], "swing": swing,
            "atr": a14, "rsi": round(r7[-1], 1), "ema20": round(e20[-1], 2), "vwap": vwap,
            "reason": ("micro-breakout of the last 3 M5 bars after a pullback" if brk else "pullback to EMA20/VWAP, RSI(7) turned, M5 closed with trend")}


# ------------------------------------------------------------------------------------------- 3. exits
def plan_exit(direction: str, entry: float, swing: float, atr14: float) -> dict:
    tp = max(TP_MIN, min(TP_MAX, TP_ATR_MULT * atr14))
    sl = abs(entry - swing) + SL_BUFFER
    if sl > SL_MAX_TP_MULT * tp:
        return {"ok": False, "skip": f"SL too wide for target (SL {sl:.2f} > {SL_MAX_TP_MULT}×TP {tp:.2f})", "tp": tp, "sl": sl}
    sign = 1 if direction == "BUY" else -1
    return {"ok": True, "tp": round(tp, 2), "sl": round(sl, 2), "take_profit": round(entry + sign * tp, 2),
            "stop_loss": round(entry - sign * sl, 2), "be_trigger": round(entry + sign * BE_TRIGGER_FRAC * tp, 2)}


def breakeven_level(direction: str, entry: float, spread: float, commission_price: float) -> float:
    """Stop at entry ± (spread + commission) once price reached 60 % of TP — the trade can never close negative."""
    off = max(0.0, float(spread or 0)) + max(0.0, float(commission_price or 0))
    return round(entry + off if direction == "BUY" else entry - off, 2)


def breakeven_due(direction: str, entry: float, tp_dist: float, price: float) -> bool:
    gain = (price - entry) if direction == "BUY" else (entry - price)
    return gain >= BE_TRIGGER_FRAC * tp_dist


def time_stop_due(opened_at: datetime, now: datetime) -> bool:
    return now - opened_at >= timedelta(minutes=TIME_STOP_MIN)


# ------------------------------------------------------------------------------------------- 4. filters
def spread_ok(spread: float, tp: float) -> tuple[bool, str]:
    lim = SPREAD_MAX_FRAC_OF_TP * tp
    return (spread <= lim, f"spread {spread:.2f} vs limit {lim:.2f} (25 % of TP {tp:.2f})")


def session_ok(now: datetime) -> tuple[bool, str]:
    wd, hm = now.weekday(), now.hour + now.minute / 60.0
    if wd >= 5:
        return False, "weekend"
    end = 18 if wd == 4 else 20
    ok = 7 <= hm < end
    return ok, f"session 07:00–{end:02d}:00 UTC {'open' if ok else 'closed'} (now {now:%H:%M} UTC)"


def cooldown_until(last_outcome: str | None, last_closed_at: datetime | None) -> datetime | None:
    if not last_outcome or not last_closed_at:
        return None
    mins = COOLDOWN_LOSS_MIN if last_outcome == "loss" else COOLDOWN_WIN_MIN
    return last_closed_at + timedelta(minutes=mins)


def daily_stop(consec_losses: int, day_pnl_pct: float, trades_today: int) -> str | None:
    if consec_losses >= DAILY_MAX_CONSEC_LOSSES:
        return f"daily stop: {consec_losses} consecutive losses"
    if day_pnl_pct <= -DAILY_MAX_LOSS_PCT:
        return f"daily stop: {day_pnl_pct:.2f}% equity today"
    if trades_today >= DAILY_MAX_TRADES:
        return f"daily cap: {trades_today}/{DAILY_MAX_TRADES} trades"
    return None


def news_blackout(events: list, now: datetime, minutes: int = NEWS_BLACKOUT_MIN) -> str | None:
    """events: [{country, impact, when_ts}] — USD HIGH-impact within ±minutes."""
    for e in events or []:
        if str(e.get("country") or "").upper() != "USD" or str(e.get("impact") or "").lower() != "high":
            continue
        ts = e.get("when_ts")
        if ts is None:
            continue
        if abs(now.timestamp() - float(ts)) <= minutes * 60:
            return f"USD high-impact event {e.get('title') or ''} within ±{minutes} min"
    return None


# ------------------------------------------------------------------------------------------- 5. sizing
def size_lots(equity: float, sl_price_dist: float, pip_usd_per_lot: float, pip_price: float,
              min_lot: float = 0.01, max_lot: float | None = None, lot_step: float = 0.01) -> dict:
    """Fixed RISK_PCT of equity from the SL distance via the authoritative pip value (#40/#42 contract logic)."""
    risk_usd = equity * RISK_PCT / 100.0
    if sl_price_dist <= 0 or pip_usd_per_lot <= 0 or pip_price <= 0:
        return {"ok": False, "reason": "risk unknown (pip value / SL distance)", "risk_usd": round(risk_usd, 2), "risk_pct": RISK_PCT}
    usd_per_lot = (sl_price_dist / pip_price) * pip_usd_per_lot
    raw = risk_usd / usd_per_lot
    lots = int(raw / lot_step + 1e-9) * lot_step
    if lots < min_lot:
        if min_lot * usd_per_lot > risk_usd * 1.5:
            return {"ok": False, "reason": f"min lot {min_lot} risks ${min_lot * usd_per_lot:.2f} > budget ${risk_usd:.2f}",
                    "risk_usd": round(risk_usd, 2), "risk_pct": RISK_PCT}
        lots = min_lot
    if max_lot and lots > max_lot:
        lots = max_lot
    lots = round(lots, 2)
    return {"ok": True, "lots": lots, "risk_usd": round(lots * usd_per_lot, 2), "risk_pct": round(lots * usd_per_lot / equity * 100, 3) if equity else None,
            "planned_risk_usd": round(risk_usd, 2), "planned_risk_pct": RISK_PCT}


# ------------------------------------------------------------------------------------------- 6. live gate
def live_gate(stats: dict) -> dict:
    """≥100 labeled shadow trades, net expectancy > 0 after costs, PF > 1.2, max drawdown < 5R."""
    n = int(stats.get("n") or 0)
    reasons = []
    if n < GATE_MIN_TRADES:
        reasons.append(f"{n}/{GATE_MIN_TRADES} labeled shadow trades")
    if (stats.get("expectancy_r") or 0) <= 0:
        reasons.append(f"net expectancy {stats.get('expectancy_r', 0):+.3f}R after costs (needs > 0)")
    pf = stats.get("profit_factor")
    if pf is None or pf <= GATE_MIN_PF:
        reasons.append(f"profit factor {pf if pf is not None else 'n/a'} (needs > {GATE_MIN_PF})")
    if (stats.get("max_dd_r") or 0) >= GATE_MAX_DD_R:
        reasons.append(f"max drawdown {stats.get('max_dd_r', 0):.1f}R (needs < {GATE_MAX_DD_R}R)")
    return {"eligible": not reasons, "reasons": reasons, "progress": min(n, GATE_MIN_TRADES), "needed": GATE_MIN_TRADES}


def outcome_stats(rows: list) -> dict:
    """rows: [{r, pnl_usd?}] labeled outcomes in time order → n, win_rate, pf, expectancy_r, max_dd_r, avg win/loss $."""
    rs = [float(x.get("r") or 0) for x in rows]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    peak = dd = cum = 0.0
    for r in rs:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    usd_w = [float(x["pnl_usd"]) for x in rows if x.get("pnl_usd") is not None and float(x["pnl_usd"]) > 0]
    usd_l = [float(x["pnl_usd"]) for x in rows if x.get("pnl_usd") is not None and float(x["pnl_usd"]) <= 0]
    return {"n": len(rs), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(rs), 3) if rs else None,
            "profit_factor": (round(gp / gl, 2) if gl > 0 else (None if not wins else 99.0)),
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0, "max_dd_r": round(dd, 2),
            "avg_win_usd": round(sum(usd_w) / len(usd_w), 2) if usd_w else None,
            "avg_loss_usd": round(sum(usd_l) / len(usd_l), 2) if usd_l else None}


# ------------------------------------------------------------------------------------------- 11. layering
LAYER_MAX_LEGS, BASKET_RISK_PCT_MAX, LAYER_L3_ATR, DAILY_MAX_LEGS = 3, 0.75, 1.0, 15


def basket_shared_stop(direction: str, m15_today: list, lookback: int = 12) -> float | None:
    """ONE hard stop for every leg: beyond the day's last swing extreme on M15 (+ SL_BUFFER)."""
    bars = m15_today[-lookback:]
    if not bars:
        return None
    return round(min(b["l"] for b in bars) - SL_BUFFER, 2) if direction == "BUY" else round(max(b["h"] for b in bars) + SL_BUFFER, 2)


def layer_levels(direction: str, ema20: float, vwap: float | None, atr14: float) -> list:
    """[(leg, level)] L1 EMA20(M5), L2 session VWAP, L3 = L1 − 1.0×ATR (mirror for SELL). Deeper legs only."""
    sgn = 1 if direction == "BUY" else -1
    lv = [("L1", round(ema20, 2))]
    if vwap is not None and (ema20 - vwap) * sgn > 0:
        lv.append(("L2", round(vwap, 2)))
    lv.append(("L3", round(ema20 - sgn * LAYER_L3_ATR * atr14, 2)))
    return lv[:LAYER_MAX_LEGS]


def leg_touched(direction: str, bar: dict, level: float) -> bool:
    return (bar["l"] <= level + TOUCH_TOL) if direction == "BUY" else (bar["h"] >= level - TOUCH_TOL)


def size_basket_leg(equity: float, entry: float, shared_stop: float, pip_usd_per_lot: float, pip_price: float,
                    risk_used_pct: float, min_lot: float = 0.01, max_lot: float | None = None) -> dict:
    """A leg is sized at RISK_PCT to the SHARED stop and skipped when the basket would exceed BASKET_RISK_PCT_MAX."""
    dist = abs(entry - shared_stop)
    s = size_lots(equity, dist, pip_usd_per_lot, pip_price, min_lot=min_lot, max_lot=max_lot)
    if not s["ok"]:
        return s
    if risk_used_pct + float(s["risk_pct"] or 0) > BASKET_RISK_PCT_MAX + 1e-9:
        return {"ok": False, "reason": f"basket risk {risk_used_pct + s['risk_pct']:.2f}% > {BASKET_RISK_PCT_MAX}% — leg skipped",
                "risk_usd": s["risk_usd"], "risk_pct": s["risk_pct"]}
    return s


def basket_invalidated(direction: str, bias: str, m15_last_close: float | None, shared_stop: float) -> str | None:
    if bias == "NONE" or (bias == "UP") != (direction == "BUY"):
        return f"bias flipped to {bias} — basket closed"
    if m15_last_close is not None and ((m15_last_close < shared_stop) if direction == "BUY" else (m15_last_close > shared_stop)):
        return "M15 close beyond the shared stop structure — basket closed"
    return None


def basket_stats(baskets: list, equity: float) -> dict:
    """baskets: [{legs: [{r, pnl_usd}]}] → legs per basket, basket win rate, worst basket loss $ / %."""
    if not baskets:
        return {"baskets": 0}
    pnls = [sum(float(l.get("pnl_usd") or 0) for l in b["legs"]) for b in baskets]
    worst = min(pnls)
    return {"baskets": len(baskets), "legs_per_basket": round(sum(len(b["legs"]) for b in baskets) / len(baskets), 2),
            "basket_win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3), "worst_basket_loss_usd": round(worst, 2),
            "worst_basket_loss_pct": round(worst / equity * 100, 3) if equity else None, "net_usd": round(sum(pnls), 2)}


# ------------------------------------------------------------------------------------------- 12. range mode
RANGE_LOOKBACK_SEC, RANGE_BAND_ATR, RANGE_MIN_W_ATR, RANGE_MAX_W_ATR = 7200, 0.2, 2.5, 8.0
RANGE_EDGE_FRAC, RANGE_TP_ATR, RANGE_TP_MIN, RANGE_TP_MAX, RANGE_TIME_STOP_MIN, RANGE_WICK_FRAC = 0.15, 0.5, 1.00, 3.00, 20, 0.5


def detect_range(m5: list, m15: list, now_epoch: float) -> dict | None:
    """Confirmed M5 range over the last 2 h: ≥2 touches of each band (2 h high/low ∓ 0.2×ATR14), width 2.5–8×ATR,
    and no M15 close outside the band in the last 30 min."""
    win = [b for b in m5 if int(b["t"]) >= now_epoch - RANGE_LOOKBACK_SEC]
    if len(win) < 12:
        return None
    a = atr(m5, 14)
    if a <= 0:
        return None
    hi, lo = max(b["h"] for b in win), min(b["l"] for b in win)
    width = hi - lo
    if not (RANGE_MIN_W_ATR * a <= width <= RANGE_MAX_W_ATR * a):
        return None
    band = RANGE_BAND_ATR * a
    if sum(1 for b in win if b["h"] >= hi - band) < 2 or sum(1 for b in win if b["l"] <= lo + band) < 2:
        return None
    for b in m15:
        if int(b["t"]) >= now_epoch - 1800 and (b["c"] > hi + band or b["c"] < lo - band):
            return None
    return {"hi": round(hi, 2), "lo": round(lo, 2), "mid": round((hi + lo) / 2, 2), "atr": round(a, 3), "width": round(width, 2),
            "top": round(hi - RANGE_EDGE_FRAC * width, 2), "bottom": round(lo + RANGE_EDGE_FRAC * width, 2)}


def regime(bias: str, m5: list, m15: list, now_epoch: float) -> dict:
    if bias in ("UP", "DOWN"):
        return {"regime": "TREND", "range": None}
    rng = detect_range(m5, m15, now_epoch)
    return {"regime": "RANGE" if rng else "NO_TRADE", "range": rng}


def detect_range_entry(m1_bar: dict, rng: dict) -> dict | None:
    """SELL: M1 bar reached the top 15 % of the range and CLOSED back inside it with an upper wick ≥ 50 % of the
    candle; BUY = mirror in the bottom 15 %."""
    size = m1_bar["h"] - m1_bar["l"]
    if size <= 0:
        return None
    o, c = m1_bar["o"], m1_bar["c"]
    if m1_bar["h"] >= rng["top"] and c < rng["top"] and (m1_bar["h"] - max(o, c)) / size >= RANGE_WICK_FRAC:
        return {"direction": "SELL", "entry": c, "reason": "rejection from the top 15 % of the range"}
    if m1_bar["l"] <= rng["bottom"] and c > rng["bottom"] and (min(o, c) - m1_bar["l"]) / size >= RANGE_WICK_FRAC:
        return {"direction": "BUY", "entry": c, "reason": "rejection from the bottom 15 % of the range"}
    return None


def plan_range_exit(direction: str, entry: float, rng: dict) -> dict:
    a = rng["atr"]
    sgn = 1 if direction == "BUY" else -1
    tp = min(abs(rng["mid"] - entry), max(RANGE_TP_MIN, min(RANGE_TP_MAX, RANGE_TP_ATR * a)))
    edge = rng["lo"] if direction == "BUY" else rng["hi"]
    sl = abs(entry - edge) + max(SL_BUFFER, 0.25 * a)
    if tp <= 0 or sl > SL_MAX_TP_MULT * tp:
        return {"ok": False, "skip": f"SL too wide for target (SL {sl:.2f} > {SL_MAX_TP_MULT}×TP {tp:.2f})", "tp": tp, "sl": sl}
    return {"ok": True, "tp": round(tp, 2), "sl": round(sl, 2), "take_profit": round(entry + sgn * tp, 2), "stop_loss": round(entry - sgn * sl, 2),
            "time_stop_min": RANGE_TIME_STOP_MIN}


def range_breakout(m5_last: dict, rng: dict) -> str | None:
    if m5_last["c"] > rng["hi"]:
        return "UP"
    if m5_last["c"] < rng["lo"]:
        return "DOWN"
    return None


def wrong_side(direction: str, breakout: str) -> bool:
    return (direction == "SELL") if breakout == "UP" else (direction == "BUY")


# ------------------------------------------------------------------------------------------- 13 / 14 (separate module)
from gold_trend_pulse.rules_news_sweep import (  # noqa: E402,F401
    NEWS_EVENTS_DEFAULT, NEWS_MAX_TRADES_PER_EVENT, NEWS_POST_MIN, NEWS_PRE_ACTIONS, NEWS_PRE_MIN, NEWS_RATE_EVENTS,
    NEWS_RELEASE_MIN_DEFAULT, NEWS_SURPRISE_THRESHOLDS, NEWS_TIME_STOP_MIN, NEWS_TP_MAX, NEWS_TP_MIN, RUNNER_TIME_STOP_MIN,
    SWEEP_DROP_ATR, detect_news_entry, detect_sweep_reclaim, fomc_bias, news_confirmation, news_event_kind, news_phase,
    news_plan_text, news_pre_action, news_surprise_bias, next_news_event, plan_news_exit, plan_sweep_exit, pre_release_range,
    split_runner_lots,
)
