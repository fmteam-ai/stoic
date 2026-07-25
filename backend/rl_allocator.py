"""iter-139 / iter-103 · RL capital allocator (institutional Phase B).

Decides the capital WEIGHT of each strategy engine from its recent realized
performance — trade logic stays fully deterministic; only sizing moves.

Evidence policy (safety review, iter-103):
  n < MIN_TRADES (30)             → 1.0 neutral — NO allocation authority
  30 ≤ n < FULL_AUTHORITY (100)   → LIMITED authority — floor 0.5×
  n ≥ FULL_AUTHORITY (100)        → full authority — floor 0.25×

Bandit posterior per scope: with n closed trades of mean P&L m and standard
error s, P(edge > 0) = Φ(m/s), then BAYESIAN-SHRUNK toward 0.5 with PRIOR_N
pseudo-trades so small samples can never drive large reallocation:
  p̂ = (n·p + PRIOR_N·0.5) / (n + PRIOR_N)

Weights are SHRINK-ONLY (never above 1.0). Additional controls:
  • gradual changes — a weight moves at most MAX_STEP per refresh vs the
    previously persisted weight (db.allocator_state)
  • tail-correlation — scopes whose daily P&L correlates ≥ TAIL_CORR get
    the lower-evidence member trimmed ×TAIL_TRIM (correlated books share
    tail risk; diversification credit must not be double-counted)
  • 95% confidence interval on mean P&L reported per scope
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
MIN_TRADES = 30
FULL_AUTHORITY_TRADES = 100
LIMITED_FLOOR = 0.5
MIN_WEIGHT = 0.25
PRIOR_N = 30
MAX_STEP = 0.10
TAIL_CORR = 0.7
TAIL_TRIM = 0.85
MIN_OVERLAP_DAYS = 10
CACHE_TTL = 15 * 60
_cache: dict = {}   # user_id → (expires_at, allocations)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def _pearson(xs: list, ys: list) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def build_allocations(pnls_by_scope: dict, daily_by_scope: dict | None = None,
                      prev_weights: dict | None = None) -> dict:
    """Pure: {scope: [pnl, ...]} → {scope: allocation dict}.
    daily_by_scope: {scope: {"YYYY-MM-DD": pnl}} for tail-correlation.
    prev_weights: {scope: weight} for the gradual-change cap."""
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
        p_shrunk = (n * p_pos + PRIOR_N * 0.5) / (n + PRIOR_N)
        ci95 = (round(mean - 1.96 * sem, 2), round(mean + 1.96 * sem, 2))
        if n < MIN_TRADES:
            weight = 1.0
            authority = "none"
            basis = (f"insufficient evidence ({n}/{MIN_TRADES} trades) — "
                     f"neutral, no allocation authority")
        else:
            limited = n < FULL_AUTHORITY_TRADES
            floor = LIMITED_FLOOR if limited else MIN_WEIGHT
            authority = "limited" if limited else "full"
            if p_shrunk >= 0.5:
                weight = 1.0
                basis = (f"shrunk P(edge>0)={p_shrunk:.2f} over {n} trades "
                         f"(mean ${mean:+.2f}, CI95 {ci95}) — full budget")
            else:
                weight = max(floor,
                             round(floor + (1 - floor) * 2 * p_shrunk, 3))
                basis = (f"shrunk P(edge>0)={p_shrunk:.2f} over {n} trades "
                         f"(mean ${mean:+.2f}, CI95 {ci95}) — "
                         f"{authority} authority shrink to {weight}×")
        out[scope] = {"scope": scope, "n": n, "mean_pnl": round(mean, 2),
                      "sem": round(sem, 2), "p_positive": round(p_pos, 3),
                      "p_shrunk": round(p_shrunk, 3), "ci95": list(ci95),
                      "authority": authority, "weight": weight,
                      "reason": basis}

    # tail-correlation control — correlated books share tail risk
    if daily_by_scope:
        scopes = [s for s in out
                  if out[s]["n"] >= MIN_TRADES and s in daily_by_scope]
        for i, a in enumerate(scopes):
            for b in scopes[i + 1:]:
                common = sorted(set(daily_by_scope[a])
                                & set(daily_by_scope[b]))
                if len(common) < MIN_OVERLAP_DAYS:
                    continue
                corr = _pearson([daily_by_scope[a][d] for d in common],
                                [daily_by_scope[b][d] for d in common])
                if corr is not None and corr >= TAIL_CORR:
                    victim = a if out[a]["n"] <= out[b]["n"] else b
                    floor = (LIMITED_FLOOR
                             if out[victim]["n"] < FULL_AUTHORITY_TRADES
                             else MIN_WEIGHT)
                    trimmed = max(floor,
                                  round(out[victim]["weight"] * TAIL_TRIM, 3))
                    if trimmed < out[victim]["weight"]:
                        out[victim]["weight"] = trimmed
                        out[victim]["reason"] += (
                            f" · tail-correlation trim ×{TAIL_TRIM} "
                            f"(daily P&L corr {corr:.2f} with {a if victim == b else b})")

    # gradual-change cap — never jump more than MAX_STEP per refresh
    if prev_weights:
        for scope, ent in out.items():
            prev = prev_weights.get(scope)
            if prev is None:
                continue
            lo, hi = prev - MAX_STEP, prev + MAX_STEP
            if ent["weight"] < lo or ent["weight"] > hi:
                stepped = round(min(hi, max(lo, ent["weight"])), 3)
                ent["reason"] += (f" · gradual cap {prev}→{stepped} "
                                  f"(target {ent['weight']}, max step "
                                  f"{MAX_STEP}/refresh)")
                ent["weight"] = stepped
    return out


async def compute_allocations(db, user_id: str) -> dict:
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "origin": "auto",
        "pnl": {"$ne": None}, "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }, {"pnl": 1, "scope": 1, "closed_at": 1}).to_list(5000)
    by_scope: dict = {}
    daily: dict = {}
    for t in trades:
        scope = t.get("scope") or "unattributed"
        pnl = float(t.get("pnl") or 0)
        by_scope.setdefault(scope, []).append(pnl)
        day = str(t.get("closed_at") or "")[:10]
        if day:
            d = daily.setdefault(scope, {})
            d[day] = d.get(day, 0.0) + pnl
    state = await db.allocator_state.find_one({"_id": user_id}) or {}
    allocs = build_allocations(by_scope, daily_by_scope=daily,
                               prev_weights=state.get("weights") or {})
    await db.allocator_state.update_one(
        {"_id": user_id},
        {"$set": {"weights": {s: a["weight"] for s, a in allocs.items()},
                  "at": datetime.now(timezone.utc)}},
        upsert=True)
    return allocs


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
