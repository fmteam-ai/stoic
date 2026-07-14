"""iter-139 · RL capital allocator (institutional Phase B).

Decides the capital WEIGHT of each strategy engine from its recent realized
performance — trade logic stays fully deterministic; only sizing moves.

Bandit posterior per scope: with n closed trades of mean P&L m and standard
error s, P(edge > 0) = Φ(m/s). Weights are SHRINK-ONLY and conservative:

  n < MIN_TRADES        → 1.0  (neutral — never punish a young strategy)
  P(edge>0) ≥ 0.5       → 1.0  (full budget while evidence is non-negative)
  P(edge>0) < 0.5       → MIN_WEIGHT + (1−MIN_WEIGHT) × 2·P   (0.25 floor)

So an engine that is *proven* to lose money gets a 0.25× capital weight;
an ambiguous one is barely touched; nothing is ever inflated above 1.0.
Modes (bot config `rl_allocator_mode`): off / advisory (default) / enforce.
A failure in this layer means full weight — the fail-closed risk engine
downstream remains the final authority on every trade.
"""
import logging
import math
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("rl-allocator")

LOOKBACK_DAYS = 60
MIN_TRADES = 10
MIN_WEIGHT = 0.25
CACHE_TTL = 15 * 60
_cache: dict = {}   # user_id → (expires_at, allocations)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def build_allocations(pnls_by_scope: dict) -> dict:
    """Pure: {scope: [pnl, ...]} → {scope: allocation dict}."""
    out = {}
    for scope, pnls in pnls_by_scope.items():
        n = len(pnls)
        mean = sum(pnls) / n if n else 0.0
        if n > 1:
            var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            sem = math.sqrt(var / n)
        else:
            sem = 0.0
        if sem > 1e-9:
            p_pos = _phi(mean / sem)
        else:
            p_pos = 1.0 if mean >= 0 else 0.0
        if n < MIN_TRADES:
            weight, basis = 1.0, f"insufficient data ({n}/{MIN_TRADES} trades) — neutral"
        elif p_pos >= 0.5:
            weight = 1.0
            basis = (f"P(edge>0)={p_pos:.2f} over {n} trades "
                     f"(mean ${mean:+.2f}) — full budget")
        else:
            weight = max(MIN_WEIGHT,
                         round(MIN_WEIGHT + (1 - MIN_WEIGHT) * 2 * p_pos, 3))
            basis = (f"P(edge>0)={p_pos:.2f} over {n} trades "
                     f"(mean ${mean:+.2f}) — capital shrunk to {weight}×")
        out[scope] = {"scope": scope, "n": n, "mean_pnl": round(mean, 2),
                      "sem": round(sem, 2), "p_positive": round(p_pos, 3),
                      "weight": weight, "reason": basis}
    return out


async def compute_allocations(db, user_id: str) -> dict:
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "origin": "auto",
        "pnl": {"$ne": None}, "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }, {"pnl": 1, "scope": 1}).to_list(5000)
    by_scope: dict = {}
    for t in trades:
        scope = t.get("scope") or "unattributed"
        by_scope.setdefault(scope, []).append(float(t.get("pnl") or 0))
    return build_allocations(by_scope)


async def get_allocations(db, user_id: str) -> dict:
    now = time.time()
    cached = _cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    allocs = await compute_allocations(db, user_id)
    _cache[user_id] = (now + CACHE_TTL, allocs)
    return allocs


async def allocator_weight_for(db, user_id: str, scope: str | None,
                               cfg: dict) -> dict:
    mode = str(cfg.get("rl_allocator_mode") or "advisory").lower()
    neutral = {"scope": scope, "weight": 1.0, "mode": mode,
               "reason": "allocator neutral"}
    if mode == "off" or not scope:
        return neutral
    allocs = await get_allocations(db, user_id)
    ent = allocs.get(scope)
    if not ent:
        return {**neutral, "reason": f"no closed history for scope '{scope}' — neutral"}
    return {**ent, "mode": mode}


def invalidate_cache(user_id: str | None = None) -> None:
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)
