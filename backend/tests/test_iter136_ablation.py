"""iter-136 · Gate ablation counterfactual replay tests (roadmap #16)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ablation import replay_outcome  # noqa: E402


def bars(seq):
    return [{"t": 1000 + i * 900, "o": c, "h": c + 2, "l": c - 2, "c": c}
            for i, c in enumerate(seq)]


class TestReplayOutcome:
    def test_vetoed_buy_would_have_lost(self):
        # entry 4000, SL 3990 — price slides to 3985 → SL first, veto saved 1R
        oc = replay_outcome("BUY", 4000, 3990, 4015, bars([3998, 3994, 3989]))
        assert oc["outcome"] == "sl_first" and oc["r"] == -1.0

    def test_vetoed_buy_would_have_won(self):
        oc = replay_outcome("BUY", 4000, 3990, 4015, bars([4004, 4010, 4016]))
        assert oc["outcome"] == "tp_first" and oc["r"] == 1.5

    def test_sell_direction(self):
        oc = replay_outcome("SELL", 4000, 4010, 3985, bars([3996, 3990, 3984]))
        assert oc["outcome"] == "tp_first" and oc["r"] == 1.5
        oc2 = replay_outcome("SELL", 4000, 4010, 3985, bars([4004, 4009]))
        assert oc2["outcome"] == "sl_first"

    def test_both_touched_conservative_sl_first(self):
        wide = [{"t": 1000, "o": 4000, "h": 4020, "l": 3985, "c": 4000}]
        oc = replay_outcome("BUY", 4000, 3990, 4015, wide)
        assert oc["outcome"] == "sl_first"

    def test_timeout_reports_drift_r(self):
        oc = replay_outcome("BUY", 4000, 3990, 4030, bars([4002, 4004, 4005]))
        assert oc["outcome"] == "timeout" and oc["r"] == 0.5

    def test_degenerate_inputs(self):
        assert replay_outcome("BUY", 4000, 4000, 4015, bars([4001])) is None
        assert replay_outcome("BUY", 4000, 3990, 4015, []) is None
