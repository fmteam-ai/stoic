"""iter-138 · Bayesian parameter optimization (institutional Phase B).

Tunes deterministic strategy-engine parameters with a Gaussian-Process
surrogate + Expected Improvement, evaluated by a truthful walk-forward
replay of the REAL M15 bar history the server has accumulated (~800 bars,
merged from EA candle pushes). Features are precomputed once per bar index
(they don't depend on the parameters), so each parameter evaluation is a
cheap pure-function sweep — the GP can afford 30+ real evaluations.

ADVISORY-ONLY: results are stored as `tuning_proposals`. A proposal must be
registered as a shadow challenger (model_shadow.py) and pass shadow testing
before it can ever touch a live config. Nothing here mutates bot behavior.
"""
import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timezone

import numpy as np

from intraday_features import compute_intraday_features, MIN_BARS
from strategy_engines import (DEFAULT_PARAMS, PARAM_BOUNDS, SCALP_ENGINES,
                              run_engine)

logger = logging.getLogger("bayes-opt")

WARMUP = max(MIN_BARS, 40)      # bars of context before the first entry
FEATURE_WINDOW = 200            # bars fed to the feature pack (matches live)
COOLDOWN_SEC = 2 * 900          # 2 bars between a close and the next entry
TP_MULT = 2.0                   # deterministic engines: TP = 2 × SL
DD_PENALTY = 0.5                # score = net_r − 0.5 × max drawdown (R)
MIN_TRADES_FOR_SCORE = 3


def precompute_features(bars: list) -> list:
    """features[i] = feature pack computed from bars strictly BEFORE bar i."""
    feats: list = [None] * len(bars)
    for i in range(WARMUP, len(bars)):
        feats[i] = compute_intraday_features(bars[max(0, i - FEATURE_WINDOW):i])
    return feats


def new_replay_state() -> dict:
    return {"equity": 0.0, "peak": 0.0, "max_dd": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "total_r": 0.0,
            "gross_win_r": 0.0, "gross_loss_r": 0.0,
            "open_pos": None, "cooldown_until_t": 0}


def _book(st: dict, r: float) -> None:
    st["trades"] += 1
    st["total_r"] += r
    if r > 0:
        st["wins"] += 1
        st["gross_win_r"] += r
    else:
        st["losses"] += 1
        st["gross_loss_r"] += -r
    st["equity"] += r
    st["peak"] = max(st["peak"], st["equity"])
    st["max_dd"] = max(st["max_dd"], st["peak"] - st["equity"])


def replay(engine: str, bars: list, feats_by_bar: list, params: dict | None,
           start: int | None = None, state: dict | None = None) -> dict:
    """Walk-forward replay with no lookahead: features at bar i come from
    bars < i, entry fills at bar i's open. Conservative SL-first when both
    SL and TP touch inside one bar. `state` allows incremental continuation
    (shadow testing) — pass the previous returned state back in."""
    st = state or new_replay_state()
    is_scalp = engine in SCALP_ENGINES
    for i in range(max(start if start is not None else WARMUP, WARMUP), len(bars)):
        b = bars[i]
        t = int(b.get("t") or 0)
        pos = st["open_pos"]
        if pos:
            hi, lo = float(b["h"]), float(b["l"])
            is_buy = pos["action"] == "BUY"
            sl_hit = (lo <= pos["sl"]) if is_buy else (hi >= pos["sl"])
            tp_hit = (hi >= pos["tp"]) if is_buy else (lo <= pos["tp"])
            if sl_hit:                      # conservative: SL first
                _book(st, -1.0)
                _r_closed = -1.0
            elif tp_hit:
                _book(st, TP_MULT)
                _r_closed = TP_MULT
            else:
                continue
            if "_r_log" in st:   # per-trade series for CC 2.0 scorecards
                st["_r_log"].append({"r": _r_closed, "t": t,
                                     "opened_t": pos.get("opened_t")})
            st["open_pos"] = None
            st["cooldown_until_t"] = t + COOLDOWN_SEC
            continue
        if t <= st["cooldown_until_t"]:
            continue
        f = feats_by_bar[i]
        if not f:
            continue
        sig, _ = run_engine(engine, f, params=params)
        if sig not in ("BUY", "SELL"):
            continue
        atr15 = float(f.get("atr15") or 0)
        if atr15 <= 0:
            continue
        entry = float(b["o"])
        sl_dist = (0.8 if is_scalp else 1.0) * atr15
        st["open_pos"] = {
            "action": sig, "entry": entry, "opened_t": t,
            "sl": entry - sl_dist if sig == "BUY" else entry + sl_dist,
            "tp": entry + TP_MULT * sl_dist if sig == "BUY"
                  else entry - TP_MULT * sl_dist,
        }
    return st


def score_of(st: dict, bars: list) -> float:
    """Objective for the GP: net R (with open-position mark-to-market)
    minus a drawdown penalty, minus a small-sample penalty."""
    total = st["total_r"]
    pos = st.get("open_pos")
    if pos and bars:
        last = float(bars[-1]["c"])
        sl_dist = abs(pos["entry"] - pos["sl"]) or 1e-9
        mtm = (last - pos["entry"]) if pos["action"] == "BUY" else (pos["entry"] - last)
        total += mtm / sl_dist
    score = total - DD_PENALTY * st["max_dd"]
    if st["trades"] < MIN_TRADES_FOR_SCORE:
        score -= 2.0
    return round(score, 4)


# ── Gaussian Process + Expected Improvement (pure numpy) ──────────────────

def _rbf(A: np.ndarray, B: np.ndarray, ls: float = 0.25) -> np.ndarray:
    d = ((A[:, None, :] - B[None, :, :]) ** 2).sum(axis=2)
    return np.exp(-0.5 * d / (ls ** 2))


def _gp_posterior(X: np.ndarray, y: np.ndarray, Xs: np.ndarray,
                  ls: float = 0.25, noise: float = 1e-4):
    K = _rbf(X, X, ls) + noise * np.eye(len(X))
    L = np.linalg.cholesky(K)
    alpha = np.linalg.solve(L.T, np.linalg.solve(L, y))
    Ks = _rbf(X, Xs, ls)
    mu = Ks.T @ alpha
    v = np.linalg.solve(L, Ks)
    var = np.clip(1.0 - (v * v).sum(axis=0), 1e-9, None)
    return mu, np.sqrt(var)


def _ei(mu: np.ndarray, sigma: np.ndarray, best: float) -> np.ndarray:
    z = (mu - best) / sigma
    phi = np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    Phi = 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2)))
    return (mu - best) * Phi + sigma * phi


def suggest_next(X: list, y: list, dims: int, rng: np.random.Generator,
                 n_candidates: int = 256) -> np.ndarray:
    """Fit GP on normalized (X, y) history, return the [0,1]^dims point
    with maximum Expected Improvement over `n_candidates` random probes."""
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    std = ya.std()
    yn = (ya - ya.mean()) / (std if std > 1e-9 else 1.0)
    cand = rng.random((n_candidates, dims))
    mu, sigma = _gp_posterior(Xa, yn, cand)
    ei = _ei(mu, sigma, yn.max())
    return cand[int(np.argmax(ei))]


def _to_params(x01: np.ndarray, names: list, bounds: dict) -> dict:
    out = {}
    for j, name in enumerate(names):
        lo, hi = bounds[name]
        out[name] = round(lo + float(np.clip(x01[j], 0, 1)) * (hi - lo), 4)
    return out


def _from_params(params: dict, names: list, bounds: dict) -> np.ndarray:
    x = np.zeros(len(names))
    for j, name in enumerate(names):
        lo, hi = bounds[name]
        x[j] = np.clip((float(params[name]) - lo) / (hi - lo), 0, 1)
    return x


def optimize_engine_params(engine: str, bars: list, iters: int = 22,
                           init: int = 8, seed: int = 7) -> dict:
    """Synchronous GP-EI loop. Returns best params + default baseline."""
    bounds = PARAM_BOUNDS[engine]
    names = sorted(bounds)
    dims = len(names)
    rng = np.random.default_rng(seed)
    feats = precompute_features(bars)

    def evaluate(params: dict) -> tuple:
        st = replay(engine, bars, feats, params)
        return score_of(st, bars), st

    default = dict(DEFAULT_PARAMS[engine])
    X, y, results = [], [], []

    def probe(x01: np.ndarray):
        params = _to_params(x01, names, bounds)
        s, st = evaluate(params)
        X.append(list(x01))
        y.append(s)
        results.append({"params": params, "score": s,
                        "trades": st["trades"], "wins": st["wins"],
                        "losses": st["losses"],
                        "total_r": round(st["total_r"], 2),
                        "max_dd_r": round(st["max_dd"], 2)})

    probe(_from_params(default, names, bounds))       # default is candidate #0
    for _ in range(init):
        probe(rng.random(dims))
    for _ in range(iters):
        probe(suggest_next(X, y, dims, rng))

    best_i = int(np.argmax(y))
    return {
        "engine": engine,
        "default": results[0],
        "best": results[best_i],
        "improvement": round(results[best_i]["score"] - results[0]["score"], 4),
        "evaluations": len(results),
        "top": sorted(results, key=lambda r: -r["score"])[:5],
    }


def proposal_id_of(engine: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps({"e": engine, "p": params},
                                sort_keys=True).encode()).hexdigest()[:10]
    return f"{engine}~{h}"


async def run_bayes_optimization(db, user_id: str, engine: str, symbol: str,
                                 iters: int = 22) -> dict:
    if engine not in PARAM_BOUNDS:
        raise ValueError(f"engine '{engine}' has no tunable parameters "
                         f"(tunable: {sorted(PARAM_BOUNDS)})")
    from pip_utils import base_symbol
    base = base_symbol(symbol)
    doc = await db.intraday_candles.find_one(
        {"user_id": user_id, "symbol": base, "timeframe": "M15"}, {"bars": 1})
    bars = (doc or {}).get("bars") or []
    if len(bars) < WARMUP + 60:
        raise ValueError(f"not enough M15 history for {base} "
                         f"({len(bars)} bars, need ≥{WARMUP + 60}) — "
                         f"keep the EA bridge streaming candles")

    res = await asyncio.to_thread(optimize_engine_params, engine, bars, iters)
    span_days = round((int(bars[-1]["t"]) - int(bars[0]["t"])) / 86400, 1)
    proposal = {
        "user_id": user_id,
        "engine": engine,
        "symbol": base,
        "version": proposal_id_of(engine, res["best"]["params"]),
        "params": res["best"]["params"],
        "default_params": dict(DEFAULT_PARAMS[engine]),
        "best": res["best"],
        "default": res["default"],
        "improvement": res["improvement"],
        "evaluations": res["evaluations"],
        "bars_used": len(bars),
        "days_span": span_days,
        "status": "proposed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.tuning_proposals.update_one(
        {"user_id": user_id, "version": proposal["version"], "symbol": base},
        {"$set": proposal}, upsert=True)
    logger.info("Bayes-opt %s/%s: default score %.2f → best %.2f (%s evals, %s bars)",
                engine, base, res["default"]["score"], res["best"]["score"],
                res["evaluations"], len(bars))
    return proposal
