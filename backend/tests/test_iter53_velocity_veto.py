"""iter-53 · Velocity veto — Loss-Lab guardrail.

In LOW_VOL_TREND, counter-momentum entries (e.g. GOLD SELL while Kalman
velocity > +3) clustered in the losing set. Two-tier veto:
  1. counter-momentum beyond velocity_counter_max (default 3.0)
  2. |velocity| > velocity_veto_threshold (5.0) contradicting the MTF bias
Overridable per-user via cfg["regime_overrides"]["LOW_VOL_TREND"].
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime_adapter import velocity_veto, REGIME_MODIFIERS  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sig(action="SELL", kv=7.9, regime="LOW_VOL_TREND", htf="DOWN"):
    return {
        "action": action,
        "regime": {"regime": regime},
        "indicators": {"kalman_filter": {"k_velocity": kv}},
        "mtf_gate": {"htf_trend": htf, "checked": True},
    }


class TestCounterMomentum:
    def test_sell_blocked_when_velocity_up(self):
        # The exact Loss-Lab pattern: SELL with kv +7.9 in LOW_VOL_TREND
        r = velocity_veto(sig("SELL", 7.9))
        assert r and "SELL blocked" in r and "+7.9" in r

    def test_sell_allowed_below_threshold(self):
        assert velocity_veto(sig("SELL", 2.5)) is None

    def test_buy_blocked_when_velocity_down(self):
        r = velocity_veto(sig("BUY", -4.2))
        assert r and "BUY blocked" in r

    def test_buy_allowed_with_up_velocity(self):
        assert velocity_veto(sig("BUY", 7.9, htf="UP")) is None


class TestMtfConflict:
    def test_strong_velocity_contradicting_htf(self):
        # BUY, kv -6 → passes rule 1? No: BUY with kv -6 < -3 hits rule 1.
        # Use SELL with kv -6 (aligned with short, passes rule 1) but HTF UP:
        r = velocity_veto(sig("SELL", -6.0, htf="UP"))
        assert r and "contradicts the MTF bias" in r

    def test_strong_velocity_aligned_with_htf_ok(self):
        assert velocity_veto(sig("SELL", -6.0, htf="DOWN")) is None

    def test_flat_htf_no_conflict(self):
        assert velocity_veto(sig("SELL", -6.0, htf="FLAT")) is None


class TestScope:
    def test_other_regimes_unaffected(self):
        assert velocity_veto(sig("SELL", 9.9, regime="HIGH_VOL_TREND")) is None

    def test_hold_ignored(self):
        assert velocity_veto(sig("HOLD", 9.9)) is None

    def test_missing_velocity_allows(self):
        s = sig("SELL")
        s["indicators"]["kalman_filter"] = {}
        assert velocity_veto(s) is None

    def test_user_override_loosens(self):
        cfg = {"regime_overrides": {"LOW_VOL_TREND": {"velocity_counter_max": 10.0,
                                                      "velocity_veto_threshold": 12.0}}}
        assert velocity_veto(sig("SELL", 7.9), cfg) is None

    def test_user_override_tightens(self):
        cfg = {"regime_overrides": {"LOW_VOL_TREND": {"velocity_counter_max": 1.0}}}
        assert velocity_veto(sig("SELL", 2.0), cfg) is not None

    def test_defaults_on_regime_profile(self):
        lv = REGIME_MODIFIERS["LOW_VOL_TREND"]
        assert lv["velocity_counter_max"] == 3.0
        assert lv["velocity_veto_threshold"] == 5.0


class TestWiring:
    def test_bot_runner_calls_veto(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "velocity_veto(signal, cfg)" in src
        assert '"velocity_veto"' in src  # intel counter
