"""Market Memory Engine (v59 #2) — episodic memory of previous market
situations. For each new opportunity: regime fingerprint → similarity
search over the user's own closed-trade history (via the market-state
vector each signal carried) → weighted outcome distribution. Evidence is
weighted by similarity × recency × session relevance × symbol match.
The verdict is downscale-only: memory can REDUCE or AVOID, never boost."""
import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("market.memory")

LOOKBACK_DAYS = 240
HALF_LIFE_DAYS = 30
MIN_SIMILARITY = 0.55
MIN_MATCHES = 8
CONT_R, REV_R = 0.15, -0.15


def _similarity(a: dict, b: dict) -> float:
    """Cosine over shared vector dims, clamped to [0, 1]."""
    keys = [k for k in a if k in b]
    if len(keys) < 5:
        return 0.0
    dot = sum(float(a[k]) * float(b[k]) for k in keys)
    na = math.sqrt(sum(float(a[k]) ** 2 for k in keys))
    nb = math.sqrt(sum(float(b[k]) ** 2 for k in keys))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return max(0.0, dot / (na * nb))


def _age_days(closed_at) -> float:
    try:
        dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds()
                   / 86400)
    except (TypeError, ValueError):
        return HALF_LIFE_DAYS * 2.0


async def recall(db, user_id: str, symbol: str, vector: dict | None,
                 session: str | None = None,
                 scope: str | None = None) -> dict:
    if not vector:
        return {"available": False, "note": "no market-state vector"}
    from outcome_attribution import result_r
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = [t async for t in db.trades.find(
        {"user_id": user_id, "status": "closed",
         "closed_at": {"$gte": since}, "alpha_clean": {"$ne": False},
         "signal_id": {"$ne": None}},
        {"signal_id": 1, "symbol": 1, "scope": 1, "closed_at": 1,
         "pnl": 1, "entry_price": 1, "stop_loss": 1, "exit_price": 1,
         "action": 1}).sort("closed_at", -1).limit(600)]
    sig_map = {}
    try:
        from bson import ObjectId
        ids = []
        for t in trades:
            try:
                ids.append(ObjectId(str(t["signal_id"])))
            except Exception:  # noqa: BLE001
                continue
        if ids:
            async for s in db.signals.find(
                    {"_id": {"$in": ids}}, {"market_state": 1}):
                sig_map[str(s["_id"])] = s
    except Exception as e:  # noqa: BLE001
        logger.debug("memory signal fetch failed: %s", e)
    base = str(symbol or "").upper()[:6]
    matches = []
    for t in trades:
        ms = (sig_map.get(str(t.get("signal_id"))) or {}).get(
            "market_state") or {}
        v = ms.get("vector")
        if not v:
            continue
        sim = _similarity(vector, v)
        if sim < MIN_SIMILARITY:
            continue
        w = sim * sim * (0.5 ** (_age_days(t.get("closed_at"))
                                 / HALF_LIFE_DAYS))
        if session and ms.get("session") != session:
            w *= 0.75
        if not str(t.get("symbol") or "").upper().startswith(base):
            w *= 0.6
        if scope and str(t.get("scope") or "") != str(scope):
            w *= 0.85
        if w <= 0:
            continue
        r, _src = result_r(t)
        matches.append({"r": r, "w": w, "sim": round(sim, 3),
                        "symbol": t.get("symbol"),
                        "scope": t.get("scope"),
                        "closed_at": t.get("closed_at")})
    if len(matches) < MIN_MATCHES:
        return {"available": False, "n": len(matches),
                "note": f"insufficient comparable situations "
                        f"({len(matches)}/{MIN_MATCHES})"}
    W = sum(m["w"] for m in matches)
    w2 = sum(m["w"] ** 2 for m in matches)
    n_eff = (W * W / w2) if w2 > 0 else 0.0
    cont = sum(m["w"] for m in matches if m["r"] > CONT_R) / W
    rev = sum(m["w"] for m in matches if m["r"] < REV_R) / W
    neutral = max(0.0, 1.0 - cont - rev)
    mean_r = sum(m["r"] * m["w"] for m in matches) / W
    acc, median_r = 0.0, 0.0
    for m in sorted(matches, key=lambda x: x["r"]):
        acc += m["w"]
        if acc >= W / 2:
            median_r = m["r"]
            break
    agreement = max(cont, rev, neutral)
    confidence = round(min(1.0, n_eff / 40.0) * agreement, 3)
    top = sorted(matches, key=lambda x: -x["w"])[:5]
    return {"available": True, "n": len(matches),
            "n_eff": round(n_eff, 1),
            "distribution": {"continuation": round(cont, 3),
                             "reversal": round(rev, 3),
                             "neutral": round(neutral, 3)},
            "mean_r": round(mean_r, 3), "median_r": round(median_r, 3),
            "confidence": confidence,
            "top_matches": [{k: m[k] for k in
                             ("sim", "r", "symbol", "closed_at")}
                            for m in top]}


def verdict(mem: dict) -> dict:
    """Downscale-only: OK / REDUCE / AVOID."""
    if not mem.get("available"):
        return {"action": "OK", "multiplier": 1.0,
                "reason": mem.get("note") or "no memory evidence"}
    c = float(mem.get("confidence") or 0)
    mr = float(mem.get("median_r") or 0)
    if c >= 0.7 and mr <= -0.25:
        return {"action": "AVOID", "multiplier": 0.0,
                "reason": f"{mem['n']} similar situations, median "
                          f"{mr}R at {round(c * 100)}% confidence"}
    if c >= 0.5 and mr < 0:
        return {"action": "REDUCE", "multiplier": 0.6,
                "reason": f"similar situations skew negative "
                          f"(median {mr}R, confidence {round(c * 100)}%)"}
    return {"action": "OK", "multiplier": 1.0,
            "reason": f"memory neutral-or-supportive (median {mr}R)"}
