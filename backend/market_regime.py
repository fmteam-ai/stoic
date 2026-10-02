"""Unified market-regime detection + regime-edge strategy gating (Phase 4).

Markets behave differently. Every bot tick classifies the CURRENT market
along four axes from the user's own candle/calendar streams:

    trend       trending_up | trending_down | ranging
    volatility  high | normal | low          (ATR14 vs its own baseline)
    sentiment   risk_on | risk_off | neutral (cross-asset: crypto+indices
                momentum vs gold momentum, ATR-normalized)
    news        news_driven True/False       (high-impact event ±window or
                active macro freeze)

Snapshots are stamped on every signal AND trade (`market_regime`), so an
edge ledger accumulates: per strategy class × regime bucket. The gate then
only lets a strategy trade when its historical edge fits the live regime —
a strategy with ≥8 stamped trades and a PROVEN negative expectancy in this
exact regime is skipped until conditions change. Unknown = allowed
(fail-open: evidence first, prohibition second).
"""
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol, pip_size
from risk_budget import DEFAULT_ALLOCATIONS

logger = logging.getLogger("market-regime")

CACHE_TTL_SEC = 300
EDGE_MIN_TRADES = 8
EDGE_T_BLOCK = -1.0
EDGE_WINDOW_DAYS = 90
SENT_THRESHOLD = 0.6

_CACHE: dict[str, tuple] = {}


def _atr_series(bars, period=14):
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]),
                       abs(b["l"] - p["c"])))
    out = []
    for i in range(period, len(trs) + 1):
        out.append(sum(trs[i - period:i]) / period)
    return out


def volatility_axis(bars) -> dict:
    atrs = _atr_series(bars)
    if len(atrs) < 20:
        return {"axis": "normal", "ratio": None,
                "detail": "insufficient bars for a volatility baseline"}
    now_atr = atrs[-1]
    baseline = sorted(atrs)[len(atrs) // 2]
    ratio = now_atr / baseline if baseline > 0 else 1.0
    axis = "high" if ratio >= 1.3 else "low" if ratio <= 0.75 else "normal"
    return {"axis": axis, "ratio": round(ratio, 2),
            "detail": f"ATR14 {ratio:.2f}× its median baseline"}


def _momentum_units(bars, lookback=16):
    """Return of the last `lookback` bars in ATR units (scale-free)."""
    if len(bars) < lookback + 15:
        return None
    atrs = _atr_series(bars)
    if not atrs or atrs[-1] <= 0:
        return None
    return (bars[-1]["c"] - bars[-lookback]["c"]) / (atrs[-1] * math.sqrt(lookback))


def sentiment_axis(momenta: dict) -> dict:
    """momenta: {asset_class: momentum_units}. Risk-on = crypto/indices bid
    while gold lags; risk-off = gold bid while risk assets sold."""
    risk_assets = [v for k, v in momenta.items()
                   if k in ("crypto", "indices") and v is not None]
    gold = momenta.get("gold")
    if not risk_assets or gold is None:
        return {"axis": "neutral", "score": None,
                "detail": "needs gold + a risk asset stream (crypto/indices)"}
    risk_m = sum(risk_assets) / len(risk_assets)
    score = risk_m - 0.5 * gold
    axis = ("risk_on" if score >= SENT_THRESHOLD
            else "risk_off" if score <= -SENT_THRESHOLD else "neutral")
    return {"axis": axis, "score": round(score, 2),
            "detail": f"risk assets {risk_m:+.2f} vs gold {gold:+.2f} (ATR units)"}


async def news_axis(symbol="XAUUSD") -> dict:
    try:
        from economic_calendar import macro_freeze_check, upcoming_for
        frz = await macro_freeze_check(symbol)
        if frz.get("frozen"):
            return {"news_driven": True,
                    "detail": f"event freeze ACTIVE — {frz.get('reason')}"}
        ev = await upcoming_for(symbol, hours=1)
        high = [e for e in ev if e.get("impact") == "high"]
        if high:
            mins = max(0, int((float(high[0]["when_ts"]) - time.time()) / 60))
            return {"news_driven": True,
                    "detail": f"high-impact print in {mins}min — "
                              f"{(high[0].get('title') or 'event')[:50]}"}
    except Exception:  # noqa: BLE001
        pass
    return {"news_driven": False, "detail": "no red-flag prints inside 60min"}


def _regime_key(trend: str, vol: str, news: bool) -> str:
    return "news_driven" if news else f"{trend}|{vol}"


REGIME_CLASSES = ("strong_trend", "weak_trend", "range", "breakout",
                  "volatility_expansion", "volatility_contraction",
                  "news_driven", "abnormal")


def regime_probabilities(bars, trend: str, trend_conf: float, vol: dict,
                         news_driven: bool) -> dict:
    """Soft-evidence probability distribution over regime classes instead of
    one absolute label. Returns classes, top, top_p and a normalized-entropy
    uncertainty (0 = certain, 1 = maximally uncertain)."""
    scores = {c: 0.05 for c in REGIME_CLASSES}
    trending = trend in ("trending_up", "trending_down")
    conf = max(0.0, min(1.0, float(trend_conf or 0)))
    if trending:
        scores["strong_trend"] += conf if conf >= 0.7 else conf * 0.5
        scores["weak_trend"] += (1 - conf) * 0.6 if conf >= 0.7 else 0.7
    else:
        scores["range"] += 0.8

    ratio = float(vol.get("ratio") or 1.0)
    if ratio >= 1.25:
        scores["volatility_expansion"] += min(1.0, ratio - 1.0)
    elif ratio <= 0.8:
        scores["volatility_contraction"] += min(1.0, (1.0 - ratio) * 2)

    if bars and len(bars) >= 25:
        ranges = sorted(b["h"] - b["l"] for b in bars[-25:])
        med = ranges[len(ranges) // 2]
        last = bars[-1]
        if med > 0 and (last["h"] - last["l"]) >= 3.0 * med:
            scores["abnormal"] += 1.0
        hi = max(b["h"] for b in bars[-23:-3])
        lo = min(b["l"] for b in bars[-23:-3])
        if any(b["c"] > hi or b["c"] < lo for b in bars[-3:]):
            scores["breakout"] += 0.7
    if ratio >= 2.5:
        scores["abnormal"] += 0.8

    if news_driven:
        scores["news_driven"] += 0.8

    total = sum(scores.values())
    probs = {c: s / total for c, s in scores.items()}
    entropy = -sum(p * math.log(p) for p in probs.values() if p > 0)
    uncertainty = entropy / math.log(len(REGIME_CLASSES))
    top = max(probs, key=probs.get)
    return {"classes": {c: round(p, 3) for c, p in
                        sorted(probs.items(), key=lambda kv: -kv[1])},
            "top": top, "top_p": round(probs[top], 3),
            "uncertainty": round(uncertainty, 3)}


async def detect(db, user_id: str, force: bool = False) -> dict:
    """Unified snapshot, cached 5 min per user."""
    cached = _CACHE.get(user_id)
    if cached and not force and time.time() - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    from strategy_portfolio import _asset_of
    docs = {}
    async for c in db.intraday_candles.find(
            {"user_id": user_id}, {"symbol": 1, "bars": 1, "updated_at": 1}):
        sym = base_symbol(c.get("symbol") or "")
        bars = c.get("bars") or []
        if len(bars) >= 30 and (sym not in docs
                                or len(bars) > len(docs[sym])):
            docs[sym] = bars

    primary_sym = "XAUUSD" if "XAUUSD" in docs else (
        max(docs, key=lambda s: len(docs[s])) if docs else None)

    trend, trend_conf, trend_detail = "ranging", 0.0, "no candle stream"
    vol = {"axis": "normal", "ratio": None, "detail": "no candle stream"}
    if primary_sym:
        bars = docs[primary_sym]
        from scalp.regime import classify
        cls = classify(bars[-24:], pip_size(primary_sym))
        mapping = {"TREND_UP": "trending_up", "TREND_DOWN": "trending_down",
                   "RANGE": "ranging", "VOLATILITY_SHOCK": "ranging"}
        trend = mapping.get(cls.get("regime"), "ranging")
        trend_conf = float(cls.get("confidence") or 0)
        trend_detail = f"{primary_sym}: {cls.get('regime')} — {cls.get('reason', '')}"
        vol = volatility_axis(bars)
        if cls.get("regime") == "VOLATILITY_SHOCK":
            vol = {"axis": "high", "ratio": vol.get("ratio"),
                   "detail": "volatility shock (2h range blowout)"}

    momenta = {}
    for sym, bars in docs.items():
        a = _asset_of(sym)
        m = _momentum_units(bars)
        if m is not None:
            momenta[a] = m if a not in momenta else (momenta[a] + m) / 2
    sent = sentiment_axis(momenta)
    news = await news_axis(primary_sym or "XAUUSD")

    key = _regime_key(trend, vol["axis"], news["news_driven"])
    label_bits = [trend.replace("_", " ").upper(), f"{vol['axis'].upper()} VOL"]
    if sent["axis"] != "neutral":
        label_bits.append(sent["axis"].replace("_", "-").upper())
    if news["news_driven"]:
        label_bits.append("NEWS-DRIVEN")
    snapshot = {
        "key": key,
        "label": " · ".join(label_bits),
        "trend": {"axis": trend, "confidence": round(trend_conf, 2),
                  "detail": trend_detail},
        "volatility": vol,
        "sentiment": sent,
        "news": news,
        "probabilities": regime_probabilities(
            docs.get(primary_sym) if primary_sym else None,
            trend, trend_conf, vol, news["news_driven"]),
        "primary_symbol": primary_sym,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _CACHE[user_id] = (time.time(), snapshot)
    return snapshot


async def strategy_edge(db, user_id: str, regime_key: str) -> dict:
    """Historical edge per strategy class in THIS regime bucket."""
    since = (datetime.now(timezone.utc)
             - timedelta(days=EDGE_WINDOW_DAYS)).isoformat()
    out = {}
    for cls in DEFAULT_ALLOCATIONS:
        pnls = []
        async for t in db.trades.find(
                {"user_id": user_id, "origin": "auto", "status": "closed",
                 "pnl": {"$ne": None}, "closed_at": {"$gte": since},
                 "strategy_class": cls, "market_regime.key": regime_key},
                {"pnl": 1}).limit(1000):
            pnls.append(float(t["pnl"]))
        n = len(pnls)
        if n == 0:
            out[cls] = {"n": 0, "expectancy": None, "allowed": True,
                        "reason": "no history in this regime yet — allowed"}
            continue
        mean = sum(pnls) / n
        sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
        t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else (1.0 if mean > 0 else -1.0)
        wins = sum(1 for p in pnls if p > 0)
        blocked = n >= EDGE_MIN_TRADES and mean < 0 and t_stat <= EDGE_T_BLOCK
        out[cls] = {
            "n": n, "expectancy": round(mean, 2),
            "win_rate": round(wins / n, 2), "t_stat": round(t_stat, 2),
            "allowed": not blocked,
            "reason": (f"proven negative edge in this regime "
                       f"(${mean:.2f}/trade over {n} trades, t={t_stat:.1f})"
                       if blocked else
                       f"${mean:.2f}/trade over {n} trades — edge fits"),
        }
    return out


async def regime_gate(db, user_id: str, cfg: dict,
                      strategy_class: str) -> dict:
    """Per-signal verdict. Fail-open on any error or missing evidence."""
    regime = await detect(db, user_id)
    if not (cfg or {}).get("regime_gating_enabled", True):
        return {"allowed": True, "regime": regime,
                "reason": "regime gating disabled in bot config"}
    edge = await strategy_edge(db, user_id, regime["key"])
    row = edge.get(strategy_class) or {"allowed": True,
                                       "reason": "unbudgeted strategy"}
    return {"allowed": row["allowed"], "regime": regime,
            "edge": row,
            "reason": f"{strategy_class} in {regime['label']}: {row['reason']}"}
