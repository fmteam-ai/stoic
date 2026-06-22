"""Shannon entropy noise filter — suppresses trades during chaotic markets.

Theory: when daily-return distribution looks like random coin-flips (high entropy),
trend-following and mean-reversion both fail because there's no signal — just noise.
We bucket the last N returns into K bins and compute Shannon entropy; values above
the threshold trigger a 'NOISE' veto so the bot waits for order to return.

H(X) = -Σ p(x) * log2(p(x)), normalised to [0, 1] by dividing by log2(K).
"""
import math
from typing import List


def shannon_entropy_returns(closes: List[float], window: int = 30, bins: int = 5) -> float:
    """Compute normalised Shannon entropy over the last `window` daily returns.

    Returns a float in [0.0, 1.0]:
      0.0 = perfectly deterministic move (all returns in one bin)
      1.0 = maximally noisy (uniform distribution across bins)
    """
    if not closes or len(closes) < window + 1:
        return 0.0
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1]
    ]
    if not rets:
        return 0.0
    lo, hi = min(rets), max(rets)
    if hi == lo:
        return 0.0  # zero variance = deterministic
    width = (hi - lo) / bins
    counts = [0] * bins
    for r in rets:
        idx = min(int((r - lo) / width), bins - 1)
        counts[idx] += 1
    total = sum(counts)
    h = 0.0
    for c in counts:
        if c == 0:
            continue
        p = c / total
        h -= p * math.log2(p)
    return h / math.log2(bins)  # normalise to [0, 1]


def classify_noise(closes: List[float], window: int = 30, bins: int = 5,
                   high_threshold: float = 0.90) -> dict:
    """Classify market as ORGANIZED / CHOPPY / NOISY based on entropy.

    Threshold tuned empirically: >0.90 normalised entropy = uniform random walk.
    Below 0.75 = clear directional bias. Between = caution.
    """
    h = shannon_entropy_returns(closes, window=window, bins=bins)
    if h >= high_threshold:
        light = "red"
        label = "NOISY"
    elif h >= 0.75:
        light = "yellow"
        label = "CHOPPY"
    else:
        light = "green"
        label = "ORGANIZED"
    return {
        "entropy": round(h, 4),
        "label": label,
        "traffic_light": light,
        "tradeable": light != "red",
        "threshold": high_threshold,
    }
