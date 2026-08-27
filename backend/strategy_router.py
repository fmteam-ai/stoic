"""Dynamic Strategy Router 2.0 — controlled mixture-of-experts. Learns
which strategy families earn in which regime fingerprint from realized R
with RECENCY weighting (21d half-life), sample-size shrinkage toward
uniform, strategy-HEALTH awareness (decay states can only reduce a
family's allocation) and hard bounds so the learner can ALLOCATE
attention but never override risk controls."""
import math
from datetime import datetime, timedelta, timezone

FAMILIES = ["ai", "scalp_fast", "swing"]
W_MIN, W_MAX = 0.05, 0.70
SHRINK_N = 25          # trades needed for full trust in a cell
LOOKBACK_DAYS = 90
RECENCY_HALF_LIFE_DAYS = 21
HEALTH_ROUTER_MULT = {"HEALTHY": 1.0, "WATCH": 1.0, "DEGRADED": 0.8,
                      "DECAYING": 0.6, "DISABLED": 0.5}


def _family_of(scope: str | None) -> str:
    s = str(scope or "").lower()
    if "scalp" in s:
        return "scalp_fast"
    if "swing" in s:
        return "swing"
    return "ai"


def recency_weight(closed_at,
                   half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    try:
        dt = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc)
                        - dt).total_seconds() / 86400)
    except (TypeError, ValueError):
        age = half_life_days * 2.0
    return 0.5 ** (age / half_life_days)


async def _cell_stats(db, user_id: str, fingerprint_key: str) -> dict:
    """Per-family RECENCY-WEIGHTED realized avg R for trades whose signal
    carried this fingerprint (falls back to all recent trades when none
    tagged yet)."""
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).isoformat()
    sig_ids = [str(s["_id"]) async for s in db.signals.find(
        {"user_id": user_id,
         "market_state.fingerprint_key": fingerprint_key,
         "created_at": {"$gte": since}}, {"_id": 1}).limit(2000)]
    q = {"user_id": user_id, "status": "closed",
         "closed_at": {"$gte": since}}
    tagged = bool(sig_ids)
    if tagged:
        q["signal_id"] = {"$in": sig_ids}
    from outcome_attribution import result_r
    stats = {f: {"n": 0, "sum_r": 0.0, "sum_w": 0.0, "sum_wr": 0.0}
             for f in FAMILIES}
    async for t in db.trades.find(
            q, {"scope": 1, "pnl": 1, "entry_price": 1, "stop_loss": 1,
                "exit_price": 1, "action": 1, "alpha_clean": 1,
                "closed_at": 1}).limit(3000):
        if t.get("alpha_clean") is False:   # noise never trains the router
            continue
        fam = _family_of(t.get("scope"))
        r, _src = result_r(t)
        w = recency_weight(t.get("closed_at"))
        stats[fam]["n"] += 1
        stats[fam]["sum_r"] += r
        stats[fam]["sum_w"] += w
        stats[fam]["sum_wr"] += w * r
    return {"stats": stats, "fingerprint_matched": tagged}


async def _family_health(db, user_id: str) -> dict:
    """Worst persisted decay state per family (router downscale-only)."""
    out = {f: "HEALTHY" for f in FAMILIES}
    order = {"HEALTHY": 0, "WATCH": 1, "DEGRADED": 2, "DECAYING": 3,
             "DISABLED": 4}
    try:
        async for h in db.strategy_health.find(
                {"user_id": user_id}, {"scope": 1, "state": 1,
                                       "unproven": 1}):
            if h.get("unproven"):
                continue
            fam = _family_of(h.get("scope"))
            if order.get(str(h.get("state")), 0) > order[out[fam]]:
                out[fam] = str(h["state"])
    except Exception:  # noqa: BLE001
        pass
    return out


async def route(db, user_id: str, fingerprint_key: str) -> dict:
    cell = await _cell_stats(db, user_id, fingerprint_key)
    stats = cell["stats"]
    health = await _family_health(db, user_id)
    raw = {}
    for fam in FAMILIES:
        n = stats[fam]["n"]
        sum_w = stats[fam]["sum_w"]
        avg_r = (stats[fam]["sum_wr"] / sum_w) if sum_w > 0 else 0.0
        trust = min(1.0, n / SHRINK_N)           # shrink toward neutral
        score = math.exp(2.0 * avg_r * trust)    # softmax-ish, bounded
        score *= HEALTH_ROUTER_MULT.get(health[fam], 1.0)
        raw[fam] = {"score": score, "n": n, "avg_r": round(avg_r, 3),
                    "trust": round(trust, 2), "health": health[fam]}
    total = sum(v["score"] for v in raw.values()) or 1.0
    weights = {f: max(W_MIN, v["score"] / total) for f, v in raw.items()}
    # hard bounds — the router allocates, it never silences or dominates;
    # cap-and-redistribute keeps every weight inside [W_MIN, W_MAX]
    for _ in range(4):
        s = sum(weights.values())
        weights = {f: w / s for f, w in weights.items()}
        over = {f for f, w in weights.items() if w > W_MAX + 1e-9}
        if not over:
            break
        excess = sum(weights[f] - W_MAX for f in over)
        under = [f for f in weights if f not in over]
        for f in over:
            weights[f] = W_MAX
        for f in under:
            weights[f] += excess / max(1, len(under))
    weights = {f: round(w, 3) for f, w in weights.items()}
    return {"fingerprint_key": fingerprint_key, "weights": weights,
            "evidence": {f: {k: v[k] for k in
                             ("n", "avg_r", "trust", "health")}
                         for f, v in raw.items()},
            "fingerprint_matched": cell["fingerprint_matched"],
            "bounds": [W_MIN, W_MAX], "engine_version": 2,
            "note": "weights allocate attention only — hard risk "
                    "controls are never overridden"}
