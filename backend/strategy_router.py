"""Dynamic Strategy Router — controlled mixture-of-experts. Learns which
strategy families earn in which regime fingerprint from realized R, with
sample-size shrinkage toward uniform and hard bounds so the learner can
ALLOCATE attention but never override risk controls."""
import math
from datetime import datetime, timedelta, timezone

FAMILIES = ["ai", "scalp_fast", "swing"]
W_MIN, W_MAX = 0.05, 0.70
SHRINK_N = 25          # trades needed for full trust in a cell
LOOKBACK_DAYS = 90


def _family_of(scope: str | None) -> str:
    s = str(scope or "").lower()
    if "scalp" in s:
        return "scalp_fast"
    if "swing" in s:
        return "swing"
    return "ai"


async def _cell_stats(db, user_id: str, fingerprint_key: str) -> dict:
    """Per-family realized avg R for trades whose signal carried this
    fingerprint (falls back to all recent trades when none tagged yet)."""
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
    stats = {f: {"n": 0, "sum_r": 0.0} for f in FAMILIES}
    async for t in db.trades.find(
            q, {"scope": 1, "pnl": 1, "entry_price": 1, "stop_loss": 1,
                "exit_price": 1, "action": 1, "alpha_clean": 1}).limit(3000):
        if t.get("alpha_clean") is False:   # noise never trains the router
            continue
        fam = _family_of(t.get("scope"))
        r, _src = result_r(t)
        stats[fam]["n"] += 1
        stats[fam]["sum_r"] += r
    return {"stats": stats, "fingerprint_matched": tagged}


async def route(db, user_id: str, fingerprint_key: str) -> dict:
    cell = await _cell_stats(db, user_id, fingerprint_key)
    stats = cell["stats"]
    raw = {}
    for fam in FAMILIES:
        n = stats[fam]["n"]
        avg_r = stats[fam]["sum_r"] / n if n else 0.0
        trust = min(1.0, n / SHRINK_N)           # shrink toward neutral
        score = math.exp(2.0 * avg_r * trust)    # softmax-ish, bounded below
        raw[fam] = {"score": score, "n": n, "avg_r": round(avg_r, 3),
                    "trust": round(trust, 2)}
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
            "evidence": {f: {k: v[k] for k in ("n", "avg_r", "trust")}
                         for f, v in raw.items()},
            "fingerprint_matched": cell["fingerprint_matched"],
            "bounds": [W_MIN, W_MAX],
            "note": "weights allocate attention only — hard risk "
                    "controls are never overridden"}
