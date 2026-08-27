"""Regime Intelligence 2.0 — a multidimensional Market State Vector
instead of a single TREND/RANGE label, plus a compact regime fingerprint
that strategies and the router condition on."""
import math
from datetime import datetime, timedelta, timezone

DIMS = ["trend", "volatility", "liquidity", "momentum", "mean_reversion",
        "correlation_stress", "news_risk", "spread_stress", "gap_risk"]


def _session(dt: datetime) -> str:
    h = dt.astimezone(timezone.utc).hour
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 21:
        return "newyork"
    return "asia"


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def _atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, low = float(bars[i]["h"]), float(bars[i]["l"])
        pc = float(bars[i - 1]["c"])
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    if not trs:
        return 0.0
    tail = trs[-n:]
    return sum(tail) / len(tail)


def vector_from_bars(bars: list, news_hits_2h: int = 0,
                     correlation_stress: float = 0.0,
                     spread_stress: float = 0.0) -> dict:
    """Pure computation from M15 bars (last ~200)."""
    now = datetime.now(timezone.utc)
    if len(bars) < 30:
        return {"available": False, "session": _session(now),
                "note": f"need ≥30 bars (have {len(bars)})"}
    bars = bars[-200:]
    closes = [float(b["c"]) for b in bars]
    atr = _atr(bars) or 1e-9
    n = len(closes)
    # trend — least-squares slope over the window, in ATR-per-bar units
    xm = (n - 1) / 2.0
    ym = sum(closes) / n
    num = sum((i - xm) * (c - ym) for i, c in enumerate(closes))
    den = sum((i - xm) ** 2 for i in range(n)) or 1.0
    slope = num / den
    trend = _clamp(slope / (atr * 0.25))
    # momentum — 14-bar rate of change in ATR units
    momentum = _clamp((closes[-1] - closes[-15]) / (atr * 3.0))
    # volatility — current ATR percentile within the window's rolling ATRs
    atrs = []
    for i in range(20, len(bars)):
        atrs.append(_atr(bars[max(0, i - 20):i + 1]))
    vol = (sum(1 for a in atrs if a <= atr) / len(atrs)) if atrs else 0.5
    # mean reversion — variance-ratio test (VR<1 → mean-reverting)
    r1 = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)
          if closes[i - 1] > 0]
    r5 = [math.log(closes[i] / closes[i - 5]) for i in range(5, n)
          if closes[i - 5] > 0]
    v1 = sum((x - sum(r1) / len(r1)) ** 2 for x in r1) / len(r1) if r1 else 0
    v5 = sum((x - sum(r5) / len(r5)) ** 2 for x in r5) / len(r5) if r5 else 0
    vr = (v5 / (5 * v1)) if v1 > 0 else 1.0
    mean_reversion = _clamp(1.0 - vr, 0.0, 1.0)
    # liquidity proxy — bar-range consistency (choppy/hollow bars = thin)
    ranges = [float(b["h"]) - float(b["l"]) for b in bars[-50:]]
    mu = sum(ranges) / len(ranges) if ranges else 0
    sd = (sum((r - mu) ** 2 for r in ranges) / len(ranges)) ** 0.5 \
        if ranges else 0
    liquidity = _clamp(1.0 - (sd / mu if mu else 1.0), 0.0, 1.0)
    # gap risk — worst open-vs-prior-close gap (ATR units) recently
    gaps = [abs(float(bars[i]["o"]) - float(bars[i - 1]["c"])) / atr
            for i in range(max(1, len(bars) - 50), len(bars))]
    gap_risk = _clamp(max(gaps) / 3.0 if gaps else 0.0, 0.0, 1.0)
    if now.weekday() == 4 and now.hour >= 18:   # Friday close proximity
        gap_risk = _clamp(gap_risk + 0.3, 0.0, 1.0)
    news_risk = _clamp(0.5 * min(news_hits_2h, 2), 0.0, 1.0)
    vec = {"trend": round(trend, 2), "volatility": round(vol, 2),
           "liquidity": round(liquidity, 2), "momentum": round(momentum, 2),
           "mean_reversion": round(mean_reversion, 2),
           "correlation_stress": round(_clamp(correlation_stress, 0, 1), 2),
           "news_risk": round(news_risk, 2),
           "spread_stress": round(_clamp(spread_stress, 0, 1), 2),
           "gap_risk": round(gap_risk, 2)}
    return {"available": True, "vector": vec, "session": _session(now),
            **fingerprint(vec, _session(now))}


def fingerprint(vec: dict, session: str) -> dict:
    t = abs(vec["trend"])
    labels = ["TRENDING" if t >= 0.35 else "RANGING",
              ("HIGH" if abs(vec["momentum"]) >= 0.5 else
               "MEDIUM" if abs(vec["momentum"]) >= 0.2 else "LOW")
              + " MOMENTUM",
              ("HIGH" if vec["volatility"] >= 0.7 else
               "MEDIUM" if vec["volatility"] >= 0.35 else "LOW")
              + " VOLATILITY",
              ("GOOD" if vec["liquidity"] >= 0.5 else "THIN") + " LIQUIDITY",
              ("HIGH" if max(vec["news_risk"], vec["gap_risk"]) >= 0.5
               else "LOW") + " EVENT RISK"]
    key = "|".join([labels[0][:5],
                    f"MOM_{labels[1][0]}", f"VOL_{labels[2][0]}",
                    f"LIQ_{labels[3][0]}", f"EVT_{labels[4][0]}", session])
    return {"labels": labels, "fingerprint_key": key}


async def _news_hits_next_2h(db, symbol: str) -> int:
    cache = await db.pamm_news_cache.find_one({"_id": "ff_thisweek"})
    if not cache:
        return 0
    now = datetime.now(timezone.utc)
    hi = now + timedelta(hours=2)
    sym = str(symbol or "").upper()
    ccys = {sym[:3], sym[-3:], "USD" if sym.startswith("XAU") else ""}
    hits = 0
    for e in cache.get("events") or []:
        if str(e.get("impact") or "").lower() not in ("high", "red"):
            continue
        if str(e.get("currency") or e.get("country") or "").upper() \
                not in ccys:
            continue
        try:
            t = datetime.fromisoformat(
                str(e.get("date") or e.get("time")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if now <= t <= hi:
            hits += 1
    return hits


async def market_state(db, user_id: str, symbol: str,
                       account_id: str | None = None) -> dict:
    from pip_utils import base_symbol
    base = base_symbol(symbol)
    doc = await db.intraday_candles.find_one(
        {"user_id": user_id, "symbol": base, "timeframe": "M15"},
        {"bars": 1}) or await db.intraday_candles.find_one(
        {"symbol": base, "timeframe": "M15"}, {"bars": 1})
    bars = (doc or {}).get("bars") or []
    news = await _news_hits_next_2h(db, base)
    corr_stress = 0.0
    if account_id:
        try:
            import portfolio_risk
            pos = await portfolio_risk.open_positions(db, account_id)
            if len(pos) >= 2:
                cm = portfolio_risk.correlation_matrix(pos)
                vals = [abs(v) for v in cm.values()] if isinstance(
                    cm, dict) else []
                corr_stress = sum(vals) / len(vals) if vals else 0.0
        except Exception:
            corr_stress = 0.0
    out = vector_from_bars(bars, news_hits_2h=news,
                           correlation_stress=corr_stress)
    out["symbol"] = base
    return out
