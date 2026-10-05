"""Champion/Challenger 2.0 (v59 #5) — institutional qualification
scorecard in FRONT of the canary ladder: purged walk-forward, embargo,
CPCV, Monte Carlo robustness, plus expectancy/sortino/PF/ES/max-DD,
regime stability and transaction-cost & slippage sensitivity. A
challenger must show robust risk-adjusted behavior, not just profit."""
import asyncio
import logging
import random
import time
from collections import deque
from datetime import datetime, timezone
from itertools import combinations

logger = logging.getLogger("champion.challenger2")

WF_FOLDS = 5
CPCV_GROUPS = 6
EMBARGO_TRADES = 3
MC_ITERS = 500
MIN_TRADES = 30
MAX_DD_R = 15.0
ES5_FLOOR_R = -12.0
OOS_LOSS_RATE_MAX = 0.35
MC_P_PROFIT_MIN = 0.75
COST_SHOCK_R = 0.05
SLIPPAGE_SHOCK_R = 0.10
# SEC-001 — qualification replays are CPU-heavy: per-user sliding window
# + fresh-scorecard reuse keep the endpoint from becoming a DoS vector.
QUALIFY_MAX_PER_WINDOW = 3
QUALIFY_WINDOW_S = 600
SCORECARD_FRESH_S = 600
_qualify_calls: dict = {}


class QualifyRateLimited(Exception):
    def __init__(self, retry_in_s: float):
        self.retry_in_s = retry_in_s
        super().__init__(f"qualification rate limit — retry in "
                         f"{retry_in_s:.0f}s")


def _rate_check(user_id: str) -> None:
    now = time.time()
    dq = _qualify_calls.setdefault(user_id, deque())
    while dq and now - dq[0] > QUALIFY_WINDOW_S:
        dq.popleft()
    if len(dq) >= QUALIFY_MAX_PER_WINDOW:
        raise QualifyRateLimited(QUALIFY_WINDOW_S - (now - dq[0]))
    dq.append(now)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def metrics_of(rs: list) -> dict:
    n = len(rs)
    exp = sum(rs) / n if n else 0.0
    gw = sum(r for r in rs if r > 0)
    gl = sum(-r for r in rs if r < 0)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    downside = [r for r in rs if r < 0]
    dvar = (sum(r * r for r in downside) / n) if n else 0.0
    sortino = exp / (dvar ** 0.5) if dvar > 0 else (999.0 if exp > 0
                                                    else 0.0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "expectancy": round(exp, 3),
            "profit_factor": round(min(pf, 999), 2),
            "sortino": round(min(sortino, 999), 2),
            "max_dd_r": round(dd, 2), "total_r": round(sum(rs), 2)}


def horizon_embargo(log: list | None) -> dict:
    """Horizon-aware purge width: embargo enough TRADES to span one full
    holding period at the observed trade cadence, so no evaluation window
    leaks across a fold boundary. Falls back to the fixed default when the
    replay log carries no timing."""
    if not log or len(log) < 5:
        return {"trades": EMBARGO_TRADES, "basis": "fixed"}
    try:
        holds = sorted(max(1, int(x["t"]) - int(x.get("opened_t") or x["t"]))
                       for x in log)
        closes = [int(x["t"]) for x in log]
        gaps = sorted(max(1, b - a) for a, b in zip(closes, closes[1:]))
        if not gaps:
            return {"trades": EMBARGO_TRADES, "basis": "fixed"}
        hold_med = holds[len(holds) // 2]
        gap_med = gaps[len(gaps) // 2]
        n = max(1, min(10, -(-hold_med // gap_med)))   # ceil division
        return {"trades": n, "basis": "horizon",
                "median_holding_bars": hold_med,
                "median_gap_bars": gap_med}
    except (KeyError, TypeError, ValueError):
        return {"trades": EMBARGO_TRADES, "basis": "fixed"}


def purged_walk_forward(rs: list, folds: int = WF_FOLDS,
                        embargo: int | None = None,
                        log: list | None = None) -> dict:
    emb = ({"trades": embargo, "basis": "explicit"} if embargo is not None
           else horizon_embargo(log))
    embargo = int(emb["trades"])
    n = len(rs)
    size = n // folds
    fold_exps = []
    for i in range(folds):
        lo = i * size + (embargo if i else 0)
        hi = (i + 1) * size - (embargo if i < folds - 1 else 0) \
            if i < folds - 1 else n
        chunk = rs[lo:hi]
        if chunk:
            fold_exps.append(round(sum(chunk) / len(chunk), 3))
    pos = sum(1 for e in fold_exps if e > 0)
    return {"folds": fold_exps, "positive_folds": pos,
            "embargo_trades": embargo, "embargo_basis": emb["basis"],
            "passed": bool(fold_exps) and pos / len(fold_exps) >= 0.6}


def cpcv(rs: list, groups: int = CPCV_GROUPS,
         embargo: int | None = None, log: list | None = None) -> dict:
    """Combinatorial purged CV — every 2-group combination is an
    out-of-sample test set (boundary trades embargoed, horizon-aware when
    the replay log is provided). `oos_loss_rate` = fraction of test
    combinations with non-positive expectancy. NOTE: this is an OOS
    consistency proxy, NOT the formal Bailey et al. PBO statistic (which
    would require ranking many configurations in-sample vs out-of-sample)."""
    emb = ({"trades": embargo, "basis": "explicit"} if embargo is not None
           else horizon_embargo(log))
    embargo = int(emb["trades"])
    n = len(rs)
    size = n // groups
    slices = []
    for g in range(groups):
        lo = g * size + (embargo if g else 0)
        hi = (g + 1) * size - (embargo if g < groups - 1 else 0) \
            if g < groups - 1 else n
        slices.append(rs[lo:hi])
    exps = []
    for a, b in combinations(range(groups), 2):
        test = slices[a] + slices[b]
        if test:
            exps.append(sum(test) / len(test))
    neg = sum(1 for e in exps if e <= 0)
    rate = round(neg / len(exps), 3) if exps else 1.0
    return {"combinations": len(exps), "oos_loss_rate": rate,
            "embargo_trades": embargo, "embargo_basis": emb["basis"],
            "median_oos_expectancy": round(
                sorted(exps)[len(exps) // 2], 3) if exps else None,
            "passed": rate <= OOS_LOSS_RATE_MAX}


def monte_carlo(rs: list, iters: int = MC_ITERS, seed: int = 42) -> dict:
    rng = random.Random(seed)
    n = len(rs)
    totals, dds = [], []
    for _ in range(iters):
        sample = [rng.choice(rs) for _ in range(n)]
        totals.append(sum(sample))
        eq = peak = dd = 0.0
        for r in sample:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        dds.append(dd)
    totals.sort()
    dds.sort()
    k = max(1, int(0.05 * iters))
    es5 = sum(totals[:k]) / k
    return {"iters": iters,
            "p_profit": round(sum(1 for t in totals if t > 0) / iters, 3),
            "expected_shortfall_5pct_r": round(es5, 2),
            "dd_p95_r": round(dds[int(0.95 * iters) - 1], 2),
            "median_total_r": round(totals[iters // 2], 2)}


def build_scorecard(rs: list, log: list | None = None) -> dict:
    m = metrics_of(rs)
    wf = purged_walk_forward(rs, log=log)
    cp = cpcv(rs, log=log)
    mc = monte_carlo(rs)
    thirds = [rs[i * len(rs) // 3:(i + 1) * len(rs) // 3]
              for i in range(3)]
    third_exps = [round(sum(c) / len(c), 3) for c in thirds if c]
    regime_pos = sum(1 for e in third_exps if e > 0)
    cost_exp = m["expectancy"] - COST_SHOCK_R
    slip_exp = m["expectancy"] - SLIPPAGE_SHOCK_R
    checks = [
        {"name": "positive expectancy", "value": m["expectancy"],
         "passed": m["expectancy"] > 0},
        {"name": "profit factor ≥ 1.15", "value": m["profit_factor"],
         "passed": m["profit_factor"] >= 1.15},
        {"name": "sortino ≥ 0.05", "value": m["sortino"],
         "passed": m["sortino"] >= 0.05},
        {"name": f"max drawdown ≤ {MAX_DD_R}R", "value": m["max_dd_r"],
         "passed": m["max_dd_r"] <= MAX_DD_R},
        {"name": "purged walk-forward ≥60% positive folds",
         "value": wf["folds"], "passed": wf["passed"]},
        {"name": f"CPCV OOS loss rate ≤ {OOS_LOSS_RATE_MAX}",
         "value": cp["oos_loss_rate"], "passed": cp["passed"]},
        {"name": f"Monte Carlo P(profit) ≥ {MC_P_PROFIT_MIN}",
         "value": mc["p_profit"], "passed":
         mc["p_profit"] >= MC_P_PROFIT_MIN},
        {"name": f"Monte Carlo ES(5%) ≥ {ES5_FLOOR_R}R",
         "value": mc["expected_shortfall_5pct_r"],
         "passed": mc["expected_shortfall_5pct_r"] >= ES5_FLOOR_R},
        {"name": f"survives +{COST_SHOCK_R}R cost shock",
         "value": round(cost_exp, 3), "passed": cost_exp > 0},
        {"name": f"survives +{SLIPPAGE_SHOCK_R}R slippage shock",
         "value": round(slip_exp, 3), "passed": slip_exp > -0.02},
        {"name": "regime stability (≥2/3 periods positive)",
         "value": third_exps, "passed": regime_pos >= 2},
    ]
    return {"qualified": all(c["passed"] for c in checks),
            "checks": checks, "metrics": m, "walk_forward": wf,
            "cpcv": cp, "monte_carlo": mc, "engine_version": 1,
            "at": _now()}


async def qualify(db, user_id: str, model_id: str) -> dict:
    """Replays the challenger over full bar history to obtain a
    timestamped per-trade R series, then scores it. Insufficient data →
    qualified=None (advisory only — the ladder still applies)."""
    from bson import ObjectId
    from bayes_opt import WARMUP, new_replay_state, precompute_features, \
        replay
    m = await db.shadow_models.find_one(
        {"_id": ObjectId(str(model_id)), "user_id": user_id})
    if not m:
        raise ValueError("shadow model not found")
    # fresh scorecard reuse — never recompute inside the freshness window
    prev = m.get("qualification2") or {}
    try:
        prev_at = datetime.fromisoformat(str(prev.get("at")))
        age = (datetime.now(timezone.utc) - prev_at).total_seconds()
    except (TypeError, ValueError):
        age = None
    if age is not None and age < SCORECARD_FRESH_S:
        return {"model_id": str(m["_id"]), "version": m.get("version"),
                "engine": m.get("engine"), "cached": True, **prev}
    _rate_check(user_id)
    doc = await db.intraday_candles.find_one(
        {"user_id": user_id, "symbol": m["symbol"], "timeframe": "M15"},
        {"bars": 1})          # fix plan A1 — own stream only
    bars = (doc or {}).get("bars") or []
    scorecard: dict
    if len(bars) < WARMUP + 80:
        scorecard = {"qualified": None, "advisory": True,
                     "note": f"insufficient bars ({len(bars)}) for "
                             f"qualification replay — ladder gates apply",
                     "at": _now()}
    else:
        def _cpu_work():
            # SEC-001 — replay + Monte Carlo run off the event loop
            feats = precompute_features(bars)
            st = new_replay_state()
            st["_r_log"] = []
            replay(m["engine"], bars, feats, m["params"], state=st)
            rs = [x["r"] for x in st["_r_log"]]
            if len(rs) < MIN_TRADES:
                return {"qualified": None, "advisory": True,
                        "note": f"only {len(rs)} replay trades "
                                f"(<{MIN_TRADES}) — ladder gates apply",
                        "at": _now()}
            return build_scorecard(rs, log=st["_r_log"])
        scorecard = await asyncio.to_thread(_cpu_work)
    await db.shadow_models.update_one(
        {"_id": m["_id"]}, {"$set": {"qualification2": scorecard}})
    return {"model_id": str(m["_id"]), "version": m.get("version"),
            "engine": m.get("engine"), **scorecard}
