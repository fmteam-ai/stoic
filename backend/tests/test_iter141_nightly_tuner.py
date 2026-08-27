"""iter-141 · Nightly auto-tuning + allocator enforce — pure tests."""
from nightly_tuner import MAX_RUNS_PER_USER, combos_from_configs


def test_combos_only_tunable_engines_deduped():
    configs = [
        {"active_preset": "scalper", "symbols": ["XAUUSD", "XAUUSD-ECN"]},
        {"active_preset": "fast_scalp", "symbols": ["XAUUSD"]},
        {"active_preset": "fast_scalp", "symbols": ["XAUUSD"]},   # duplicate cfg
        {"active_preset": "breakout", "symbols": ["BTCUSD"]},
        {"active_preset": "sniper", "symbols": ["XAUUSD"]},        # MTF — not tunable
        {"active_preset": None, "symbols": ["XAUUSD"]},            # default MTF
    ]
    combos = combos_from_configs(configs)
    assert ("hf_scalp", "XAUUSD") in combos
    assert ("hf_scalp_fast", "XAUUSD") in combos
    assert ("breakout_m15", "BTCUSD") in combos
    # broker suffix normalized to base symbol — no duplicate combo
    assert combos.count(("hf_scalp", "XAUUSD")) == 1
    # MTF engines have no tunable params
    assert not any(e.startswith("mtf") for e, _ in combos)
    assert len(combos) == 3


def test_combos_empty_and_cap():
    assert combos_from_configs([]) == []
    assert combos_from_configs(None) == []
    many = [{"active_preset": "scalper", "symbols": [f"SYM{i}"]}
            for i in range(20)]
    assert len(combos_from_configs(many)[:MAX_RUNS_PER_USER]) == MAX_RUNS_PER_USER


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
