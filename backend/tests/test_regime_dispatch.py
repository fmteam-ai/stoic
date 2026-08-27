"""Regime-adaptive dispatch — unit tests.

The ADAPTIVE engine routes each evaluation to the engine matching the live
regime; CHOP and unknown regimes stand down. The Phase-3 auto-preset table
must map every regime label AND every regime_adapter execution mode.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adaptive_mode import REGIME_TO_PRESET, pick_preset_for_regime
from regime_adapter import REGIME_MODIFIERS
from strategy_engines import (ENGINE_BY_PRESET, ENGINE_LABELS,
                              MTF_MODE_BY_ENGINE, dispatch_engine_for_regime,
                              resolve_engine)
from strategy_presets import PRESETS, list_presets


class TestRegimeDispatch:
    def test_trending_regimes_dispatch_to_mtf_cascades(self):
        eng, note = dispatch_engine_for_regime("HIGH_VOL_TREND")
        assert eng == "mtf_relaxed" and "momentum" in note
        eng, note = dispatch_engine_for_regime("LOW_VOL_TREND")
        assert eng == "mtf_moderate"
        # dispatched cascades resolve to a real MTF strictness mode
        assert MTF_MODE_BY_ENGINE["mtf_relaxed"] == "relaxed"
        assert MTF_MODE_BY_ENGINE["mtf_moderate"] == "moderate"

    def test_range_regime_dispatches_to_range_fade(self):
        eng, note = dispatch_engine_for_regime("RANGE")
        assert eng == "range_fade" and "fading" in note

    def test_chop_and_unknown_stand_down(self):
        eng, note = dispatch_engine_for_regime("CHOP")
        assert eng is None and "standing down" in note
        for label in (None, "", "unknown", "SOMETHING_NEW"):
            eng, note = dispatch_engine_for_regime(label)
            assert eng is None, label
            assert "standing down" in note

    def test_adaptive_preset_resolves_to_regime_adaptive_engine(self):
        assert resolve_engine("adaptive") == "regime_adaptive"
        assert ENGINE_BY_PRESET["adaptive"] == "regime_adaptive"
        assert "regime_adaptive" in ENGINE_LABELS
        # dispatch target engines all have labels too
        for eng in ("mtf_relaxed", "mtf_moderate", "range_fade"):
            assert eng in ENGINE_LABELS

    def test_adaptive_preset_exposed_to_ui(self):
        assert "adaptive" in PRESETS
        cfg = PRESETS["adaptive"]["config"]
        assert cfg["trade_of_day_cap"] >= 1
        assert cfg["anti_tilt_enabled"] is True
        keys = [p["key"] for p in list_presets()]
        assert "adaptive" in keys


class TestAutoPresetRegimeTable:
    def test_all_classify_regime_labels_mapped(self):
        """Every label classify_regime can emit maps to a real preset —
        HIGH_VOL_TREND/RANGE previously fell through to 'balanced'."""
        assert REGIME_TO_PRESET["HIGH_VOL_TREND"] == "trend_rider"
        assert REGIME_TO_PRESET["LOW_VOL_TREND"] == "balanced"
        assert REGIME_TO_PRESET["RANGE"] == "mean_reversion"

    def test_all_execution_modes_mapped_or_blocked(self):
        """Every regime_adapter execution mode is either mapped or is HALT
        (CHOP is blocked upstream via min_confidence_delta +99)."""
        for label, mods in REGIME_MODIFIERS.items():
            mode = mods["mode"]
            if mode == "HALT":
                assert mods["min_confidence_delta"] >= 99
                continue
            assert mode in REGIME_TO_PRESET, mode

    def test_pick_preset_prefers_execution_mode(self):
        pick = pick_preset_for_regime("DYNAMIC_MOMENTUM", "HIGH_VOL_TREND")
        assert pick["preset_key"] == "trend_rider"
        pick = pick_preset_for_regime(None, "RANGE")
        assert pick["preset_key"] == "mean_reversion"
        pick = pick_preset_for_regime(None, None)
        assert pick["preset_key"] == "balanced"

    def test_mapped_presets_all_exist(self):
        for preset in REGIME_TO_PRESET.values():
            assert preset in PRESETS, preset


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
