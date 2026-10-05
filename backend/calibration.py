"""iter-137/144 · Empirical probability calibration.

Maps stated setup score → REALIZED win rate per engine from actual closed
bot trades (reliability table) + Brier score honesty check.

Quant review round 4 hardening:
  C1 — cache keyed PER USER (was global: first user's table leaked to all).
  C3 — MIN_BUCKET_N raised 8→30 and every estimate carries a Wilson 95%
       one-sided LOWER bound; consumers must gate on `p_win_lb`, never the
       point estimate.
  H6 — buckets carry the payoff distribution (avg win/loss R) and a
       conservative expected value `ev_r = p_lb·E[win_R] − (1−p_lb)·E[loss_R]`
       so callers can gate on EV, not win rate alone.
"""
import math
import time
from datetime import datetime, timedelta, timezone

BUCKETS = [(0, 55), (55, 65), (65, 75), (75, 101)]
MIN_BUCKET_N = 30         # C3: 8 was statistically meaningless
MIN_SCOPE_N = 50
MIN_GLOBAL_N = 80
CACHE_TTL = 15 * 60
_cache: dict = {}         # C1: user_id → {"table": ..., "expires": ...}


def invalidate_cache(user_id: str | None = None) -> None:
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


def wilson_lb(wins: int, n: int, z: float = 1.645) -> float:
    """One-sided 95% Wilson score lower bound for a binomial proportion."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _bucket(conf: float) -> tuple:
    for lo, hi in BUCKETS:
        if lo <= conf < hi:
            return (lo, hi)
    return BUCKETS[-1]


async def compute_calibration(db, user_id: str, days: int = 90) -> dict:
    q = {"user_id": user_id, "status": "closed", "origin": "auto", "stats_excluded": {"$ne": True},
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
                                       {"confidence": 1, "scope": 1, "setup_score": 1}):
            conf_by_sig[str(s["_id"])] = s

    engines: dict = {}
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        if pnl == 0:
            continue
        sig = conf_by_sig.get(str(t.get("signal_id")), {})
        conf = calibration_input(t, sig)
        if conf is None:
            continue
        scope = t.get("scope") or sig.get("scope") or "unattributed"
        e = engines.setdefault(scope, {"n": 0, "wins": 0, "brier_sum": 0.0,
                                       "buckets": {}})
        won = 1.0 if pnl > 0 else 0.0
        p = float(conf) / 100.0
        # R multiple when the trade recorded its risked amount (H6 payoff)
        risk = float(t.get("risk_amount") or 0)
        r_mult = (pnl / risk) if risk > 0 else None
        e["n"] += 1
        e["wins"] += int(won)
        e["brier_sum"] += (p - won) ** 2
        b = e["buckets"].setdefault(_bucket(float(conf)),
                                    {"n": 0, "wins": 0, "conf_sum": 0.0,
                                     "win_r_sum": 0.0, "win_r_n": 0,
                                     "loss_r_sum": 0.0, "loss_r_n": 0})
        b["n"] += 1
        b["wins"] += int(won)
        b["conf_sum"] += float(conf)
        if r_mult is not None:
            if pnl > 0:
                b["win_r_sum"] += r_mult
                b["win_r_n"] += 1
            else:
                b["loss_r_sum"] += abs(r_mult)
                b["loss_r_n"] += 1

    out = {}
    for scope, e in engines.items():
        rows = []
        for (lo, hi), b in sorted(e["buckets"].items()):
            p_lb = wilson_lb(b["wins"], b["n"])
            avg_win_r = (b["win_r_sum"] / b["win_r_n"]) if b["win_r_n"] else None
            avg_loss_r = (b["loss_r_sum"] / b["loss_r_n"]) if b["loss_r_n"] else None
            # H6: conservative EV in R — lower-bound win prob, realized payoffs
            ev_r = (round(p_lb * avg_win_r - (1 - p_lb) * avg_loss_r, 3)
                    if (avg_win_r is not None and avg_loss_r is not None) else None)
            rows.append({"bucket": f"{lo}-{hi - 1}", "n": b["n"],
                         "stated": round(b["conf_sum"] / b["n"], 1),
                         "realized": round(b["wins"] / b["n"] * 100, 1),
                         "realized_lb": round(p_lb * 100, 1),
                         "avg_win_r": round(avg_win_r, 2) if avg_win_r is not None else None,
                         "avg_loss_r": round(avg_loss_r, 2) if avg_loss_r is not None else None,
                         "ev_r": ev_r,
                         "gap": round(b["wins"] / b["n"] * 100
                                      - b["conf_sum"] / b["n"], 1)})
        out[scope] = {
            "n": e["n"],
            "win_rate": round(e["wins"] / e["n"] * 100, 1),
            "win_rate_lb": round(wilson_lb(e["wins"], e["n"]) * 100, 1),
            "wins": e["wins"],
            "brier": round(e["brier_sum"] / e["n"], 4),
            "buckets": rows,
        }
    return out


async def _table_for(db, user_id: str) -> dict:
    now = time.time()
    ent = _cache.get(user_id)
    if ent and ent["expires"] > now:
        return ent["table"]
    table = await compute_calibration(db, user_id, days=90)
    _cache[user_id] = {"table": table, "expires": now + CACHE_TTL}
    return table


def calibration_input(trade: dict | None = None, sig: dict | None = None) -> float | None:
    """Fix plan A9 — the number calibration buckets on, identical for history and
    the live lookup: the RAW setup score (never the news-/session-adjusted
    confidence). Falls back to confidence only for legacy rows without a score."""
    trade, sig = trade or {}, sig or {}
    raw = trade.get("setup_score_raw")
    if raw is None and isinstance(sig.get("setup_score"), dict):
        raw = sig["setup_score"].get("score")
    if raw is None:
        raw = trade.get("confidence") if trade.get("confidence") is not None else sig.get("confidence")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def calibrated_p_win(db, user_id: str, scope: str,
                           confidence: float) -> dict | None:
    """Realized win rate for this engine + score bucket, with the Wilson
    lower bound consumers MUST gate on. None when history is too thin —
    absence of calibration means Kelly-style sizing must stay off."""
    table = await _table_for(db, user_id) or {}
    ent = table.get(scope)
    lo, hi = _bucket(float(confidence or 0))
    label = f"{lo}-{hi - 1}"
    if ent:
        for b in ent["buckets"]:
            if b["bucket"] == label and b["n"] >= MIN_BUCKET_N:
                return {"p_win": round(b["realized"] / 100, 3),
                        "p_win_lb": round(b["realized_lb"] / 100, 3),
                        "ev_r": b.get("ev_r"), "n": b["n"],
                        "basis": f"{scope} bucket {label} (n={b['n']})"}
        if ent["n"] >= MIN_SCOPE_N:
            return {"p_win": round(ent["win_rate"] / 100, 3),
                    "p_win_lb": round(ent["win_rate_lb"] / 100, 3),
                    "ev_r": None, "n": ent["n"],
                    "basis": f"{scope} overall (n={ent['n']})"}
    ns = sum(e["n"] for e in table.values())
    if ns >= MIN_GLOBAL_N:
        wins = sum(e.get("wins", 0) for e in table.values())
        return {"p_win": round(wins / ns, 3),
                "p_win_lb": round(wilson_lb(wins, ns), 3),
                "ev_r": None, "n": ns, "basis": f"global (n={ns})"}
    return None
