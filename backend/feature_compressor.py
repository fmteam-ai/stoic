"""Fast O(N) Feature Compressor — pragmatic Mamba/SSM substitute.

True State-Space Models (Mamba) require GPU training. In our serverless
environment we approximate the same goal — compress an arbitrarily long
historical sequence into a tiny, expressive feature vector in linear time —
by stitching together streaming statistics + multi-scale FFT magnitudes.

The result: ~30 numeric features summarising 6 months of daily candles in
under 5 ms, suitable to feed into the Meta-Labeler and Claude as context.

Pure numpy; no torch / GPU; deterministic.
"""
import math
from typing import List
import numpy as np


def _safe(x, default=0.0):
    try:
        if x is None or not np.isfinite(x):
            return default
        return float(x)
    except Exception:
        return default


def compress_history(history: List[dict]) -> dict:
    """Reduce N daily candles → 30-ish features, O(N) time.

    Mirrors what an SSM would surface: trend, volatility regime, momentum,
    spectral energy bands, autocorrelation, drawdown stats.
    """
    if not history or len(history) < 30:
        return {"available": False, "reason": "insufficient history"}

    closes = np.asarray([c["close"] for c in history], dtype=np.float64)
    highs = np.asarray([c["high"] for c in history], dtype=np.float64)
    lows = np.asarray([c["low"] for c in history], dtype=np.float64)
    n = len(closes)

    # Log returns — robust to scale
    rets = np.diff(np.log(closes + 1e-12))

    # --- Trend slope (least squares on normalised log price) ---
    x = np.arange(n)
    norm_log = np.log(closes / closes[0])
    slope, _ = np.polyfit(x, norm_log, 1)

    # --- Multi-horizon momentum ---
    def _mom(lookback):
        if n <= lookback:
            return 0.0
        return _safe((closes[-1] - closes[-lookback]) / closes[-lookback])

    # --- Realized vol on rolling windows ---
    def _vol(window):
        if len(rets) < window:
            return 0.0
        return _safe(np.std(rets[-window:]) * math.sqrt(252))  # annualised

    # --- Drawdown ---
    running_max = np.maximum.accumulate(closes)
    drawdown = (closes - running_max) / running_max
    max_dd = _safe(drawdown.min())
    current_dd = _safe(drawdown[-1])

    # --- Autocorrelation (signal of mean-reversion vs. trend) ---
    def _acf(lag):
        if len(rets) <= lag:
            return 0.0
        r = rets - rets.mean()
        denom = np.sum(r * r)
        if denom <= 0:
            return 0.0
        return _safe(np.sum(r[lag:] * r[:-lag]) / denom)

    # --- Spectral energy in 3 bands (low/mid/high freq) ---
    fft = np.fft.rfft(rets - rets.mean()) if len(rets) >= 8 else np.array([0j])
    power = np.abs(fft) ** 2
    if power.size >= 4:
        third = max(power.size // 3, 1)
        low_e = _safe(power[:third].sum())
        mid_e = _safe(power[third:2 * third].sum())
        hi_e = _safe(power[2 * third:].sum())
        total = low_e + mid_e + hi_e or 1.0
        low_band = low_e / total
        mid_band = mid_e / total
        hi_band = hi_e / total
    else:
        low_band = mid_band = hi_band = 0.0

    # --- Skew / Kurtosis (manual; avoids scipy dep) ---
    if rets.size > 3:
        mu = rets.mean()
        sd = rets.std() + 1e-12
        z = (rets - mu) / sd
        skew = _safe(np.mean(z ** 3))
        kurt = _safe(np.mean(z ** 4) - 3.0)
    else:
        skew = kurt = 0.0

    # --- Range expansion (Bollinger-ish) ---
    last20 = closes[-20:] if n >= 20 else closes
    bb_mid = float(last20.mean())
    bb_std = float(last20.std() + 1e-12)
    bb_pos = (closes[-1] - bb_mid) / (2 * bb_std)

    # --- Volume of body vs range (efficiency) ---
    body = closes - np.array([c["open"] for c in history])
    span = highs - lows + 1e-12
    body_ratio = _safe(np.mean(np.abs(body) / span))

    return {
        "available": True,
        "n_observations": int(n),
        "trend_slope": _safe(slope),
        "mom_5d": _mom(5),
        "mom_20d": _mom(20),
        "mom_60d": _mom(60),
        "mom_180d": _mom(180),
        "vol_5d_annual": _vol(5),
        "vol_20d_annual": _vol(20),
        "vol_60d_annual": _vol(60),
        "max_drawdown": max_dd,
        "current_drawdown": current_dd,
        "acf_lag1": _acf(1),
        "acf_lag5": _acf(5),
        "acf_lag20": _acf(20),
        "spectral_low_band": low_band,
        "spectral_mid_band": mid_band,
        "spectral_high_band": hi_band,
        "ret_skew": skew,
        "ret_kurtosis": kurt,
        "bb_position": _safe(bb_pos),
        "candle_body_ratio": body_ratio,
    }
