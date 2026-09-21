"""Post-deploy check for the OPT-IN forecast profile.

Run inside the backend/worker container:
    docker compose exec backend python ops/verify_forecast_profile.py

Proves, in order: the ML gate is ON for this container, torch + chronos
import, the Chronos-Bolt model loads (downloads once into HF_HOME), and a
real quantile forecast comes back on a synthetic series. Exit 0 = ready.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from ml_runtime import _memory_budget_gb, ml_runtime_enabled
    budget = _memory_budget_gb()
    print(f"[1/4] container memory budget: "
          f"{'unlimited' if math.isinf(budget) else f'{budget:.1f} GB'} · "
          f"ML_ENSEMBLE_ENABLED={os.environ.get('ML_ENSEMBLE_ENABLED')} · "
          f"FORECAST_AGENT_ENABLED={os.environ.get('FORECAST_AGENT_ENABLED', 'true')}")
    if not ml_runtime_enabled():
        print("FAIL: heavy-ML gate is OFF here (raise the memory limit or set "
              "ML_ENSEMBLE_ENABLED=true)")
        return 2
    if os.environ.get("FORECAST_AGENT_ENABLED", "true").lower() != "true":
        print("FAIL: FORECAST_AGENT_ENABLED is not true")
        return 2
    try:
        import torch
        import chronos  # noqa: F401
        print(f"[2/4] torch {torch.__version__} · chronos-forecasting "
              f"{getattr(chronos, '__version__', 'n/a')} · "
              f"threads={torch.get_num_threads()}")
    except ImportError as e:
        print(f"FAIL: forecast plane not installed in this image ({e}). "
              "Rebuild with docker-compose.forecast.yml (ML_FORECAST=1).")
        return 3
    import forecast_agent as fa
    t0 = time.time()
    pipe = fa._load_model()
    if pipe is None:
        print("FAIL: Chronos model failed to load — see backend logs")
        return 4
    print(f"[3/4] model {fa.MODEL_NAME} loaded in {time.time() - t0:.1f}s "
          f"(cache: {os.environ.get('HF_HOME', '~/.cache/huggingface')})")
    closes = [2000 + 5 * math.sin(i / 7) + i * 0.05 for i in range(300)]
    t0 = time.time()
    q = fa._forecast_sync(closes, fa.HORIZON_M15)
    if not q:
        print("FAIL: forecast returned nothing")
        return 5
    s = fa.summarize(closes, q, "synthetic", fa.HORIZON_M15)
    print(f"[4/4] forecast ok in {time.time() - t0:.2f}s · q10={s['q10']} "
          f"q50={s['q50']} q90={s['q90']} · band "
          f"{s['band_low_pct']}%..{s['band_high_pct']}%")
    print("READY: forecast profile is live for this container")
    return 0


if __name__ == "__main__":
    sys.exit(main())
