"""iter-105 — Phase 1 Shadow Readiness: four-verdict quorum, benchmark,
twin stress scenarios, shadow health score + promotion pause."""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from decision_validation import four_verdicts
from shadow_benchmark import variant_metrics
from twin_stress import SCENARIOS, apply_scenario


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter105-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------- four-verdict quorum
def test_quorum_all_approve():
    q = four_verdicts({"action": "BUY", "confidence": 72,
                       "monte_carlo": {"ev_r_net": 0.4},
                       "consensus": {"score": 61},
                       "risk_engine": {"status": "ok"},
                       "adaptive_risk": {"multiplier": 0.8},
                       "liquidity": {"verdict": "ok"}})
    assert q["proceed"] is True
    for k in ("ai", "deterministic", "risk", "execution"):
        assert q[k]["verdict"] == "approve"


def test_quorum_rejects_on_any_dissent():
    base = {"action": "BUY", "confidence": 72,
            "monte_carlo": {"ev_r_net": 0.4}, "consensus": {"score": 61}}
    q = four_verdicts({**base, "monte_carlo": {"ev_r_net": -0.2}})
    assert q["deterministic"]["verdict"] == "reject"
    assert q["proceed"] is False
    q = four_verdicts({**base, "risk_engine": {"status": "block"}})
    assert q["risk"]["verdict"] == "reject" and q["proceed"] is False
    q = four_verdicts({**base, "spread_blocked": True})
    assert q["execution"]["verdict"] == "reject" and q["proceed"] is False
    q = four_verdicts({**base, "confidence": 30})
    assert q["ai"]["verdict"] == "reject" and q["proceed"] is False


def test_quorum_abstain_approves_when_data_absent():
    q = four_verdicts({"action": "SELL"})
    assert q["proceed"] is True


# --------------------------------------------------------- twin stress
def test_stress_scenarios_perturb_correctly():
    rs = [1.0, -1.0, 2.0, -1.0, 0.5]
    assert apply_scenario(rs, "latency_spike") == [
        0.95, -1.05, 1.95, -1.05, 0.45]
    assert apply_scenario(rs, "delayed_fill") == [0.9, -1.0, 1.8, -1.0, 0.45]
    assert apply_scenario(rs, "liquidity_drop") == [
        0.5, -0.5, 1.0, -0.5, 0.25]
    assert apply_scenario(rs, "broker_outage") == [1.0, -1.0, 2.0, -1.0]
    gap = apply_scenario(rs, "market_gap")
    assert gap[1] == pytest.approx(-1.3) and gap[0] == 1.0
    assert set(SCENARIOS) == {"latency_spike", "delayed_fill",
                              "spread_explosion", "liquidity_drop",
                              "broker_outage", "market_gap"}


# -------------------------------------------------------- benchmark math
def test_variant_metrics():
    m = variant_metrics([1.0, -1.0, 2.0, -1.0])
    assert m["n"] == 4
    assert m["ev_r"] == pytest.approx(0.25)
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == 1.5
    assert m["max_drawdown_r"] == 1.0
    assert variant_metrics([]) is None


# ------------------------------------------- shadow health + promotion
def test_health_score_composition_and_pause(db):
    async def go():
        from shadow_health import THRESHOLD, health_score
        out = await health_score(db, f"nobody-{UID}")
        assert set(out["components"]) == {
            "data_freshness", "regime_confidence", "calibration_quality",
            "execution_quality", "broker_stability", "worker_health",
            "synchronization"}
        assert out["threshold"] == THRESHOLD
        if out["overall"] is not None:
            expected = (out["overall"] < THRESHOLD
                        or out.get("fail_closed", False))
            assert out["promotions_paused"] == expected
    _run(go())


def test_quorum_stats_reads_ledger(db):
    async def go():
        from decision_validation import quorum_stats
        uid = f"{UID}-qs"
        q = four_verdicts({"action": "BUY", "confidence": 80})
        await db.trade_decisions.insert_one({
            "user_id": uid, "symbol": "XAUUSD", "status": "executed",
            "stage": "execution", "ts": "2026-07-25T00:00:00+00:00",
            "execution": {"validation_quorum": q}})
        out = await quorum_stats(db, uid)
        assert out["total"] == 1 and out["agreed"] == 1
        assert out["agreement_rate"] == 100.0
        await db.trade_decisions.delete_many({"user_id": uid})
    _run(go())
