"""iter-53 · Velocity veto — Loss-Lab guardrail (DISARMED by default).

Replay against 30 recent live signals showed the suggested defaults would
have blocked 100% of entries (including a 60/60 winning day), so no regime
ships armed thresholds. The mechanism is fully functional and is armed
per-user via cfg["regime_overrides"]["LOW_VOL_TREND"].
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime_adapter import velocity_veto, REGIME_MODIFIERS  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARMED = {"regime_overrides": {"LOW_VOL_TREND": {
    "velocity_counter_max": 3.0, "velocity_veto_threshold": 5.0}}}


def sig(action="SELL", kv=7.9, regime="LOW_VOL_TREND", htf="DOWN"):
    return {
        "action": action,
        "regime": {"regime": regime},
        "indicators": {"kalman_filter": {"k_velocity": kv}},
        "mtf_gate": {"htf_trend": htf, "checked": True},
    }


class TestDisarmedByDefault:
    def test_no_regime_ships_armed_thresholds(self):
        for name, mods in REGIME_MODIFIERS.items():
            assert "velocity_counter_max" not in mods, name
            assert "velocity_veto_threshold" not in mods, name

    def test_default_config_never_vetoes(self):
        # The exact pattern that would fire when armed
        assert velocity_veto(sig("SELL", 7.9)) is None
        assert velocity_veto(sig("BUY", -7.9)) is None


class TestArmedCounterMomentum:
    def test_sell_blocked_when_velocity_up(self):
        r = velocity_veto(sig("SELL", 7.9), ARMED)
        assert r and "SELL blocked" in r and "+7.9" in r

    def test_sell_allowed_below_threshold(self):
        assert velocity_veto(sig("SELL", 2.5), ARMED) is None

    def test_buy_blocked_when_velocity_down(self):
        r = velocity_veto(sig("BUY", -4.2), ARMED)
        assert r and "BUY blocked" in r

    def test_buy_allowed_with_up_velocity(self):
        assert velocity_veto(sig("BUY", 7.9, htf="UP"), ARMED) is None


class TestArmedMtfConflict:
    def test_strong_velocity_contradicting_htf(self):
        r = velocity_veto(sig("SELL", -6.0, htf="UP"), ARMED)
        assert r and "contradicts the MTF bias" in r

    def test_strong_velocity_aligned_with_htf_ok(self):
        assert velocity_veto(sig("SELL", -6.0, htf="DOWN"), ARMED) is None

    def test_flat_htf_no_conflict(self):
        assert velocity_veto(sig("SELL", -6.0, htf="FLAT"), ARMED) is None


class TestScope:
    def test_other_regimes_unaffected(self):
        assert velocity_veto(sig("SELL", 9.9, regime="HIGH_VOL_TREND"), ARMED) is None

    def test_hold_ignored(self):
        assert velocity_veto(sig("HOLD", 9.9), ARMED) is None

    def test_missing_velocity_allows(self):
        s = sig("SELL")
        s["indicators"]["kalman_filter"] = {}
        assert velocity_veto(s, ARMED) is None


class TestWiring:
    def test_bot_runner_calls_veto(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "velocity_veto(signal, cfg)" in src
        assert '"velocity_veto"' in src  # intel counter
