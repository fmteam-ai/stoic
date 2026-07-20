"""Scalp subsystem · Round 14 item 7 — uncertainty quantification.

A positive sample-mean expectancy with a negative lower confidence bound is
NOT established profitability. Block bootstrap preserves the short-range
autocorrelation of consecutive scalp outcomes that an iid bootstrap ignores.
"""
import random


def block_bootstrap_ci(values, block: int = 50, iters: int = 500,
                       alpha: float = 0.05, seed: int | None = None,
                       min_n: int = 20) -> list | None:
    """95% (default) CI for the mean of a time-ordered series via moving
    block bootstrap. Returns [lo, hi] or None below min_n samples."""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < min_n:
        return None
    rng = random.Random(seed)
    block = max(1, min(block, n))
    nblocks = max(1, (n + block - 1) // block)
    means = []
    for _ in range(iters):
        sample = []
        for _ in range(nblocks):
            s = rng.randrange(0, n - block + 1) if n > block else 0
            sample.extend(vals[s:s + block])
        sample = sample[:n]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(alpha / 2 * len(means))]
    hi = means[min(len(means) - 1, int((1 - alpha / 2) * len(means)))]
    return [round(lo, 3), round(hi, 3)]
