"""iter-134 · Quant roadmap phase-2: decision ledger, cost-aware EV,
and property/invariant tests (#2, #3, #4, #6, #14).
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monte_carlo import mc_gate, typical_cost  # noqa: E402
from risk import compute_lot_for_account, get_profile  # noqa: E402
from trade_decisions import _snapshot, infer_stage  # noqa: E402
from versioning import version_stamp  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
random.seed(1337)


class TestSizingInvariants:
    """Property tests over randomized inputs (quant roadmap #14)."""

    def _size(self, equity, entry, sl_dist, conf, profile_key, kelly):
        return compute_lot_for_account(
            account={"equity": equity, "account_type": "standard"},
            symbol="XAUUSD", entry_price=entry,
            stop_loss=entry - sl_dist, confidence_pct=conf,
            profile=get_profile(profile_key), kelly_enabled=kelly)

    def test_lot_never_negative_or_zero(self):
        # C5 fail-closed contract: valid sizing → lot ≥ 0.01; invalid sizing
        # (broker-minimum overshoots the risk budget) → lot 0 + reject reason.
        for _ in range(200):
            r = self._size(random.uniform(100, 1e6), random.uniform(500, 5000),
                           random.uniform(0.5, 100), random.uniform(0, 100),
                           random.choice(["low", "medium", "high", "extreme"]),
                           random.random() < 0.5)
            if r.get("sizing_valid", True):
                assert r["lot_size"] >= 0.01
            else:
                assert r["lot_size"] == 0.0
                assert r.get("reject_reason")

    def test_wider_stop_never_increases_size(self):
        for _ in range(100):
            equity = random.uniform(1000, 100000)
            entry = random.uniform(1000, 5000)
            conf = random.uniform(50, 90)
            pk = random.choice(["low", "medium", "high"])
            kelly = random.random() < 0.5
            d1 = random.uniform(1, 40)
            d2 = d1 * random.uniform(1.1, 4.0)
            r1 = self._size(equity, entry, d1, conf, pk, kelly)
            r2 = self._size(equity, entry, d2, conf, pk, kelly)
            assert r2["lot_size"] <= r1["lot_size"] + 1e-9, (d1, d2, r1, r2)

    def test_risk_never_exceeds_profile_cap(self):
        from risk import RISK_OVERSHOOT_TOLERANCE
        for _ in range(200):
            pk = random.choice(["low", "medium", "high", "extreme"])
            p = get_profile(pk)
            r = self._size(random.uniform(1000, 1e6),
                           random.uniform(1000, 5000),
                           random.uniform(1, 50), random.uniform(0, 100), pk,
                           random.random() < 0.5)
            if not r.get("sizing_valid", True):
                continue  # C5: rejected sizing never reaches the broker
            assert r["effective_risk_pct"] <= p["risk_pct"] + 1e-9
            # C5: post-rounding ACTUAL risk stays within budget × tolerance
            assert (r["actual_risk_usd"]
                    <= r["risk_amount_usd"] * RISK_OVERSHOOT_TOLERANCE + 1e-6)

    def test_kelly_never_sizes_larger_than_fixed_fraction(self):
        for _ in range(100):
            equity = random.uniform(1000, 100000)
            entry = random.uniform(1000, 5000)
            d = random.uniform(1, 50)
            conf = random.uniform(0, 100)
            pk = random.choice(["low", "medium", "high"])
            rk = self._size(equity, entry, d, conf, pk, True)
            rf = self._size(equity, entry, d, conf, pk, False)
            assert rk["lot_size"] <= rf["lot_size"] + 1e-9


class TestCostAwareEV:
    def test_gate_uses_net_ev(self):
        # gross EV barely positive, costs push it clearly negative → veto
        mc = {"paths": 10000, "p_tp_first": 0.44, "p_sl_first": 0.56,
              "rr": 1.25, "ev_r": 0.02, "cost_r": 0.07, "ev_r_net": -0.05}
        msg = mc_gate(mc)
        assert msg and "costs" in msg and "-0.05R net" in msg.replace("−", "-")

    def test_marginal_negative_within_tolerance_passes(self):
        # iter-141 · cost model is an estimate: −0.015R is inside the
        # −0.02R tolerance band → no veto
        mc = {"paths": 10000, "p_tp_first": 0.46, "p_sl_first": 0.54,
              "rr": 1.25, "ev_r": 0.035, "cost_r": 0.05, "ev_r_net": -0.015}
        assert mc_gate(mc) is None

    def test_just_beyond_tolerance_vetoes(self):
        mc = {"paths": 10000, "p_tp_first": 0.46, "p_sl_first": 0.54,
              "rr": 1.25, "ev_r": 0.03, "cost_r": 0.055, "ev_r_net": -0.025}
        assert mc_gate(mc) is not None

    def test_gate_passes_when_net_positive(self):
        mc = {"paths": 10000, "p_tp_first": 0.55, "p_sl_first": 0.45,
              "rr": 1.5, "ev_r": 0.375, "cost_r": 0.05, "ev_r_net": 0.325}
        assert mc_gate(mc) is None

    def test_legacy_dict_without_net_still_works(self):
        assert mc_gate({"paths": 1, "p_tp_first": .4, "p_sl_first": .6,
                        "rr": 1.0, "ev_r": -0.2}) is not None

    def test_typical_cost_symbol_aware(self):
        assert typical_cost("XAUUSD", 4000) == 0.35 * 1.5
        assert typical_cost("BTCUSD", 63000) == 30.0 * 1.5
        assert abs(typical_cost("UNKNOWN", 2000) - 0.3) < 1e-9  # 0.01% × 1.5

    def test_simulate_reports_net(self):
        from monte_carlo import simulate_trade
        bars = [{"c": 4000 + i % 7 - 3, "h": 4004 + i % 7 - 3,
                 "l": 3996 + i % 7 - 3} for i in range(120)]
        mc = simulate_trade("BUY", 4000, 3990, 4015, bars, n_paths=500,
                            seed=7, cost_price=0.5)
        assert mc["cost_r"] == 0.05
        assert abs(mc["ev_r_net"] - (mc["ev_r"] - 0.05)) < 1e-9


class TestDecisionLedger:
    def test_snapshot_extracts_key_fields(self):
        sig = {"action": "BUY", "entry_price": 4000, "stop_loss": 3990,
               "confidence": 70, "scope": "hf_scalp",
               "monte_carlo": {"ev_r": 0.2, "ev_r_net": 0.15, "cost_r": 0.05,
                               "p_tp_first": 0.5, "p_sl_first": 0.4, "rr": 1.5},
               "news_ai": {"net": -1.2}, "secret_internal": "x"}
        snap = _snapshot(sig)
        assert snap["action"] == "BUY" and snap["mc"]["ev_r_net"] == 0.15
        assert snap["news_net"] == -1.2
        assert "secret_internal" not in snap

    def test_stage_inference(self):
        assert infer_stage("Session-trend gate: 2% day...") == "session_trend_gate"
        assert infer_stage("Monte Carlo gate: 10,000 paths") == "monte_carlo_gate"
        assert infer_stage("Exhaustion gate: spent move") == "exhaustion_chase_gate"
        assert infer_stage("Safety Guardian blocked execution") == "safety_guardian"
        assert infer_stage("something novel") == "gate"

    def test_version_stamp_complete(self):
        v = version_stamp("hf_scalp")
        for k in ("strategy_version", "risk_policy", "execution_policy",
                  "feature_schema"):
            assert v.get(k)
        assert v["strategy_version"] == "hf_scalp_v132"

    def test_rejections_wired_into_pulse(self):
        """A rejected decision never reaches execution — rejections are
        recorded at the pulse layer (before `continue`), executions only
        after engine.execute() succeeds."""
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert 'action in ("SKIP", "BLOCKED")' in src
        assert 'status="rejected"' in src
        assert 'status="executed"' in src
        # executed record only after the blocked-check `continue`
        assert src.index('trade_doc.get("blocked")') < src.index('status="executed"')

    def test_trades_stamp_versions(self):
        src = open(os.path.join(BACKEND, "execution.py")).read()
        assert '"versions": signal.get("versions")' in src
        src2 = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "version_stamp(signal.get" in src2
