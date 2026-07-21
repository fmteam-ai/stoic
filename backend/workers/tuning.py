"""Tuning worker — AI optimizer sweeps + nightly quant tuner."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import _nightly_tuning_loop, _optimizer_loop
    main("tuning", [_optimizer_loop, _nightly_tuning_loop])
