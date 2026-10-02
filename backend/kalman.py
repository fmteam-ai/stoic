"""1-D Kalman filter for price denoising.

Adaptive smoothing of a noisy price series. Cuts microstructure / wick noise
without the lag of a long SMA. Used as a denoised input to AI signals
(`kalman_close`, `kalman_velocity`).

The model treats price as a hidden state x_t with constant-velocity drift:
    x_t  = x_{t-1} + v_{t-1} * dt + w_t   (process noise w_t ~ N(0, Q))
    v_t  = v_{t-1}                + r_t   (process noise r_t ~ N(0, R))
    y_t  = x_t + n_t                       (measurement noise n_t ~ N(0, S))

Returns the smoothed state estimates for the full series. Q/R/S are exposed
as tuning knobs — defaults are sane for daily XAUUSD closes.

This is intentionally dependency-free (no scipy / filterpy) so it can run on
the backend container without bloating the image.
"""
from typing import Iterable


def kalman_smooth(
    prices: Iterable[float],
    *,
    process_var: float = 1e-3,
    measurement_var: float = 1.0,
    initial_velocity: float = 0.0,
) -> list[dict]:
    """Return [{price, k_price, k_velocity}] for the full input series.

    Parameters
    ----------
    prices : iterable of floats
        Raw close-price series, oldest first.
    process_var : float
        Process noise — how much the underlying price is allowed to drift
        between samples. Larger = faster adaptation, more noise pass-through.
    measurement_var : float
        Measurement noise — trust in each observation. Larger = more smoothing.
    initial_velocity : float
        Seed velocity (price units / step). 0 is fine for daily bars.
    """
    p_list = [float(x) for x in prices if x is not None]
    if not p_list:
        return []
    if len(p_list) == 1:
        return [{"price": p_list[0], "k_price": p_list[0], "k_velocity": 0.0}]

    # State vector [x, v] with covariance P (2x2 represented as 4 floats).
    x = p_list[0]
    v = float(initial_velocity)
    p_xx, p_xv, p_vv = 1.0, 0.0, 1.0   # symmetric

    Q = float(process_var)         # process noise covariance on x
    Qv = float(process_var) * 10   # velocity allowed to drift faster
    R = float(measurement_var)      # measurement noise

    out: list[dict] = [
        {"price": p_list[0], "k_price": x, "k_velocity": v},
    ]

    for i in range(1, len(p_list)):
        z = p_list[i]
        # ---- Predict ----
        x = x + v
        # P = F P F^T + Q where F = [[1,1],[0,1]]
        new_p_xx = p_xx + 2 * p_xv + p_vv + Q
        new_p_xv = p_xv + p_vv
        new_p_vv = p_vv + Qv
        p_xx, p_xv, p_vv = new_p_xx, new_p_xv, new_p_vv

        # ---- Update with measurement z (observes x only) ----
        # innovation
        y = z - x
        S = p_xx + R
        # Kalman gain K = P H^T / S  with H = [1, 0]
        k_x = p_xx / S
        k_v = p_xv / S
        x = x + k_x * y
        v = v + k_v * y
        # P = (I - K H) P
        # All three use the PRIOR (predicted) covariance terms.
        p_xx, p_xv, p_vv = ((1 - k_x) * p_xx,
                            (1 - k_x) * p_xv,
                            p_vv - k_v * p_xv)
        out.append({"price": z, "k_price": x, "k_velocity": v})

    return out


def kalman_features(prices: Iterable[float]) -> dict:
    """Return a compact summary suitable for inclusion in a signal payload."""
    smoothed = kalman_smooth(prices)
    if not smoothed:
        return {"k_price": None, "k_velocity": None, "noise_pct": None}
    last = smoothed[-1]
    raw_last = last["price"]
    # 5-day noise estimate: stdev of (raw - smoothed) over last 5 samples
    tail = smoothed[-5:]
    if len(tail) >= 2:
        diffs = [s["price"] - s["k_price"] for s in tail]
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        sd = var ** 0.5
        noise_pct = (sd / raw_last * 100) if raw_last else None
    else:
        noise_pct = None
    return {
        "k_price": round(last["k_price"], 5),
        "k_velocity": round(last["k_velocity"], 6),
        "noise_pct": round(noise_pct, 4) if noise_pct is not None else None,
    }
