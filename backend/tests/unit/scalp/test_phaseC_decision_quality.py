"""Phase C — AI decision quality: regime classifier (with H1 MTF confirm),
meta strategy selector, and the ONE combined verdict. MongoDB-free."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import inspect

import pytest

from scalp import decision_quality as dq
from scalp import regime
from scalp import strategy_select as sel

pytestmark = pytest.mark.unit
PIP = 0.0001


def _bars(closes):
    return [{"c": c} for c in closes]


class TestRegimeClassifier:
    def test_clean_uptrend_with_h1_agreement(self):
        closes = [1.0800 + i * 0.0004 for i in range(24)]   # straight up
        out = regime.classify(_bars(closes), PIP)
        assert out["regime"] == "TREND_UP"
        assert out["h1_agrees"] is True
        assert out["confidence"] > 0.5
        assert out["efficiency_ratio"] > 0.8

    def test_clean_downtrend(self):
        closes = [1.0900 - i * 0.0004 for i in range(24)]
        out = regime.classify(_bars(closes), PIP)
        assert out["regime"] == "TREND_DOWN"

    def test_chop_is_range_even_with_drift(self):
        # zig-zag with slight drift: low efficiency ratio → RANGE
        closes = [1.0850 + (0.0008 if i % 2 else -0.0006) for i in range(24)]
        out = regime.classify(_bars(closes), PIP)
        assert out["regime"] == "RANGE"
        assert out["efficiency_ratio"] < regime.ER_TREND_FLOOR

    def test_shock_detection(self):
        closes = [1.0850] * 16 + [1.0850, 1.0990, 1.0860, 1.0980,
                                  1.0855, 1.0975, 1.0850, 1.0970]
        out = regime.classify(_bars(closes), PIP)
        assert out["regime"] == "VOLATILITY_SHOCK"

    def test_insufficient_bars_unknown(self):
        out = regime.classify(_bars([1.08] * 5), PIP)
        assert out["regime"] == "UNKNOWN"
        assert out["confidence"] == 0.0

    def test_permissions_wired_to_classifier(self):
        src = open(_os.path.join(_BACKEND_DIR, "scalp/permissions.py")).read()
        assert "regime_mod.classify(" in src
        assert "regime_detail" in src


class TestStrategySelector:
    def _rows(self, preset, regime_name, pips, n):
        return [{"preset": preset, "regime": regime_name, "net_pips": pips}
                for _ in range(n)]

    def test_better_preset_wins_with_samples(self):
        rows = (self._rows("pullback_strict", "TRENDING_UP", 0.8, 40)
                + self._rows("pullback_loose", "TRENDING_UP", -0.5, 40))
        out = sel.score_presets(rows)
        assert out["TRENDING_UP"]["preset"] == "pullback_strict"

    def test_low_sample_presets_keep_default(self):
        rows = self._rows("pullback_loose", "TRENDING_UP", 5.0, 3)  # n < 10
        out = sel.score_presets(rows)
        assert out["TRENDING_UP"]["preset"] == sel.DEFAULT_PRESET

    def test_recency_weighting_prefers_recent_form(self):
        # strict was great long ago, loose is good NOW (recent = end of list)
        rows = (self._rows("pullback_strict", "TRENDING_UP", 1.0, 30)
                + self._rows("pullback_strict", "TRENDING_UP", -1.2, 30)
                + self._rows("pullback_loose", "TRENDING_UP", 0.9, 30))
        out = sel.score_presets(rows)
        t = out["TRENDING_UP"]["table"]
        assert t["pullback_loose"]["weighted_mean"] > \
            t["pullback_strict"]["weighted_mean"]

    def test_unknown_presets_ignored(self):
        out = sel.score_presets(self._rows("deleted_preset", "FLAT", 9.9, 50))
        assert "FLAT" not in out or "deleted_preset" not in \
            out.get("FLAT", {}).get("table", {})

    def test_cold_cache_returns_safe_default(self):
        got = sel.get_cached("NEVER_SEEN", "TRENDING_UP")
        assert got["preset"] == sel.DEFAULT_PRESET
        assert got["source"] == "default"

    def test_presets_only_tune_selectivity(self):
        # safety: presets may only touch the documented detector knobs
        allowed = {"impulse_min_pips", "impulse_vol_mult",
                   "pullback_min_frac", "pullback_max_frac",
                   "resume_min_pips"}
        for name, params in sel.PRESETS.items():
            assert set(params) <= allowed, name


class TestCombinedVerdict:
    BASE = dict(p_win=0.62, model_source="model", ev_pips=1.2, cost_pips=0.8,
                exec_score=80, regime="TRENDING_UP", regime_confidence=0.8,
                direction_aligned=True, h1_agrees=True,
                risk_headroom_frac=1.0, loss_streak=0)

    def test_strong_setup_scores_strong(self):
        out = dq.combine(**self.BASE)
        assert out["verdict"] == "STRONG"
        assert out["score"] >= dq.STRONG
        assert out["live_allowed"] is True
        assert set(out["pillars"]) == set(dq.WEIGHTS)

    def test_negative_ev_zeroes_the_ev_pillar(self):
        out = dq.combine(**{**self.BASE, "ev_pips": -0.5})
        assert out["pillars"]["ev"] == 0.0

    def test_misaligned_regime_zeroes_regime_pillar(self):
        out = dq.combine(**{**self.BASE, "direction_aligned": False})
        assert out["pillars"]["regime"] == 0.0

    def test_loss_streak_halves_risk_pillar(self):
        out = dq.combine(**{**self.BASE, "loss_streak": 2})
        assert out["pillars"]["risk"] == 0.5

    def test_heuristic_fallback_discounts_prediction(self):
        m = dq.combine(**self.BASE)["pillars"]["prediction"]
        h = dq.combine(**{**self.BASE,
                          "model_source": "heuristic"})["pillars"]["prediction"]
        assert h == pytest.approx(m * 0.7, abs=1e-3)

    def test_everything_weak_is_blocked(self):
        out = dq.combine(p_win=0.46, model_source="heuristic", ev_pips=0.05,
                         cost_pips=1.5, exec_score=40, regime="FLAT",
                         regime_confidence=0.2, direction_aligned=True,
                         h1_agrees=False, risk_headroom_frac=0.2,
                         loss_streak=3)
        assert out["verdict"] == "BLOCKED"
        assert out["live_allowed"] is False
        assert len(out["weakest"]) == 2

    def test_weights_sum_to_one(self):
        assert sum(dq.WEIGHTS.values()) == pytest.approx(1.0)


class TestPhaseCWiring:
    def _src(self):
        from scalp import engine
        return inspect.getsource(engine)

    def test_engine_uses_selector_and_verdict(self):
        src = self._src()
        assert "strategy_select.get_cached(" in src
        assert "strategy_select.maybe_refresh(" in src
        assert 'params=_sel["params"]' in src
        assert '"setup_preset": cand.get("preset")' in src
        assert "decision_quality.combine(" in src
        assert '"combined_decision_quality"' in src
        assert 'if not dq["live_allowed"]:' in src

    def test_verdict_gate_runs_after_hard_risk_gates(self):
        src = self._src()
        assert src.index('"pre_submit_resize"') < \
            src.index("decision_quality.combine(")
