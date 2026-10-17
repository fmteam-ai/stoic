"""v1.60.60 #71 — Gold Daily Bias + news plan (shadow first).

compute_bias() is pure: fixed weights, each input normalised to [-1, +1]; a missing input counts as 0 and is listed
under `unavailable` — it never fails open into a direction. gather_inputs()/refresh() read only data the bot already
stores. Gating helpers are behind BotConfig flags that default OFF (gold_bias_gtp_direction_enabled,
gold_bias_block_counter_enabled); with the flags off the bias has NO trading effect (shadow log only).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WEIGHTS: dict[str, int] = {          # fixed; sums to 100
    "d1_h4_trend": 25, "dxy": 15, "real_yields": 15, "news_sentiment": 10, "fed_tone": 10,
    "risk_regime": 10, "cot": 5, "calendar_surprise": 10,
}
LONG_AT, SHORT_AT = 20, -20
STRONG = 60
REFRESH_SEC = 900
STATE_ID = "gold_bias"

# surprise sign for gold: hotter USD data / hawkish Fed → USD up → gold down
SURPRISE_SIGN = {"cpi": -1, "core cpi": -1, "nfp": -1, "non-farm": -1, "nonfarm": -1, "fomc": -1, "rate decision": -1,
                 "ppi": -1, "retail sales": -1, "gdp": -1, "pce": -1, "ism": -1, "unemployment rate": +1}


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def compute_bias(inputs: dict, weights: dict | None = None) -> dict:
    """inputs: {name: value in [-1,1] | None} (+ optional f"{name}_why" strings). Returns bias/score/reasons."""
    w = weights or WEIGHTS
    score = 0.0
    reasons, unavailable, components = [], [], {}
    for name, weight in w.items():
        v = inputs.get(name)
        if v is None:
            unavailable.append(name)
            components[name] = {"value": None, "weight": weight, "contribution": 0.0}
            continue
        v = _clamp(v)
        c = round(weight * v, 2)
        score += c
        components[name] = {"value": round(v, 3), "weight": weight, "contribution": c}
        why = inputs.get(f"{name}_why")
        if abs(c) >= 1.0:
            reasons.append(f"{name} {'+' if c > 0 else ''}{c:.0f}" + (f" — {why}" if why else ""))
    score = round(_clamp(score, -100, 100), 1)
    bias = "LONG" if score >= LONG_AT else "SHORT" if score <= SHORT_AT else "NEUTRAL"
    if unavailable:
        reasons.append("unavailable: " + ", ".join(unavailable))
    return {"bias": bias, "score": score, "strong": abs(score) >= STRONG, "reasons": reasons, "unavailable": unavailable,
            "components": components, "computed_at": datetime.now(timezone.utc).isoformat()}


# ── input normalisers (pure) ─────────────────────────────────────────────────────────────────────────

def trend_value(closes_d1: list[float] | None, closes_h4: list[float] | None) -> tuple[float | None, str]:
    """Completed D1 (EMA20) and H4 (EMA50) structure → ±1 when both agree, ±0.5 when only one is clear."""
    def ema_dir(closes, n):
        if not closes or len(closes) < n + 1:
            return None
        k = 2 / (n + 1)
        e = closes[0]
        for c in closes[1:]:
            e = c * k + e * (1 - k)
        return 1 if closes[-1] > e else -1 if closes[-1] < e else 0
    d1, h4 = ema_dir(closes_d1, 20), ema_dir(closes_h4, 50)
    if d1 is None and h4 is None:
        return None, "no completed D1/H4 bars"
    vals = [x for x in (d1, h4) if x is not None]
    v = sum(vals) / 2.0
    return _clamp(v), f"D1 {'up' if d1 == 1 else 'down' if d1 == -1 else 'n/a'} · H4 {'up' if h4 == 1 else 'down' if h4 == -1 else 'n/a'}"


def dxy_value(regime: str | None, change_pct: float | None) -> tuple[float | None, str]:
    if regime is None and change_pct is None:
        return None, "DXY cache unavailable"
    v = 0.0
    r = (regime or "").lower()
    if "bull" in r or "strong" in r or "up" in r:
        v = -0.7
    elif "bear" in r or "weak" in r or "down" in r:
        v = 0.7
    if change_pct is not None:
        v = _clamp(v - _clamp(change_pct / 0.5) * 0.3)
    return v, f"DXY {regime or '—'}{f' {change_pct:+.2f}%' if change_pct is not None else ''}"


def real_yield_value(dfii10_now: float | None, dfii10_prev: float | None) -> tuple[float | None, str]:
    if dfii10_now is None or dfii10_prev is None:
        return None, "DFII10 unavailable"
    d = dfii10_now - dfii10_prev
    return _clamp(-d / 0.15), f"10y real yield {dfii10_now:.2f}% ({d:+.2f} vs prior)"


def surprise(actual: float | None, forecast: float | None) -> float | None:
    if actual is None or forecast is None:
        return None
    return actual - forecast


def surprise_value(event_name: str, actual: float | None, forecast: float | None, scale: float = 0.2) -> tuple[float | None, str]:
    s = surprise(actual, forecast)
    if s is None:
        return None, f"{event_name}: actual missing"
    sign = next((v for k, v in SURPRISE_SIGN.items() if k in event_name.lower()), 0)
    if sign == 0:
        return None, f"{event_name}: no gold mapping"
    return _clamp(sign * s / scale), f"{event_name} surprise {s:+.2f} (actual {actual} vs fcst {forecast})"


# ── gating (flags default OFF) ───────────────────────────────────────────────────────────────────────

def gtp_direction_allowed(bias: dict | None, action: str, cfg: dict | None) -> tuple[bool, str]:
    """gold_trend_pulse enters only in the bias direction when gold_bias_gtp_direction_enabled is ON."""
    if not (cfg or {}).get("gold_bias_gtp_direction_enabled"):
        return True, ""
    b = (bias or {}).get("bias")
    if b not in ("LONG", "SHORT"):
        return False, "gold bias NEUTRAL/unknown — gtp waits for a bias"
    if (b == "LONG") != (action == "BUY"):
        return False, f"gold bias {b} ({(bias or {}).get('score')}) — {action} not in bias direction"
    return True, ""


def counter_bias_block(bias: dict | None, action: str, engine: str | None, cfg: dict | None) -> str | None:
    """Block scalp / fast-scalp / breakout entries AGAINST a strong bias (|score| ≥ 60) when the flag is ON."""
    if not (cfg or {}).get("gold_bias_block_counter_enabled"):
        return None
    if engine not in ("hf_scalp", "hf_scalp_fast", "breakout_m15"):
        return None
    b = bias or {}
    if not b.get("strong"):
        return None
    if (b.get("bias") == "LONG" and action == "SELL") or (b.get("bias") == "SHORT" and action == "BUY"):
        return f"gold bias {b['bias']} {b.get('score')} — {action} blocked (counter-bias, |score| ≥ {STRONG})"
    return None


def post_news_entry_ok(*, freeze_active: bool, spread_ok: bool, m5_close: float | None, first5_high: float | None,
                       first5_low: float | None, bias: str | None) -> tuple[bool, str]:
    """After an event: freeze ended AND spread normal AND an M5 close beyond the event's first 5-min range in the bias direction."""
    if freeze_active:
        return False, "news freeze still active"
    if not spread_ok:
        return False, "spread not back to normal"
    if m5_close is None or first5_high is None or first5_low is None:
        return False, "first 5-min range / M5 close unavailable"
    if bias == "LONG" and m5_close > first5_high:
        return True, f"M5 close {m5_close} above first-5m high {first5_high} (bias LONG)"
    if bias == "SHORT" and m5_close < first5_low:
        return True, f"M5 close {m5_close} below first-5m low {first5_low} (bias SHORT)"
    return False, "no M5 close beyond the first 5-min range in the bias direction"


def news_plan(event: dict, open_trades: list[dict], prediction: dict | None, bias: dict | None, pre_policy: str) -> dict:
    """What happens before the freeze: expected volatility, calendar_intel prediction, affected open gold trades."""
    affected = [{"trade_id": str(t.get("_id")), "symbol": t.get("symbol"), "action": t.get("action"),
                 "plan": "close" if pre_policy == "close" else "protect (break-even)"}
                for t in open_trades if "XAU" in str(t.get("symbol") or "").upper() and t.get("status") == "open"]
    return {"event": event.get("event") or event.get("title"), "at": event.get("at") or event.get("ts"),
            "expected_volatility": (prediction or {}).get("expected_move") or (prediction or {}).get("volatility") or "unknown",
            "prediction": prediction or {"note": "calendar_intel has no posterior for this event class yet"},
            "bias": {"bias": (bias or {}).get("bias"), "score": (bias or {}).get("score")},
            "forecast": event.get("forecast"), "previous": event.get("previous"), "actual": event.get("actual"),
            "affected_open_trades": affected, "post_news_rule": "freeze ended + spread normal + M5 close beyond first-5m range in bias direction"}


# ── gathering (best-effort, read-only) ───────────────────────────────────────────────────────────────

async def _closes(db, tf: str, n: int) -> list[float] | None:
    """Completed closes from db.intraday_candles (one doc per user/symbol/timeframe, `bars` array). Any user's XAU doc
    with the requested timeframe is fine (broker bars are the same); fall back to resampling a lower timeframe via bars_util."""
    try:
        doc = await db.intraday_candles.find_one({"symbol": {"$regex": "^XAU"}, "timeframe": tf}, {"bars": 1})
        bars = (doc or {}).get("bars") or []
        if not bars:
            src = {"D1": "H4", "H4": "M15"}.get(tf)
            doc = await db.intraday_candles.find_one({"symbol": {"$regex": "^XAU"}, "timeframe": src}, {"bars": 1}) if src else None
            raw = (doc or {}).get("bars") or []
            if raw:
                from bars_util import resample
                bars = resample(raw, 86400 if tf == "D1" else 14400)
        bars = bars[:-1]                                             # drop the forming bar → completed only
        closes = [float(b.get("close", b.get("c")) if isinstance(b, dict) else b[4]) for b in bars[-n:]]
        return closes or None
    except Exception:  # noqa: BLE001
        return None


async def gather_inputs(db) -> dict:
    out: dict = {}
    d1, h4 = await _closes(db, "D1", 60), await _closes(db, "H4", 120)
    out["d1_h4_trend"], out["d1_h4_trend_why"] = trend_value(d1, h4)
    try:
        from entry_context import cached_dxy
        dx = await cached_dxy()
        out["dxy"], out["dxy_why"] = dxy_value((dx or {}).get("regime"), (dx or {}).get("change_pct"))
    except Exception:  # noqa: BLE001
        out["dxy"], out["dxy_why"] = None, "DXY unavailable"
    try:
        from entry_context import cached_fred_series
        fs = await cached_fred_series() or {}
        ser = fs.get("DFII10") or {}
        vals = ser.get("values") if isinstance(ser, dict) else None
        out["real_yields"], out["real_yields_why"] = real_yield_value(
            float(vals[-1]) if vals else (float(ser) if isinstance(ser, (int, float)) else None),
            float(vals[-6]) if vals and len(vals) >= 6 else None)
    except Exception:  # noqa: BLE001
        out["real_yields"], out["real_yields_why"] = None, "FRED cache unavailable"
    try:
        from news import score_sentiment
        s = await score_sentiment("XAUUSD")
        out["news_sentiment"] = _clamp(float(s["score"])) if s.get("available") and s.get("score") is not None else None
        out["news_sentiment_why"] = s.get("label") or s.get("summary")
    except Exception:  # noqa: BLE001
        out["news_sentiment"] = None
    try:
        from fed_tone import get_fed_tone
        ft = await get_fed_tone()
        out["fed_tone"] = _clamp(-float(ft["score"])) if ft and ft.get("score") is not None else None   # hawkish>0 → gold down
        out["fed_tone_why"] = (ft or {}).get("label")
    except Exception:  # noqa: BLE001
        out["fed_tone"] = None
    try:
        from risk_regime import latest_advisory
        adv = await latest_advisory(db)
        ro = (adv or {}).get("risk_off_score")
        out["risk_regime"] = _clamp(float(ro) / 100.0 if ro is not None and abs(float(ro)) > 1 else float(ro)) if ro is not None else None
        out["risk_regime_why"] = (adv or {}).get("regime") or (adv or {}).get("label")
    except Exception:  # noqa: BLE001
        out["risk_regime"] = None
    try:
        doc = await db.platform_state.find_one({"_id": "cot_gold"}) or {}
        net = doc.get("net_positioning_z") or doc.get("z")
        out["cot"] = _clamp(float(net) / 2.0) if net is not None else None
        out["cot_why"] = f"COT z {float(net):+.2f}" if net is not None else None
    except Exception:  # noqa: BLE001
        out["cot"] = None
    try:
        since = datetime.now(timezone.utc).timestamp() - 72 * 3600
        rows = await db.calendar_outcomes.find({"ts": {"$gte": since}, "surprise_value": {"$ne": None}}).sort("ts", -1).limit(1).to_list(length=1)
        out["calendar_surprise"] = _clamp(float(rows[0]["surprise_value"])) if rows else None
        out["calendar_surprise_why"] = rows[0].get("why") if rows else "no scored HIGH event in 72 h"
    except Exception:  # noqa: BLE001
        out["calendar_surprise"] = None
    return out


async def refresh(db) -> dict:
    try:
        from economic_calendar import record_calendar_outcomes
        await record_calendar_outcomes(db)
    except Exception as e:  # noqa: BLE001
        logger.debug("calendar outcomes skipped: %s", e)
    snap = compute_bias(await gather_inputs(db))
    await db.platform_state.update_one({"_id": STATE_ID}, {"$set": snap}, upsert=True)
    return snap


async def latest(db) -> dict | None:
    try:
        doc = await db.platform_state.find_one({"_id": STATE_ID})
    except Exception:  # noqa: BLE001
        return None
    if not doc:
        return None
    doc.pop("_id", None)
    return doc


async def gold_bias_loop(db=None):
    from database import get_db
    import asyncio
    db = db or get_db()
    while True:
        try:
            await refresh(db)
            await resolve_shadow_outcomes(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("gold_bias refresh failed: %s", e)
        await asyncio.sleep(REFRESH_SEC)


# ── shadow log (no trading effect) ───────────────────────────────────────────────────────────────────

def alignment(bias: dict | None, action: str) -> str:
    b = (bias or {}).get("bias")
    if b not in ("LONG", "SHORT") or action not in ("BUY", "SELL"):
        return "n/a"
    return "with" if (b == "LONG") == (action == "BUY") else "against"


async def shadow_log(db, signal: dict, bias: dict | None) -> None:
    if not bias or "XAU" not in str(signal.get("symbol") or "").upper() or signal.get("action") not in ("BUY", "SELL"):
        return
    try:
        await db.gold_bias_shadow.insert_one({
            "signal_id": str(signal.get("_id") or signal.get("id") or ""), "user_id": signal.get("user_id"),
            "account_id": signal.get("account_id"), "symbol": signal.get("symbol"), "action": signal.get("action"),
            "engine": signal.get("engine") or signal.get("scope"), "bias": bias.get("bias"), "score": bias.get("score"),
            "alignment": alignment(bias, signal.get("action")), "outcome": None, "pnl": None,
            "created_at": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001
        pass


async def resolve_shadow_outcomes(db) -> int:
    n = 0
    try:
        rows = await db.gold_bias_shadow.find({"outcome": None, "signal_id": {"$ne": ""}}).limit(200).to_list(length=200)
        for r in rows:
            t = await db.trades.find_one({"signal_id": r["signal_id"], "status": "closed"}, {"pnl": 1})
            if t is None:
                continue
            pnl = float(t.get("pnl") or 0)
            await db.gold_bias_shadow.update_one({"_id": r["_id"]}, {"$set": {
                "outcome": "win" if pnl > 0 else "loss" if pnl < 0 else "scratch", "pnl": pnl}})
            n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


async def shadow_summary(db, user_id: str | None = None, days: int = 14) -> dict:
    q: dict = {"outcome": {"$ne": None}}
    if user_id:
        q["user_id"] = user_id
    out = {"with": {"n": 0, "wins": 0, "pnl": 0.0}, "against": {"n": 0, "wins": 0, "pnl": 0.0}, "n/a": {"n": 0, "wins": 0, "pnl": 0.0}}
    try:
        async for r in db.gold_bias_shadow.find(q).limit(2000):
            k = out.setdefault(r.get("alignment") or "n/a", {"n": 0, "wins": 0, "pnl": 0.0})
            k["n"] += 1
            k["wins"] += 1 if r.get("outcome") == "win" else 0
            k["pnl"] = round(k["pnl"] + float(r.get("pnl") or 0), 2)
    except Exception:  # noqa: BLE001
        pass
    return out


def _now_ts() -> float:
    return time.time()
