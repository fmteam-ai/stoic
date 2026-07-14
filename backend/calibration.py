"""iter-137 · Empirical probability calibration (quant roadmap item 1).

The engine's confidence is a heuristic setup score. This module maps stated
confidence → REALIZED win rate per engine from actual closed bot trades
(reliability table), producing `calibrated_p_win` plus a Brier score that
measures how honest the raw scores are. The statistically defensible first
step before any meta-label ML model.
"""
import time
from datetime import datetime, timedelta, timezone

BUCKETS = [(0, 55), (55, 65), (65, 75), (75, 101)]
MIN_BUCKET_N = 8          # below this, fall back to the engine/global rate
_cache: dict = {"table": None, "expires": 0.0}
CACHE_TTL = 15 * 60


def _bucket(conf: float) -> tuple:
    for lo, hi in BUCKETS:
        if lo <= conf < hi:
            return (lo, hi)
    return BUCKETS[-1]


async def compute_calibration(db, user_id: str, days: int = 90) -> dict:
    q = {"user_id": user_id, "status": "closed", "origin": "auto",
         "pnl": {"$ne": None}}
    if days > 0:
        q["closed_at"] = {"$gte": (datetime.now(timezone.utc)
                                   - timedelta(days=days)).isoformat()}
    trades = await db.trades.find(q).to_list(3000)

    # join confidence from the originating signal when absent on the trade
    sig_ids = list({t["signal_id"] for t in trades
                    if t.get("signal_id") and t.get("confidence") is None})
    conf_by_sig = {}
    if sig_ids:
        from bson import ObjectId
        oids = []
        for s in sig_ids:
            try:
                oids.append(ObjectId(s))
            except Exception:
                pass
        async for s in db.signals.find({"_id": {"$in": oids}},
                                       {"confidence": 1, "scope": 1}):
            conf_by_sig[str(s["_id"])] = s

    engines: dict = {}
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        if pnl == 0:
            continue
        sig = conf_by_sig.get(str(t.get("signal_id")), {})
        conf = t.get("confidence") or sig.get("confidence")
        if conf is None:
            continue
        scope = t.get("scope") or sig.get("scope") or "unattributed"
        e = engines.setdefault(scope, {"n": 0, "wins": 0, "brier_sum": 0.0,
                                       "buckets": {}})
        won = 1.0 if pnl > 0 else 0.0
        p = float(conf) / 100.0
        e["n"] += 1
        e["wins"] += int(won)
        e["brier_sum"] += (p - won) ** 2
        b = e["buckets"].setdefault(_bucket(float(conf)),
                                    {"n": 0, "wins": 0, "conf_sum": 0.0})
        b["n"] += 1
        b["wins"] += int(won)
        b["conf_sum"] += float(conf)

    out = {}
    for scope, e in engines.items():
        rows = []
        for (lo, hi), b in sorted(e["buckets"].items()):
            rows.append({"bucket": f"{lo}-{hi - 1}", "n": b["n"],
                         "stated": round(b["conf_sum"] / b["n"], 1),
                         "realized": round(b["wins"] / b["n"] * 100, 1),
                         "gap": round(b["wins"] / b["n"] * 100
                                      - b["conf_sum"] / b["n"], 1)})
        out[scope] = {
            "n": e["n"],
            "win_rate": round(e["wins"] / e["n"] * 100, 1),
            "brier": round(e["brier_sum"] / e["n"], 4),
            "buckets": rows,
        }
    return out


async def calibrated_p_win(db, user_id: str, scope: str,
                           confidence: float) -> dict | None:
    """Look up the realized win rate for this engine + confidence bucket.
    Cached 15 min. Returns None when there's no usable history."""
    now = time.time()
    if _cache["table"] is None or now > _cache["expires"]:
        _cache["table"] = await compute_calibration(db, user_id, days=90)
        _cache["expires"] = now + CACHE_TTL
    table = _cache["table"] or {}
    ent = table.get(scope)
    lo, hi = _bucket(float(confidence or 0))
    label = f"{lo}-{hi - 1}"
    if ent:
        for b in ent["buckets"]:
            if b["bucket"] == label and b["n"] >= MIN_BUCKET_N:
                return {"p_win": round(b["realized"] / 100, 3),
                        "basis": f"{scope} bucket {label} (n={b['n']})"}
        if ent["n"] >= MIN_BUCKET_N:
            return {"p_win": round(ent["win_rate"] / 100, 3),
                    "basis": f"{scope} overall (n={ent['n']})"}
    # global fallback
    ns = sum(e["n"] for e in table.values())
    if ns >= MIN_BUCKET_N:
        wr = sum(e["win_rate"] * e["n"] for e in table.values()) / ns
        return {"p_win": round(wr / 100, 3), "basis": f"global (n={ns})"}
    return None
