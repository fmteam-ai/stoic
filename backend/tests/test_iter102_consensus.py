from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-63 · Master Agent consensus — weighted multi-agent vote."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from consensus import compute_consensus, WEIGHTS, DEFAULT_THRESHOLD  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url
BASE_URL = require_live_base_url()


def bullish_ctx(action="BUY"):
    return {
        "action": action,
        "confidence": 78,
        "mtf_tiers": {"SHORT": {"direction": "UP"}, "MEDIUM": {"direction": "UP"},
                      "LONG": {"direction": "UP"}},
        "market_structure": {"ready": True, "bias": "BULLISH",
                             "acc_dist": {"phase": "ACCUMULATION"}},
        "forecast": {"last": 100.0, "q10": 100.2, "q50": 101.0, "q90": 102.0},
        "rl_policy": {"decision": "ALLOW", "mean": 30.0, "n": 12},
        "fed_tone": {"score": -0.5},           # dovish = gold-bullish
        "intraday_momentum": {"change_pct": 0.6},
        "liquidity": {"ready": True, "draw": "UP", "active_zone": "DEMAND",
                      "cum_delta": {"bias": "BULLISH"},
                      "dom": {"live": True, "imbalance": 0.4}},
        "news_ai": {"net": 1.5},
    }


class TestConsensus:
    def test_weights_sum_to_one(self):
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_fully_aligned_buy_scores_high(self):
        c = compute_consensus(bullish_ctx("BUY"))
        assert c["score"] >= 85 and c["verdict"] == "STRONG"
        assert all(v > 0 for v in c["votes"].values())

    def test_same_context_sell_scores_low(self):
        c = compute_consensus(bullish_ctx("SELL"))
        assert c["score"] <= 30 and c["verdict"] == "CONFLICTED"

    def test_neutral_context_near_50(self):
        c = compute_consensus({"action": "BUY", "confidence": 60})
        assert 45 <= c["score"] <= 55

    def test_rl_block_drags_quant_down(self):
        ctx = bullish_ctx("BUY")
        ctx["rl_policy"] = {"decision": "BLOCK", "mean": -80, "n": 20}
        c = compute_consensus(ctx)
        assert c["votes"]["quant"] < compute_consensus(bullish_ctx())["votes"]["quant"]

    def test_forecast_band_stronger_than_median(self):
        ctx = bullish_ctx("BUY")
        ctx["forecast"] = {"last": 100.0, "q10": 99.0, "q50": 100.5, "q90": 102.0}
        mixed = compute_consensus(ctx)["votes"]["forecast"]
        assert mixed == 0.5  # median-only support
        assert compute_consensus(bullish_ctx())["votes"]["forecast"] == 1.0

    def test_hawkish_fed_hurts_gold_buy(self):
        ctx = bullish_ctx("BUY")
        ctx["fed_tone"] = {"score": 0.8}
        assert compute_consensus(ctx)["votes"]["macro"] < \
            compute_consensus(bullish_ctx())["votes"]["macro"]

    def test_score_bounds(self):
        c = compute_consensus(bullish_ctx("BUY"))
        assert 0 <= c["score"] <= 100


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200
    return s


class TestHttp:
    def test_posture_includes_consensus(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=90)
        assert r.status_code == 200
        sym = list(r.json()["symbols"].values())[0]
        cons = sym.get("consensus")
        assert cons and "BUY" in cons and "SELL" in cons
        assert 0 <= cons["BUY"]["score"] <= 100


class TestWiring:
    def test_bot_runner_consensus_gate(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "compute_consensus" in src and '"consensus_block"' in src

    def test_config_defaults(self):
        src = open(os.path.join(BACKEND, "models.py")).read()
        assert 'consensus_gate_mode: str = "enforce"' in src
        assert "consensus_threshold: int = Field(default=55" in src
        assert DEFAULT_THRESHOLD == 55


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
