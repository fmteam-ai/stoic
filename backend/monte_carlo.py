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

# iter-135 · trend-aware drift (July-15 diagnostic: IID bootstrap is
# drift-blind — with-trend entries on directional days were auto-vetoed)
DRIFT_LOOKBACK = 24           # 6h of M15 — recent-trend measurement window
DRIFT_HALF_LIFE = 16          # bars — injected drift decays as the trend ages
DRIFT_CAP_SIGMA = 0.5         # |excess drift| capped at 0.5σ per bar
DRIFT_MIN_SIG = 1.0           # inject only when |recent mean| ≥ 1 std error
STRONG_TREND_SIG = 2.0        # counter-trend gate arms at 2 std errors
CT_MIN_EV_R = 0.10            # fading a strong trend needs ≥ +0.10R net EV
MC_EV_TOLERANCE_R = 0.02      # iter-141 · enforce only below −0.02R net (cost
                              # model is an estimate; ≤2 cents/R inside its
                              # error bars — Jul 20-22: EV never > 0, 21
                              # near-misses within 0.02R, zero trades)


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
                   seed=None, cost_price: float = 0.0) -> dict | None:
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

    # iter-135 · measure recent drift vs the full calibration window. The
    # bootstrap already carries the FULL-sample mean drift (≈0 over weeks);
    # inject only the statistically significant EXCESS of the recent window,
    # capped and decaying, so directional days are priced without letting a
    # 6h move extrapolate for 24h.
    k = min(DRIFT_LOOKBACK, n_hist)
    recent = dcs[-k:]
    sigma = float(dcs.std(ddof=1)) if n_hist > 1 else 0.0
    se = sigma / (k ** 0.5) if k > 0 and sigma > 0 else 0.0
    mu_recent = float(recent.mean()) if k else 0.0
    drift_sig = abs(mu_recent) / se if se > 0 else 0.0
    excess_drift = 0.0
    if drift_sig >= DRIFT_MIN_SIG and sigma > 0:
        cap = DRIFT_CAP_SIGMA * sigma
        excess_drift = float(np.clip(mu_recent - float(dcs.mean()), -cap, cap))
    trend = ("up" if mu_recent > 0 else "down") \
        if drift_sig >= DRIFT_MIN_SIG else "flat"
    trend_aligned = None if trend == "flat" else ((trend == "up") == is_buy)

    price = np.full(n_paths, entry)
    active = np.ones(n_paths, dtype=bool)
    tp_hit = np.zeros(n_paths, dtype=bool)
    sl_hit = np.zeros(n_paths, dtype=bool)
    worst = np.zeros(n_paths)                       # adverse excursion ($)
    exit_step = np.full(n_paths, horizon, dtype=np.int32)

    for step in range(horizon):
        idx = rng.integers(0, n_hist, n_paths)
        step_drift = (excess_drift * (0.5 ** (step / DRIFT_HALF_LIFE))
                      if excess_drift else 0.0)
        new = price + dcs[idx] + step_drift
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
    # iter-134 · Cost-aware EV (quant roadmap #4): spread + slippage paid on
    # every trade, expressed in R. Marginal setups that are only +EV before
    # costs get filtered.
    cost_r = float(cost_price) / sl_dist if cost_price else 0.0
    dd_r = worst / sl_dist
    return {"paths": int(n_paths), "horizon_bars": int(horizon),
            "p_tp_first": round(p_tp, 3), "p_sl_first": round(p_sl, 3),
            "p_timeout": round(p_to, 3), "rr": round(r_tp, 2),
            "ev_r": round(float(ev_r), 3),
            "cost_r": round(cost_r, 3),
            "ev_r_net": round(float(ev_r) - cost_r, 3),
            "timeout_mean_r": round(to_mean_r, 3),
            "drift_price_per_bar": round(excess_drift, 6),
            "drift_sig": round(drift_sig, 2),
            "trend": trend, "trend_aligned": trend_aligned,
            "max_dd_r_median": round(float(np.median(dd_r)), 2),
            "max_dd_r_p95": round(float(np.quantile(dd_r, 0.95)), 2),
            "median_bars_to_exit": int(np.median(exit_step))}


# Typical all-in round-trip cost (spread + slippage buffer) in PRICE units.
TYPICAL_SPREAD = {"XAUUSD": 0.35, "XAGUSD": 0.035, "BTCUSD": 30.0,
                  "ETHUSD": 2.5, "US30": 3.0, "NAS100": 2.0, "SPX500": 0.6,
                  "EURUSD": 0.00012, "GBPUSD": 0.00015, "USDJPY": 0.015}
SLIPPAGE_BUFFER = 1.5


def typical_cost(symbol: str, entry: float) -> float:
    base = str(symbol or "").upper()
    spread = TYPICAL_SPREAD.get(base, float(entry or 0) * 0.0001)
    return spread * SLIPPAGE_BUFFER


def mc_gate(mc: dict | None) -> str | None:
    """Enter only if the simulated EV net of costs clears −MC_EV_TOLERANCE_R.
    Symmetric counter-trend rule (iter-135): fading a strong recent trend
    (≥2σ drift) demands a strictly positive EV margin — the same rigor that
    blocks chasing negative-EV breakouts blocks fading directional days."""
    if not mc:
        return None
    ev_net = mc.get("ev_r_net", mc["ev_r"])
    if (mc.get("trend_aligned") is False
            and float(mc.get("drift_sig") or 0) >= STRONG_TREND_SIG
            and ev_net < CT_MIN_EV_R):
        return (f"Counter-trend gate: fading a strong {mc.get('trend')}-trend "
                f"({mc['drift_sig']:.1f}σ recent drift) requires ≥ "
                f"+{CT_MIN_EV_R:.2f}R net EV — simulation shows "
                f"{ev_net:+.2f}R net. Trade vetoed.")
    if ev_net < -MC_EV_TOLERANCE_R:
        cost_note = (f" − {mc['cost_r']:.2f}R costs" if mc.get("cost_r") else "")
        trend_note = ""
        if mc.get("trend") in ("up", "down"):
            trend_note = (f" [trend-aware: {mc['trend']} drift "
                          f"{mc['drift_sig']:.1f}σ priced in]")
        return (f"Monte Carlo gate: {mc['paths']:,} simulated paths — "
                f"TP first {mc['p_tp_first']:.0%} vs SL first "
                f"{mc['p_sl_first']:.0%} (R:R {mc['rr']}) → expected value "
                f"{mc['ev_r']:+.2f}R{cost_note} = {ev_net:+.2f}R net "
                f"(tolerance −{MC_EV_TOLERANCE_R:.2f}R)."
                f"{trend_note} Negative EV, trade vetoed.")
    return None
