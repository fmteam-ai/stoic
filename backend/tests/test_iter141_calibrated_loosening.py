"""iter-141 · Calibrated loosening — volatility-adaptive exhaustion gate +
Monte Carlo EV tolerance band."""
import time

from intraday_features import compute_intraday_features
from payoff_guard import exhaustion_chase_gate
from monte_carlo import mc_gate, MC_EV_TOLERANCE_R


def _feats(rng, pos, typical=None):
    f = {"day_range_pct": rng, "range_pos_pct": pos}
    if typical is not None:
        f["typical_day_range_pct"] = typical
    return f


class TestAdaptiveExhaustion:
    def test_fixed_threshold_without_history(self):
        # no typical → legacy behaviour: 1.8% day at the high is vetoed
        assert exhaustion_chase_gate("BUY", _feats(1.8, 92.0), "XAUUSD")

    def test_high_vol_regime_raises_threshold(self):
        # typical day = 2.1% → arms at 2.73%; a 2.2% day is NOT "spent"
        assert exhaustion_chase_gate("BUY", _feats(2.2, 92.0, typical=2.1), "XAUUSD") is None

    def test_truly_outsized_day_still_vetoed(self):
        # 3.0% day vs 2.1% typical (1.43×) at the extreme → veto, with context
        msg = exhaustion_chase_gate("BUY", _feats(3.0, 92.0, typical=2.1), "XAUUSD")
        assert msg and "adaptive" in msg and "typical 2.10%" in msg

    def test_calm_regime_keeps_fixed_floor(self):
        # typical 0.8% → 1.3×0.8=1.04 < 1.5 floor → still arms at 1.5%
        assert exhaustion_chase_gate("SELL", _feats(1.6, 5.0, typical=0.8), "XAUUSD")
        assert exhaustion_chase_gate("SELL", _feats(1.4, 5.0, typical=0.8), "XAUUSD") is None

    def test_mid_range_never_vetoed(self):
        assert exhaustion_chase_gate("BUY", _feats(4.0, 50.0, typical=2.0), "XAUUSD") is None


class TestMcTolerance:
    def test_tolerance_constant(self):
        assert MC_EV_TOLERANCE_R == 0.02

    def test_zero_net_ev_passes(self):
        mc = {"paths": 10000, "p_tp_first": 0.5, "p_sl_first": 0.5,
              "rr": 1.0, "ev_r": 0.05, "cost_r": 0.05, "ev_r_net": 0.0}
        assert mc_gate(mc) is None

    def test_counter_trend_rule_unaffected(self):
        # fading a 2σ+ trend still needs ≥ +0.10R even inside the tolerance
        mc = {"paths": 10000, "p_tp_first": 0.5, "p_sl_first": 0.5,
              "rr": 1.0, "ev_r": 0.04, "cost_r": 0.05, "ev_r_net": -0.01,
              "trend_aligned": False, "drift_sig": 2.5, "trend": "up"}
        msg = mc_gate(mc)
        assert msg and "Counter-trend" in msg


class TestTypicalDayRange:
    def _day_bars(self, day_offset, lo, hi, base_ts, n=16):
        t0 = base_ts + day_offset * 86400
        out = []
        for i in range(n):
            out.append({"t": t0 + i * 900, "o": lo, "h": hi, "l": lo,
                        "c": (lo + hi) / 2, "v": 1})
        return out

    def test_median_of_complete_prior_days(self):
        now = int(time.time())
        today0 = now - (now % 86400)
        bars = []
        # 4 prior days with ranges 1%, 2%, 3%, 4% around 4000 → median 2.5
        for k, r in enumerate((0.01, 0.02, 0.03, 0.04)):
            lo = 4000.0
            bars += self._day_bars(-(4 - k), lo, lo * (1 + r), today0 + 3600)
        bars += self._day_bars(0, 4000.0, 4020.0, today0 + 3600)  # today
        f = compute_intraday_features(bars)
        assert f is not None
        assert abs(f["typical_day_range_pct"] - 2.5) < 0.01

    def test_none_when_fewer_than_three_days(self):
        now = int(time.time())
        today0 = now - (now % 86400)
        bars = (self._day_bars(-2, 4000, 4040, today0 + 3600)
                + self._day_bars(-1, 4000, 4080, today0 + 3600)
                + self._day_bars(0, 4000, 4020, today0 + 3600))
        f = compute_intraday_features(bars)
        assert f is not None
        assert f["typical_day_range_pct"] is None
