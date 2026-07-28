"""iter-173 · Shared OOM guard for heavy ML (GBM zoo, XGBoost, torch/Chronos).

The full ML stack needs well over 1GB resident; loading or training it inside
a small production container OOM-kills the API (observed: 1Gi prod pod
crash-looping behind Cloudflare 520s). Every heavy-ML entry point must call
ml_runtime_enabled() before importing torch / xgboost / lightgbm / catboost /
sklearn. Explicit ML_ENSEMBLE_ENABLED wins; otherwise auto-disable when the
container memory budget is below ML_MIN_MEMORY_GB (default 2).
"""
import os


def _memory_budget_gb() -> float:
    """Container memory limit from cgroups (v2 then v1); inf when unlimited."""
    for p in ("/sys/fs/cgroup/memory.max",
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
        except OSError:
            continue
        if v.isdigit():
            b = int(v)
            if b < 1 << 48:  # cgroup "unlimited" sentinel
                return b / (1 << 30)
    return float("inf")


def ml_runtime_enabled() -> bool:
    v = os.environ.get("ML_ENSEMBLE_ENABLED")
    if v is not None:
        return v.lower() == "true"
    return _memory_budget_gb() >= float(os.environ.get("ML_MIN_MEMORY_GB", "2"))
