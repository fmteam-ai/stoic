"""iter-105 · Liquidity Mapping Agent — institutional order-flow view.

Maps WHERE resting liquidity sits instead of computing lagging indicators:
  · Order blocks (last opposing candle before a displacement move)
  · Stop clusters / resting liquidity (equal highs/lows = pooled stops)
  · Fair value gaps / imbalances (reused from market_structure)
  · Volume profile (POC, value area high/low)
  · Cumulative delta proxy (CLV × volume — buying vs selling pressure)
  · Depth of Market (EA v1.43 streams the live order book where the broker
    provides one — RoboForex supports MarketBookGet for XAUUSD)

`liquidity_gate` vetoes trades fired directly INTO opposing resting
liquidity (e.g. BUY inside an unmitigated supply order block, or BUY into a
heavily ask-stacked book). All detectors fail-open on missing data."""
from datetime import datetime, timezone

from market_structure import find_swings, detect_fvg

OB_KEEP = 6
CLUSTER_KEEP = 8
DOM_FRESH_S = 120
DOM_WALL_IMBALANCE = 0.6


def _atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
    w = trs[-n:]
    return sum(w) / len(w) if w else 0.0


def detect_order_blocks(bars, atr):
    """OB = last opposing candle before a displacement candle that breaks it.
    Only unmitigated blocks (price never closed through the zone) survive."""
    n = len(bars)
    bodies = [abs(b["c"] - b["o"]) for b in bars]
    avg_body = sum(bodies) / max(1, len(bodies))
    thresh = max(1.5 * avg_body, 0.8 * atr)
    obs = []
    for i in range(1, n - 1):
        d = bars[i + 1]
        if abs(d["c"] - d["o"]) < thresh:
            continue
        prev = bars[i]
        if d["c"] > d["o"] and prev["c"] < prev["o"] and d["c"] > prev["h"]:
            obs.append({"dir": "DEMAND", "top": round(prev["h"], 5),
                        "bottom": round(prev["l"], 5), "i": i})
        elif d["c"] < d["o"] and prev["c"] > prev["o"] and d["c"] < prev["l"]:
            obs.append({"dir": "SUPPLY", "top": round(prev["h"], 5),
                        "bottom": round(prev["l"], 5), "i": i})
    out = []
    for ob in obs:
        mitigated = False
        for b in bars[ob["i"] + 2:]:
            if ob["dir"] == "DEMAND" and b["c"] < ob["bottom"]:
                mitigated = True; break
            if ob["dir"] == "SUPPLY" and b["c"] > ob["top"]:
                mitigated = True; break
        if not mitigated:
            out.append(ob)
    return out[-OB_KEEP:]


def _group(points, tol):
    groups = []
    for p in sorted(points, key=lambda x: x["p"]):
        if groups and abs(p["p"] - groups[-1][-1]["p"]) <= tol:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def detect_stop_clusters(bars, price, atr, k=2):
    """Equal highs/lows = pooled stop-losses. BUY_SIDE liquidity rests above
    equal highs (shorts' stops + breakout buys); SELL_SIDE below equal lows.
    A pool is dead once a wick sweeps through it."""
    highs, lows = find_swings(bars, k)
    tol = max(atr * 0.15, 1e-9)
    out = []
    for grp in _group(highs, tol):
        lvl = max(x["p"] for x in grp)
        last_i = max(x["i"] for x in grp)
        if any(b["h"] > lvl + tol for b in bars[last_i + k + 1:]):
            continue
        if lvl <= price:
            continue
        out.append({"side": "BUY_SIDE", "level": round(lvl, 5),
                    "strength": len(grp), "dist": round(lvl - price, 5)})
    for grp in _group(lows, tol):
        lvl = min(x["p"] for x in grp)
        last_i = max(x["i"] for x in grp)
        if any(b["l"] < lvl - tol for b in bars[last_i + k + 1:]):
            continue
        if lvl >= price:
            continue
        out.append({"side": "SELL_SIDE", "level": round(lvl, 5),
                    "strength": len(grp), "dist": round(lvl - price, 5)})
    out.sort(key=lambda c: abs(c["dist"]))
    return out[:CLUSTER_KEEP]


def volume_profile(bars, bins=24):
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        v = float(b.get("v") or 1)
        i0 = min(bins - 1, max(0, int((b["l"] - lo) / step)))
        i1 = min(bins - 1, max(0, int((b["h"] - lo) / step)))
        span = i1 - i0 + 1
        for j in range(i0, i1 + 1):
            vol[j] += v / span
    total = sum(vol) or 1.0
    poc_i = max(range(bins), key=lambda j: vol[j])
    va = vol[poc_i]
    lo_i = hi_i = poc_i
    while va < 0.7 * total and (lo_i > 0 or hi_i < bins - 1):
        vl = vol[lo_i - 1] if lo_i > 0 else -1.0
        vh = vol[hi_i + 1] if hi_i < bins - 1 else -1.0
        if vh >= vl:
            hi_i += 1; va += vol[hi_i]
        else:
            lo_i -= 1; va += vol[lo_i]
    price_at = lambda j: lo + (j + 0.5) * step  # noqa: E731
    return {"poc": round(price_at(poc_i), 5),
            "vah": round(price_at(hi_i), 5),
            "val": round(price_at(lo_i), 5)}


def cumulative_delta(bars):
    """Delta proxy per bar = CLV × volume (close near high = buyers absorbed
    the offer). Bias from the last-third slope; divergence vs price flags
    absorption (price up while delta bleeds = distribution into strength)."""
    if len(bars) < 20:
        return None
    cum, s = [], 0.0
    for b in bars:
        rng = b["h"] - b["l"]
        clv = 0.0 if rng <= 0 else ((b["c"] - b["l"]) - (b["h"] - b["c"])) / rng
        s += clv * float(b.get("v") or 1)
        cum.append(s)
    third = max(5, len(cum) // 3)
    scale = max(abs(v) for v in cum) or 1.0
    norm = (cum[-1] - cum[-third]) / scale
    p_slope = bars[-1]["c"] - bars[-third]["c"]
    bias = "BULLISH" if norm > 0.1 else "BEARISH" if norm < -0.1 else "NEUTRAL"
    divergence = None
    if p_slope > 0 and norm < -0.1:
        divergence = "BEARISH_DIVERGENCE"
    elif p_slope < 0 and norm > 0.1:
        divergence = "BULLISH_DIVERGENCE"
    return {"bias": bias, "slope": round(norm, 3), "divergence": divergence}


def analyze_dom(dom_doc):
    if not dom_doc:
        return None
    try:
        upd = datetime.fromisoformat(str(dom_doc.get("updated_at")))
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - upd).total_seconds()
    except (ValueError, TypeError):
        return None
    if age > DOM_FRESH_S:
        return {"live": False, "age_s": int(age)}
    bids = dom_doc.get("bids") or []
    asks = dom_doc.get("asks") or []
    bv = sum(float(r.get("v") or 0) for r in bids)
    av = sum(float(r.get("v") or 0) for r in asks)
    if bv + av <= 0:
        return {"live": False, "age_s": int(age)}
    wall_bid = max(bids, key=lambda r: float(r.get("v") or 0), default=None)
    wall_ask = max(asks, key=lambda r: float(r.get("v") or 0), default=None)
    return {"live": True, "age_s": int(age),
            "imbalance": round((bv - av) / (bv + av), 2),
            "bid_vol": round(bv, 2), "ask_vol": round(av, 2),
            "wall_bid": wall_bid, "wall_ask": wall_ask}


def build_liquidity_map(bars, price=None, dom_doc=None) -> dict:
    if not bars or len(bars) < 30:
        return {"ready": False, "reason": "insufficient intraday bars",
                "bars_n": len(bars or [])}
    price = float(price or bars[-1]["c"])
    atr = _atr(bars)
    obs = detect_order_blocks(bars, atr)
    clusters = detect_stop_clusters(bars, price, atr)
    fvgs = detect_fvg(bars)[-5:]
    active_zone = None
    for ob in obs:
        if ob["bottom"] <= price <= ob["top"]:
            active_zone = ob["dir"]
    pull_up = pull_dn = 0.0
    nearest_above = nearest_below = None
    for c in clusters:
        d_atr = abs(c["dist"]) / atr if atr > 0 else 1.0
        w = c["strength"] / max(0.25, d_atr)
        if c["side"] == "BUY_SIDE":
            pull_up += w
            if nearest_above is None or c["level"] < nearest_above["level"]:
                nearest_above = c
        else:
            pull_dn += w
            if nearest_below is None or c["level"] > nearest_below["level"]:
                nearest_below = c
    draw = None
    if pull_up > pull_dn * 1.3 and pull_up > 0:
        draw = "UP"
    elif pull_dn > pull_up * 1.3 and pull_dn > 0:
        draw = "DOWN"
    return {"ready": True, "price": round(price, 5), "atr": round(atr, 5),
            "order_blocks": obs, "stop_clusters": clusters, "fvgs": fvgs,
            "profile": volume_profile(bars), "cum_delta": cumulative_delta(bars),
            "dom": analyze_dom(dom_doc), "active_zone": active_zone,
            "draw": draw, "nearest_above": nearest_above,
            "nearest_below": nearest_below, "bars_n": len(bars)}


def liquidity_gate(action: str, lmap: dict, entry=None) -> str | None:
    """Veto trades fired directly into opposing resting liquidity."""
    if action not in ("BUY", "SELL") or not lmap or not lmap.get("ready"):
        return None
    zone = lmap.get("active_zone")
    obs = lmap.get("order_blocks") or []
    if action == "BUY" and zone == "SUPPLY":
        ob = next((o for o in reversed(obs) if o["dir"] == "SUPPLY"), {})
        return (f"Liquidity gate: price is inside an unmitigated SUPPLY order "
                f"block ({ob.get('bottom')}–{ob.get('top')}) — buying directly "
                f"into resting sell orders vetoed.")
    if action == "SELL" and zone == "DEMAND":
        ob = next((o for o in reversed(obs) if o["dir"] == "DEMAND"), {})
        return (f"Liquidity gate: price is inside an unmitigated DEMAND order "
                f"block ({ob.get('bottom')}–{ob.get('top')}) — selling directly "
                f"into resting buy orders vetoed.")
    dom = lmap.get("dom") or {}
    imb = dom.get("imbalance")
    if dom.get("live") and imb is not None:
        if action == "BUY" and imb <= -DOM_WALL_IMBALANCE:
            return (f"Liquidity gate: live order book is {abs(round(imb * 100))}% "
                    f"ask-heavy — BUY into a sell wall vetoed.")
        if action == "SELL" and imb >= DOM_WALL_IMBALANCE:
            return (f"Liquidity gate: live order book is {round(imb * 100)}% "
                    f"bid-heavy — SELL into a buy wall vetoed.")
    return None
