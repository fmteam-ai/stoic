"""iter-61 · Offline RL policy layer — reward shaping, state extraction,
distributional decisions, trainer + HTTP API."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rl_policy import (  # noqa: E402
    compute_rewards, extract_state, build_policy, decide, rl_decision,
    MIN_VISITS, RISK_LAMBDA, DD_MU,
)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")


def trade(pnl, sym="XAUUSD", action="SELL", sid="s1", i=0):
    return {"pnl": pnl, "symbol": sym, "action": action, "signal_id": sid,
            "closed_at": f"2026-07-01T{i:02d}:00:00"}


SIG = {"session": {"primary": "london"}, "regime": {"regime": "LOW_VOL_TREND"},
       "mtf_tiers": {"SHORT": {"direction": "UP"}}}


class TestRewards:
    def test_win_passes_through(self):
        out = compute_rewards([trade(100.0)])
        assert out[0][1] == pytest.approx(100.0)

    def test_loss_amplified_and_dd_penalised(self):
        # single loss from equity 0: dd_inc == 50 → r = -50 -0.5*50 -0.5*50
        out = compute_rewards([trade(-50.0)])
        assert out[0][1] == pytest.approx(-50 - RISK_LAMBDA * 50 - DD_MU * 50)

    def test_loss_inside_profit_cushion_less_penalised(self):
        # +100 then -50: equity stays positive but dd deepens by 50 from peak
        out = compute_rewards([trade(100.0, i=1), trade(-50.0, i=2)])
        assert out[1][1] == pytest.approx(-50 - 25 - 25)

    def test_recovery_trade_no_dd_penalty(self):
        out = compute_rewards([trade(-50.0, i=1), trade(80.0, i=2)])
        assert out[1][1] == pytest.approx(80.0)


class TestState:
    def test_state_key(self):
        k = extract_state("XAUUSD-ECN", "SELL", SIG)
        assert k == "XAUUSD|SELL|london|LOW_VOL_TREND|short:AGAINST"

    def test_alignment_with(self):
        assert "short:WITH" in extract_state("XAUUSD", "BUY", SIG)

    def test_missing_context_defaults(self):
        k = extract_state("US30", "BUY", {})
        assert k == "US30|BUY|unknown|UNKNOWN|short:FLAT"


class TestDecide:
    def _policy(self, pnls):
        trades = [trade(p, sid="s1", i=i) for i, p in enumerate(pnls)]
        return build_policy(trades, {"s1": SIG})

    def test_insufficient_data_allows(self):
        pol = self._policy([-100] * (MIN_VISITS - 1))
        d = decide(pol, extract_state("XAUUSD", "SELL", SIG))
        assert d["decision"] == "ALLOW" and "insufficient" in d["reason"]

    def test_reliably_losing_state_blocked(self):
        pol = self._policy([-80, -90, -100, -70, -85, -95, -75, -60, -110, -50])
        d = decide(pol, extract_state("XAUUSD", "SELL", SIG))
        assert d["decision"] == "BLOCK"

    def test_profitable_state_allowed(self):
        pol = self._policy([40, 55, 30, 60, 45, 35, 50, 42, 38, 44])
        d = decide(pol, extract_state("XAUUSD", "SELL", SIG))
        assert d["decision"] == "ALLOW" and d["mean"] > 0

    def test_marginally_negative_state_scaled(self):
        # mean slightly negative but high variance → UCB > 0 → SCALE
        pol = self._policy([-100, 90, -80, 70, -90, 85, -75, 60, -95, 80])
        d = decide(pol, extract_state("XAUUSD", "SELL", SIG))
        assert d["decision"] in ("SCALE", "ALLOW")
        if d["decision"] == "SCALE":
            assert d["scale"] == 0.5

    def test_unknown_state_allows(self):
        d = decide({"states": {}}, "XAUUSD|BUY|tokyo|RANGE|short:WITH")
        assert d["decision"] == "ALLOW"

    def test_rl_decision_from_signal(self):
        pol = self._policy([-80] * 10)
        sig = {**SIG, "action": "SELL"}
        d = rl_decision(pol, sig, "XAUUSD-ECN")
        assert d["decision"] == "BLOCK"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=15)
    assert r.status_code == 200
    return s


class TestHttp:
    def test_train_and_get(self, session):
        r = session.post(f"{BASE_URL}/api/rl/train", timeout=60)
        assert r.status_code == 200
        d = r.json()
        assert d["trades_used"] > 0 and d["states_learned"] > 0
        r2 = session.get(f"{BASE_URL}/api/rl/policy", timeout=30)
        assert r2.status_code == 200
        assert r2.json()["trained_at"] == d["trained_at"]

    def test_posture_includes_rl(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=30)
        assert r.status_code == 200
        assert "rl_policy" in r.json()


class TestWiring:
    def test_bot_runner_rl_gate(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "rl_decision" in src and '"rl_policy_block"' in src
        assert "rl_scale" in src

    def test_config_model_has_mode(self):
        src = open(os.path.join(BACKEND, "models.py")).read()
        assert 'rl_policy_mode: str = "advisory"' in src
