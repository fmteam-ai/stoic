"""iter-138 · Bayesian parameter optimization (institutional Phase B).

Tunes deterministic strategy-engine parameters with a Gaussian-Process
surrogate + Expected Improvement, evaluated by a causal (no-lookahead)
replay of the REAL M15 bar history the server has accumulated (~800 bars,
merged from EA candle pushes). Features are precomputed once per bar index
(they don't depend on the parameters), so each parameter evaluation is a
cheap pure-function sweep — the GP can afford 30+ real evaluations.

Validation (validation.py):
  · every booked trade is charged a round-trip cost in R (spread +
    slippage + commission over the stop distance, floored) — no more
    frictionless −1R/+2R;
  · bars are split CHRONOLOGICALLY: the GP only ever sees the first
    IS_FRAC (70%); the default and the selected params are then replayed
    on the untouched last 30% (after a small embargo) and reported as OOS;
  · every trial is returned, so the Deflated Sharpe Ratio can deflate the
    winner's in-sample Sharpe by the real number of trials, and the trials'
    per-block in-sample P&L feeds a CSCV Probability of Backtest
    Overfitting estimate.

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
from validation import (dsr_from_trials, probability_of_backtest_overfitting,
                        resolve_costs, sharpe, trade_cost_r)
from strategy_engines import (DEFAULT_PARAMS, PARAM_BOUNDS, SCALP_ENGINES,
                              run_engine)

logger = logging.getLogger("bayes-opt")

WARMUP = max(MIN_BARS, 40)      # bars of context before the first entry
FEATURE_WINDOW = 200            # bars fed to the feature pack (matches live)
COOLDOWN_SEC = 2 * 900          # 2 bars between a close and the next entry
TP_MULT = 2.0                   # deterministic engines: TP = 2 × SL
DD_PENALTY = 0.5                # score = net_r − 0.5 × max drawdown (R)
MIN_TRADES_FOR_SCORE = 3
IS_FRAC = 0.7                   # first 70% of bars: optimiser only sees these
EMBARGO_BARS = 4                # gap between in-sample end and OOS start
PBO_BLOCKS = 8                  # CSCV blocks over the in-sample span


def precompute_features(bars: list) -> list:
    """features[i] = feature pack computed from bars strictly BEFORE bar i."""
    feats: list = [None] * len(bars)
    for i in range(WARMUP, len(bars)):
        feats[i] = compute_intraday_features(bars[max(0, i - FEATURE_WINDOW):i])
    return feats


def new_replay_state() -> dict:
    return {"equity": 0.0, "peak": 0.0, "max_dd": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "total_r": 0.0,
            "gross_win_r": 0.0, "gross_loss_r": 0.0, "cost_r": 0.0,
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
           start: int | None = None, state: dict | None = None, *,
           costs: dict | None = None) -> dict:
    """Causal replay with no lookahead: features at bar i come from
    bars < i, entry fills at bar i's open. Conservative SL-first when both
    SL and TP touch inside one bar. `state` allows incremental continuation
    (shadow testing) — pass the previous returned state back in.

    `costs` (validation.resolve_costs spec) charges each trade its round-trip
    cost in R at entry (stored on the position); booked R is NET of cost.
    None keeps the legacy frictionless booking."""
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
            c_r = float(pos.get("cost_r") or 0.0)
            if sl_hit:                      # conservative: SL first
                _r_closed = -1.0 - c_r
            elif tp_hit:
                _r_closed = TP_MULT - c_r
            else:
                continue
            _book(st, _r_closed)
            if c_r:
                st["cost_r"] = st.get("cost_r", 0.0) + c_r
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
            "cost_r": round(trade_cost_r(costs, sl_dist), 4),
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
        total += mtm / sl_dist - float(pos.get("cost_r") or 0.0)
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


def split_bars(n_bars: int, is_frac: float = IS_FRAC,
               embargo_bars: int = EMBARGO_BARS) -> tuple[int, int]:
    """Chronological split → (is_end, oos_start): bars[:is_end] are in-sample,
    bars[oos_start:] out-of-sample, with an `embargo_bars` gap between."""
    is_end = int(n_bars * is_frac)
    return is_end, min(n_bars, is_end + max(0, int(embargo_bars)))


def _trade_rs(st: dict) -> list:
    return [float(e["r"]) for e in st.get("_r_log") or []]


def _metrics(st: dict, bars: list) -> dict:
    rs = _trade_rs(st)
    s = sharpe(rs)
    return {"score": score_of(st, bars), "trades": st["trades"],
            "wins": st["wins"], "losses": st["losses"],
            "total_r": round(st["total_r"], 2),
            "cost_r": round(st.get("cost_r", 0.0), 2),
            "expectancy_r": round(st["total_r"] / st["trades"], 4)
            if st["trades"] else None,
            "max_dd_r": round(st["max_dd"], 2),
            "sharpe": round(s, 4) if s is not None else None}


def _block_sums(st: dict, t_lo: int, t_hi: int, n_blocks: int) -> list:
    """Net R per equal-time block of the in-sample span (for CSCV/PBO)."""
    out = [0.0] * n_blocks
    span = max(1, t_hi - t_lo)
    for e in st.get("_r_log") or []:
        k = int((int(e["t"]) - t_lo) * n_blocks / span)
        out[min(max(k, 0), n_blocks - 1)] += float(e["r"])
    return out


def optimize_engine_params(engine: str, bars: list, iters: int = 22,
                           init: int = 8, seed: int = 7, *,
                           costs: dict | None = None, symbol: str | None = None,
                           is_frac: float = IS_FRAC,
                           embargo_bars: int = EMBARGO_BARS) -> dict:
    """Synchronous GP-EI loop on the IN-SAMPLE bars only, then an honest
    out-of-sample replay of the default and the selected params.

    Every replay books NET R (costs via validation.resolve_costs: explicit
    `costs` > symbol default table > R floor). Returns the in-sample best
    (`best`, `improvement` — kept for backward compatibility, now labelled
    in-sample), the OOS comparison (`oos`, `oos_improvement`), every trial
    (`trials`), and DSR/PBO diagnostics computed from those trials."""
    bounds = PARAM_BOUNDS[engine]
    names = sorted(bounds)
    dims = len(names)
    rng = np.random.default_rng(seed)
    cost_spec = resolve_costs(symbol, costs)
    feats = precompute_features(bars)
    is_end, oos_start = split_bars(len(bars), is_frac, embargo_bars)
    is_bars, is_feats = bars[:is_end], feats[:is_end]
    t_lo = int(bars[min(WARMUP, max(is_end - 1, 0))].get("t") or 0) if bars else 0
    t_hi = int(is_bars[-1].get("t") or 0) if is_bars else 0

    def run(params: dict, b: list, f: list, start: int | None = None) -> dict:
        st = new_replay_state()
        st["_r_log"] = []
        return replay(engine, b, f, params, start=start, state=st,
                      costs=cost_spec)

    default = dict(DEFAULT_PARAMS[engine])
    X, y, results, blocks = [], [], [], []

    def probe(x01: np.ndarray):
        params = _to_params(x01, names, bounds)
        st = run(params, is_bars, is_feats)       # optimiser sees IS ONLY
        m = _metrics(st, is_bars)
        X.append(list(x01))
        y.append(m["score"])
        results.append({"params": params, **m, "_rs": _trade_rs(st)})
        blocks.append(_block_sums(st, t_lo, t_hi, PBO_BLOCKS))

    probe(_from_params(default, names, bounds))       # default is candidate #0
    for _ in range(init):
        probe(rng.random(dims))
    for _ in range(iters):
        probe(suggest_next(X, y, dims, rng))

    best_i = int(np.argmax(y))
    best_rs = results[best_i]["_rs"]
    trials = [{k: v for k, v in r.items() if k != "_rs"} for r in results]

    # ── out-of-sample: same causal features, untouched bars ──────────────
    oos = {"bars": max(0, len(bars) - oos_start), "start_index": oos_start}
    if oos_start < len(bars) - 1:
        st_d = run(results[0]["params"], bars, feats, start=oos_start)
        st_b = run(results[best_i]["params"], bars, feats, start=oos_start)
        oos["default"] = _metrics(st_d, bars)
        oos["best"] = _metrics(st_b, bars)
        oos_improvement = round(oos["best"]["total_r"]
                                - oos["default"]["total_r"], 4)
    else:
        oos["default"] = oos["best"] = None
        oos_improvement = None

    dsr = dsr_from_trials(best_rs, [t["sharpe"] for t in trials])
    try:
        pbo = probability_of_backtest_overfitting(
            np.asarray(blocks, dtype=float).T, n_blocks=PBO_BLOCKS,
            metric="mean")
    except ValueError as e:
        pbo = {"pbo": None, "reason": str(e)}

    return {
        "engine": engine,
        "default": trials[0],
        "best": trials[best_i],
        "improvement": round(trials[best_i]["score"] - trials[0]["score"], 4),
        "improvement_basis": "in_sample_score",
        "oos": oos,
        "oos_improvement": oos_improvement,
        "oos_improvement_basis": "net_R_after_costs (best − default)",
        "evaluations": len(results),
        "top": sorted(trials, key=lambda r: -r["score"])[:5],
        "trials": trials,
        "best_is_returns": [round(r, 4) for r in best_rs],
        "dsr": dsr,
        "pbo": pbo,
        "split": {"is_bars": is_end, "oos_start": oos_start,
                  "n_bars": len(bars), "embargo_bars": oos_start - is_end,
                  "is_until_t": t_hi,
                  "oos_from_t": int(bars[oos_start]["t"])
                  if oos_start < len(bars) else None},
        "costs": cost_spec,
    }


def proposal_id_of(engine: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps({"e": engine, "p": params},
                                sort_keys=True).encode()).hexdigest()[:10]
    return f"{engine}~{h}"


async def _cumulative_trials(db, user_id: str, engine: str, symbol: str,
                             res: dict) -> dict:
    """Persist this run's trials in `tuning_trial_log` and return the
    CUMULATIVE trial count + Sharpe dispersion for (user, engine, symbol):
    re-optimising the same history every night is more trials, not fewer,
    so DSR must deflate by every evaluation ever made on this combo."""
    srs = [t["sharpe"] for t in res["trials"] if t.get("sharpe") is not None]
    inc = {"n_trials": res["evaluations"], "n_runs": 1,
           "n_sr": len(srs), "sum_sr": float(sum(srs)),
           "sum_sr2": float(sum(s * s for s in srs))}
    key = {"user_id": user_id, "engine": engine, "symbol": symbol}
    try:
        await db.tuning_trial_log.update_one(
            key, {"$inc": inc,
                  "$set": {"last_run_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        doc = await db.tuning_trial_log.find_one(key) or {}
    except Exception as e:  # noqa: BLE001 — never lose the run over the log
        logger.warning("tuning_trial_log write failed: %s", e)
        doc = {}
    n = max(int(doc.get("n_trials") or 0), res["evaluations"])
    n_sr = int(doc.get("n_sr") or 0)
    if n_sr > 1:
        mean = doc["sum_sr"] / n_sr
        var = max(0.0, (doc["sum_sr2"] - n_sr * mean * mean) / (n_sr - 1))
    else:
        var = res["dsr"].get("sr_var_trials") or 0.0
    return {"n_trials_total": n, "n_runs": int(doc.get("n_runs") or 1),
            "sr_var_trials": round(var, 6), "n_trials_this_run": res["evaluations"]}


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

    res = await asyncio.to_thread(optimize_engine_params, engine, bars, iters,
                                  symbol=base)
    trial_log = await _cumulative_trials(db, user_id, engine, base, res)
    # DSR deflated by the CUMULATIVE number of trials on this combo
    dsr = dsr_from_trials(res["best_is_returns"],
                          [t["sharpe"] for t in res["trials"]],
                          n_trials=trial_log["n_trials_total"],
                          sr_var_override=trial_log["sr_var_trials"])
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
        "improvement_basis": res["improvement_basis"],
        "oos": res["oos"],
        "oos_improvement": res["oos_improvement"],
        "dsr": dsr,
        "pbo": res["pbo"],
        "split": res["split"],
        "costs": res["costs"],
        "trial_log": {**trial_log,
                      "trials": [{k: t.get(k) for k in (
                          "params", "score", "trades", "total_r", "sharpe")}
                          for t in res["trials"]]},
        "evaluations": res["evaluations"],
        "bars_used": len(bars),
        "days_span": span_days,
        "status": "proposed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.tuning_proposals.update_one(
        {"user_id": user_id, "version": proposal["version"], "symbol": base},
        {"$set": proposal}, upsert=True)
    logger.info("Bayes-opt %s/%s: IS score %.2f → %.2f, OOS ΔR %s, DSR %s, "
                "PBO %s (%s evals, %s cumulative trials, %s bars)",
                engine, base, res["default"]["score"], res["best"]["score"],
                res["oos_improvement"], dsr.get("dsr"),
                (res["pbo"] or {}).get("pbo"), res["evaluations"],
                trial_log["n_trials_total"], len(bars))
    return proposal
