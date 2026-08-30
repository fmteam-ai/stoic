"""iter-65 · Bayesian decision model — Beta-Binomial P(success) + R-multiples."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bayes_decision import (  # noqa: E402
    build_model, decision, trade_r_multiple, MIN_EVIDENCE, PRIOR_A, PRIOR_B,
)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url
BASE_URL = require_live_base_url()

SIG = {"session": {"primary": "ny"}, "regime": {"regime": "TRENDING"},
       "mtf_tiers": {"SHORT": {"direction": "UP"}}, "stop_loss": 4090.0}
STATE = "XAUUSD|BUY|ny|TRENDING|short:WITH"


def t(pnl, entry=4100.0, exit_p=None, sid="s1"):
    if exit_p is None:
        exit_p = entry + (5.0 if pnl > 0 else -5.0)
    return {"pnl": pnl, "symbol": "XAUUSD", "action": "BUY", "signal_id": sid,
            "entry_price": entry, "exit_price": exit_p, "stop_loss": 4090.0}


class TestRMultiple:
    def test_full_sl_hit_is_minus_1r(self):
        # SL 10 below entry; exited exactly at SL → R = -1
        tr = t(-100.0, entry=4100.0, exit_p=4090.0)
        assert trade_r_multiple(tr, SIG) == pytest.approx(-1.0)

    def test_2r_winner(self):
        tr = t(200.0, entry=4100.0, exit_p=4120.0)  # +20 move vs 10 risk
        assert trade_r_multiple(tr, SIG) == pytest.approx(2.0)

    def test_missing_data_none(self):
        assert trade_r_multiple({"pnl": 10.0, "symbol": "XAUUSD"}, {}) is None


class TestModelAndDecision:
    def _model(self, wins, losses):
        trades = ([t(150.0, exit_p=4115.0) for _ in range(wins)]
                  + [t(-100.0, exit_p=4090.0) for _ in range(losses)])
        return build_model(trades, {"s1": SIG})

    def test_posterior_shrinks_small_samples(self):
        m = self._model(1, 0)   # 1 win only → prior pulls toward 50%
        d = decision(m, STATE, "XAUUSD|BUY")
        assert 0.5 < d["p_success"] < 0.7

    def test_high_win_rate_state(self):
        m = self._model(15, 3)
        d = decision(m, STATE, "XAUUSD|BUY")
        assert d["p_success"] > 0.7
        assert d["expected_reward_r"] == pytest.approx(1.5)   # +15 pips vs 10 risk
        assert d["expected_loss_r"] == pytest.approx(1.0)
        assert d["ev_r"] > 0.5 and d["quality"] == "A"
        assert d["ci90"][0] < d["p_success"] < d["ci90"][1]

    def test_losing_state_quality_d(self):
        m = self._model(2, 14)
        d = decision(m, STATE, "XAUUSD|BUY")
        assert d["p_success"] < 0.35 and d["quality"] == "D" and d["ev_r"] < 0
        assert d["n"] >= MIN_EVIDENCE

    def test_hierarchical_fallback_to_symbol(self):
        m = self._model(12, 2)
        d = decision(m, "XAUUSD|BUY|tokyo|RANGE|short:FLAT", "XAUUSD|BUY")
        assert d["n"] == 14 and d["p_success"] > 0.6

    def test_unknown_everything_uses_prior(self):
        d = decision({"states": {}, "symbols": {}}, "X|BUY|a|B|short:FLAT", "X|BUY")
        assert d["p_success"] == pytest.approx(PRIOR_A / (PRIOR_A + PRIOR_B))
        assert d["n"] == 0 and d["quality"] in ("C", "D", "B")

    def test_plan_r_fallback(self):
        d = decision({"states": {}, "symbols": {}}, "X|BUY|a|B|short:FLAT",
                     "X|BUY", tp_pips=28.0, sl_pips=10.0)
        assert d["expected_reward_r"] == pytest.approx(2.8)


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=15)
    assert r.status_code == 200
    return s


class TestHttp:
    def test_posture_includes_bayes(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=90)
        assert r.status_code == 200
        sym = list(r.json()["symbols"].values())[0]
        b = sym.get("bayes")
        assert b and "BUY" in b and "SELL" in b
        assert 0 <= b["BUY"]["p_success"] <= 1
        assert b["SELL"]["quality"] in "ABCD"


class TestWiring:
    def test_bot_runner_bayes_gate(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "bayes_decision" in src and '"bayes_block"' in src

    def test_consensus_uses_bayes(self):
        assert 'signal.get("bayes")' in \
            open(os.path.join(BACKEND, "consensus.py")).read()

    def test_config_mode(self):
        assert 'bayes_gate_mode: str = "advisory"' in \
            open(os.path.join(BACKEND, "models.py")).read()


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
