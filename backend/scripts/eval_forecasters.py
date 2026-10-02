#!/usr/bin/env python
"""Rolling-origin evaluation of probabilistic price forecasters.

For each forecaster and each origin t (every --step bars, after
--min-context bars of history) the forecaster sees closes[:t+1] and emits
the QUANTILE_LEVELS quantiles of the close --horizon bars ahead. Scored
against the realised close with:

* pinball (quantile) loss, averaged over levels, in return units (bp);
* CRPS, approximated from the quantile grid as 2·mean_τ pinball_τ
  (exact in the limit of a dense τ grid; with deciles it is the standard
  quantile-decomposition estimate used by e.g. GluonTS);
* empirical coverage of the outer (q10..q90) band, raw AND after online
  Adaptive Conformal Inference (conformal.ACIState) — what the live
  forecast gate actually uses once the per-user state is mature.

Data: stored M15 candles from db.intraday_candles (``--user``/``--symbol``,
needs MONGO_URL + DB_NAME) or a JSON file of bars (``--bars-file``; a list
of {"t","o","h","l","c"} or {"bars": [...]}).

Forecasters (``--models``, comma separated):
  naive      empirical quantiles of the trailing window's h-step changes
             added to the last close (random walk, no distribution model);
  gaussian   random walk with σ from the trailing 1-step changes × √h;
  chronos    forecast_agent's Chronos-Bolt path (imports torch — only
             loaded when selected; requires the heavy-ML gate to be open).

Adding a candidate (e.g. Chronos-2 or TimesFM-2.5)
-------------------------------------------------
Register a function ``f(closes: np.ndarray, horizon: int, levels: list)
-> list[float]`` (the quantile VALUES of the price `horizon` steps ahead,
one per level, non-decreasing) in ``FORECASTERS`` below, importing the
heavy library INSIDE the function so this script never imports torch for
the baselines. Sketches (model ids / call signatures follow the projects'
READMEs at the time of writing — verify against the installed version):

    def _chronos2(closes, horizon, levels):
        import torch
        from chronos import BaseChronosPipeline          # chronos-forecasting ≥2
        pipe = _cached("chronos2", lambda: BaseChronosPipeline.from_pretrained(
            "amazon/chronos-2", device_map="cpu", torch_dtype=torch.float32))
        q, _ = pipe.predict_quantiles(inputs=torch.tensor(closes[-2048:]),
                                      prediction_length=horizon,
                                      quantile_levels=levels)
        return q[0][-1].tolist()

    def _timesfm25(closes, horizon, levels):
        import timesfm                                   # timesfm ≥2.5
        m = _cached("tfm25", lambda: timesfm.TimesFM_2p5_200M_torch
                    .from_pretrained("google/timesfm-2.5-200m-pytorch"))
        # compile once with ForecastConfig(max_context=..., max_horizon=...,
        # use_continuous_quantile_head=True); m.forecast returns
        # (point, quantiles[..., 10]) with mean + deciles 0.1..0.9:
        _, qs = m.forecast(horizon=horizon, inputs=[closes[-1024:]])
        return [float(v) for v in qs[0, horizon - 1, 1:10]]

    FORECASTERS["chronos2"] = _chronos2
    FORECASTERS["timesfm25"] = _timesfm25

then run with ``--models naive,chronos,chronos2,timesfm25``. Promote a
candidate into forecast_agent only if it beats the incumbent on CRPS over
several symbols AND its conformal coverage is close to target.

Usage:
  python scripts/eval_forecasters.py --bars-file bars.json --horizon 8
  python scripts/eval_forecasters.py --user <uid> --symbol XAUUSD \
      --models naive,gaussian --step 4 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
_CACHE: dict = {}


def _cached(key, factory):
    if key not in _CACHE:
        _CACHE[key] = factory()
    return _CACHE[key]


# ─────────────────────────────────────────────────────────── forecasters
def naive_quantiles(closes, horizon, levels, window: int = 500):
    c = np.asarray(closes, float)[-(window + horizon):]
    if len(c) <= horizon + 5:
        return None
    diffs = c[horizon:] - c[:-horizon]
    return [float(c[-1] + np.quantile(diffs, lv)) for lv in levels]


def gaussian_quantiles(closes, horizon, levels, window: int = 500):
    from statistics import NormalDist
    c = np.asarray(closes, float)[-(window + 1):]
    if len(c) < 10:
        return None
    sd = float(np.std(np.diff(c), ddof=1)) * np.sqrt(horizon)
    nd = NormalDist()
    return [float(c[-1] + sd * nd.inv_cdf(lv)) for lv in levels]


def chronos_quantiles(closes, horizon, levels):
    """The production Chronos-Bolt path (forecast_agent._forecast_sync)."""
    import forecast_agent  # noqa: WPS433 — imports torch lazily inside
    q = forecast_agent._forecast_sync(list(map(float, closes)), horizon)
    if not q:
        return None
    row = q[-1]
    lv = forecast_agent.QUANTILE_LEVELS
    return [float(np.interp(x, lv, row)) for x in levels]


FORECASTERS = {"naive": naive_quantiles, "gaussian": gaussian_quantiles,
               "chronos": chronos_quantiles}


# ────────────────────────────────────────────────────────────── scoring
def pinball(q_values, levels, y) -> np.ndarray:
    q = np.asarray(q_values, float)
    tau = np.asarray(levels, float)
    d = y - q
    return np.maximum(tau * d, (tau - 1.0) * d)


def crps_from_quantiles(q_values, levels, y) -> float:
    return float(2.0 * np.mean(pinball(q_values, levels, y)))


def evaluate(closes, model: str, horizon: int = 8, min_context: int = 200,
             step: int = 4, levels=QUANTILE_LEVELS,
             max_origins: int | None = None, conformal: bool = True) -> dict:
    fn = FORECASTERS[model]
    closes = np.asarray(closes, float)
    origins = list(range(min_context, len(closes) - horizon, step))
    if max_origins:
        origins = origins[-max_origins:]
    pin, crps, cover = [], [], []
    aci = None
    if conformal:
        from conformal import ACIState
        aci = ACIState(alpha=1 - (levels[-1] - levels[0]), gamma=0.01,
                       alpha_t=1 - (levels[-1] - levels[0]), window=250)
    pending = []   # ACI is updated only once an outcome is realised
    for t in origins:
        while pending and pending[0][0] <= t:
            _, lo, hi, y_p, m_p = pending.pop(0)
            aci.update(lo, hi, y_p, margin_used=m_p)
        q = fn(closes[:t + 1], horizon, list(levels))
        if q is None:
            continue
        q = np.maximum.accumulate(np.asarray(q, float))
        y = float(closes[t + horizon])
        last = float(closes[t])
        # scores in return units (bp) so symbols are comparable
        pin.append(float(np.mean(pinball(q / last, levels, y / last))) * 1e4)
        crps.append(crps_from_quantiles(q / last, levels, y / last) * 1e4)
        cover.append(1.0 if q[0] <= y <= q[-1] else 0.0)
        if aci is not None:
            m = aci.margin() if aci.n_updates >= 20 else 0.0
            pending.append((t + horizon, float(q[0]), float(q[-1]), y, m))
    out = {"model": model, "horizon": horizon, "origins": len(pin),
           "pinball_bp": round(float(np.mean(pin)), 4) if pin else None,
           "crps_bp": round(float(np.mean(crps)), 4) if crps else None,
           "coverage_raw": round(float(np.mean(cover)), 3) if cover else None,
           "target_coverage": round(levels[-1] - levels[0], 3)}
    if aci is not None and aci.n_updates:
        out["coverage_aci"] = round(aci.n_cov_conf / aci.n_updates, 3)
    return out


# ─────────────────────────────────────────────────────────────── data
def load_bars_file(path: str) -> list:
    with open(path) as fh:
        data = json.load(fh)
    bars = data.get("bars") if isinstance(data, dict) else data
    return sorted(bars or [], key=lambda b: float(b.get("t") or 0))


def load_bars_db(user: str, symbol: str, timeframe: str = "M15") -> list:
    from pymongo import MongoClient
    client = MongoClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    q = {"symbol": symbol, "timeframe": timeframe}
    if user:
        q["user_id"] = user
    doc = db.intraday_candles.find_one(q, {"bars": 1}) or {}
    client.close()
    return sorted(doc.get("bars") or [], key=lambda b: float(b.get("t") or 0))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bars-file")
    ap.add_argument("--user", default="")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--models", default="naive,gaussian")
    ap.add_argument("--horizon", type=int, default=8)
    ap.add_argument("--min-context", type=int, default=200)
    ap.add_argument("--step", type=int, default=4)
    ap.add_argument("--max-origins", type=int, default=None)
    ap.add_argument("--no-conformal", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    bars = (load_bars_file(a.bars_file) if a.bars_file
            else load_bars_db(a.user, a.symbol, a.timeframe))
    closes = [float(b["c"]) for b in bars if b.get("c") is not None]
    if len(closes) < a.min_context + a.horizon + 10:
        print(f"not enough bars ({len(closes)})", file=sys.stderr)
        return 2
    results = []
    for m in [x.strip() for x in a.models.split(",") if x.strip()]:
        if m not in FORECASTERS:
            print(f"unknown model {m!r}; known: {sorted(FORECASTERS)}",
                  file=sys.stderr)
            return 2
        results.append(evaluate(closes, m, a.horizon, a.min_context, a.step,
                                max_origins=a.max_origins,
                                conformal=not a.no_conformal))
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        hdr = f"{'model':<10}{'origins':>8}{'pinball_bp':>12}{'crps_bp':>10}" \
              f"{'cov_raw':>9}{'cov_aci':>9}"
        print(hdr)
        for r in results:
            print(f"{r['model']:<10}{r['origins']:>8}{r['pinball_bp']!s:>12}"
                  f"{r['crps_bp']!s:>10}{r['coverage_raw']!s:>9}"
                  f"{r.get('coverage_aci', '-')!s:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
