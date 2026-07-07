"""iter-112 · Monte Carlo Trade Simulation — simulate before you enter.

Before any entry, 10,000 future price paths are simulated by bootstrap-
resampling the symbol's actual M15 bar dynamics (close-change + wick sizes —
preserves fat tails, volatility and drift; no Gaussian assumption). Each
path runs until SL hit, TP hit, or the horizon expires (same-bar collisions
count as SL — conservative).

Outputs: P(TP first) · P(SL first) · P(timeout) · EV in R · max-drawdown
distribution · median bars to resolution.
`mc_gate`: enter ONLY if expected value is positive."""
import logging

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PATHS = 10000
DEFAULT_HORIZON = 96          # 24h of M15
MIN_BARS = 40


def calibrate(bars):
    dcs, uws, dws = [], [], []
    for i in range(1, len(bars)):
        p, b = bars[i - 1], bars[i]
        dcs.append(b["c"] - p["c"])
        uws.append(max(0.0, b["h"] - max(b["c"], p["c"])))
        dws.append(max(0.0, min(b["c"], p["c"]) - b["l"]))
    return np.asarray(dcs), np.asarray(uws), np.asarray(dws)


def simulate_trade(action, entry, sl, tp, bars,
                   n_paths=DEFAULT_PATHS, horizon=DEFAULT_HORIZON,
                   seed=None) -> dict | None:
    if action not in ("BUY", "SELL") or not entry or not sl or not tp:
        return None
    if not bars or len(bars) < MIN_BARS:
        return None
    entry, sl, tp = float(entry), float(sl), float(tp)
    sl_dist = abs(entry - sl)
    if sl_dist <= 0 or abs(tp - entry) <= 0:
        return None
    dcs, uws, dws = calibrate(bars)
    n_hist = len(dcs)
    rng = np.random.default_rng(seed)
    is_buy = action == "BUY"

    price = np.full(n_paths, entry)
    active = np.ones(n_paths, dtype=bool)
    tp_hit = np.zeros(n_paths, dtype=bool)
    sl_hit = np.zeros(n_paths, dtype=bool)
    worst = np.zeros(n_paths)                       # adverse excursion ($)
    exit_step = np.full(n_paths, horizon, dtype=np.int32)

    for step in range(horizon):
        idx = rng.integers(0, n_hist, n_paths)
        new = price + dcs[idx]
        hi = np.maximum(price, new) + uws[idx]
        lo = np.minimum(price, new) - dws[idx]
        if is_buy:
            adverse = entry - lo
            bar_sl = lo <= sl
            bar_tp = hi >= tp
        else:
            adverse = hi - entry
            bar_sl = hi >= sl
            bar_tp = lo <= tp
        worst = np.where(active, np.maximum(worst, adverse), worst)
        newly_sl = active & bar_sl                  # collision → SL first
        newly_tp = active & bar_tp & ~bar_sl
        done = newly_sl | newly_tp
        sl_hit |= newly_sl
        tp_hit |= newly_tp
        exit_step = np.where(done, step + 1, exit_step)
        active &= ~done
        price = new
        if not active.any():
            break

    r_tp = abs(tp - entry) / sl_dist
    p_tp = float(tp_hit.mean())
    p_sl = float(sl_hit.mean())
    p_to = float(active.mean())
    timeout_r = ((price - entry) if is_buy else (entry - price)) / sl_dist
    to_mean_r = float(timeout_r[active].mean()) if active.any() else 0.0
    ev_r = p_tp * r_tp - p_sl * 1.0 + p_to * to_mean_r
    dd_r = worst / sl_dist
    return {"paths": int(n_paths), "horizon_bars": int(horizon),
            "p_tp_first": round(p_tp, 3), "p_sl_first": round(p_sl, 3),
            "p_timeout": round(p_to, 3), "rr": round(r_tp, 2),
            "ev_r": round(float(ev_r), 3),
            "timeout_mean_r": round(to_mean_r, 3),
            "max_dd_r_median": round(float(np.median(dd_r)), 2),
            "max_dd_r_p95": round(float(np.quantile(dd_r, 0.95)), 2),
            "median_bars_to_exit": int(np.median(exit_step))}


def mc_gate(mc: dict | None) -> str | None:
    """Enter only if the simulated expected value is positive."""
    if not mc:
        return None
    if mc["ev_r"] <= 0:
        return (f"Monte Carlo gate: {mc['paths']:,} simulated paths — "
                f"TP first {mc['p_tp_first']:.0%} vs SL first "
                f"{mc['p_sl_first']:.0%} (R:R {mc['rr']}) → expected value "
                f"{mc['ev_r']:+.2f}R. Negative EV, trade vetoed.")
    return None
