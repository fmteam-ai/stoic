"""iter-99 HTTP smoke: /api/trades/{id}/dna, /api/bot/market-state,
/api/ops/release-safety. Session-cookie auth for user endpoints;
METRICS_TOKEN header for ops."""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") + "/api"
METRICS_TOKEN = os.environ["METRICS_TOKEN"]
ADMIN = ("admin@trading.bot", "admin123")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def mongo():
    cli = MongoClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    yield db
    cli.close()


def test_market_state_returns_shape(admin_session):
    r = admin_session.get(f"{BASE}/bot/market-state", timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["scores"]) == {"trend", "volatility", "vol_stability",
                                   "liquidity", "news_risk", "confidence"}
    assert 0 <= body["market_health"] <= 100
    for v in body["scores"].values():
        assert 0 <= v <= 100


def test_market_state_requires_auth():
    r = requests.get(f"{BASE}/bot/market-state", timeout=10)
    assert r.status_code in (401, 403)


def test_release_safety_with_metrics_token():
    r = requests.get(f"{BASE}/ops/release-safety",
                     headers={"X-Metrics-Token": METRICS_TOKEN}, timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "release_safety_score" in body
    assert 0 <= body["release_safety_score"] <= 100
    assert body["verdict"] in ("PROMOTE", "CANARY_ONLY", "BLOCK")
    assert "components" in body


def test_release_safety_with_admin_cookie(admin_session):
    r = admin_session.get(f"{BASE}/ops/release-safety", timeout=30)
    assert r.status_code == 200, r.text
    assert "release_safety_score" in r.json()


def test_release_safety_forbidden_without_auth():
    r = requests.get(f"{BASE}/ops/release-safety", timeout=10)
    assert r.status_code in (401, 403)


def test_trade_dna_unknown_returns_404(admin_session):
    r = admin_session.get(f"{BASE}/trades/000000000000000000000000/dna",
                          timeout=15)
    assert r.status_code == 404, r.text


def test_trade_dna_requires_auth():
    r = requests.get(f"{BASE}/trades/000000000000000000000000/dna", timeout=10)
    assert r.status_code in (401, 403)


def test_trade_dna_real_trade(admin_session, mongo):
    """Seed a closed trade + signal + eval owned by admin, verify DNA."""
    admin_user = mongo.users.find_one({"email": ADMIN[0]})
    assert admin_user, "admin user missing"
    uid = str(admin_user["_id"])
    now = datetime.now(timezone.utc).isoformat()
    tag = f"TEST-iter99-{uuid.uuid4().hex[:6]}"
    sid = mongo.signals.insert_one({
        "user_id": uid, "symbol": "XAUUSD", "action": "BUY", "confidence": 71,
        "reasoning": f"why {tag} pullback",
        "monte_carlo": {"ev_r_net": 0.22},
        "uncertainty": {"confidence_pct": 65, "risk": "MEDIUM"},
        "consensus": {"score": 60}, "spread": 2.5,
        "news_ai": {"net": 0.1}, "strategy_engine": "mtf_v2",
        "trend_score": {"quality": {"score": 70, "direction": "UP",
                                    "components": {"volatility_support": 60}}},
        "created_at": now,
    }).inserted_id
    tid = mongo.trades.insert_one({
        "user_id": uid, "account_id": f"acct-{tag}", "symbol": "XAUUSD",
        "action": "BUY", "status": "closed", "origin": "auto", "pnl": 30.0,
        "signal_id": str(sid), "scope": "mtf", "risk_pct": 0.5, "lot_size": 0.1,
        "entry_price": 4000.0, "stop_loss": 3995.0, "take_profit": 4010.0,
        "close_reason": "take_profit",
        "market_regime": {"key": "trending_up|normal"},
        "versions": {"strategy_version": "mtf_v2"},
        "opened_at": now, "closed_at": now, "_test_tag": tag,
    }).inserted_id
    mongo.trade_evaluations.insert_one({
        "trade_id": str(tid), "user_id": uid,
        "mfe_r": 2.0, "mae_r": -0.2, "realized_r": 1.9,
    })
    try:
        r = admin_session.get(f"{BASE}/trades/{tid}/dna", timeout=20)
        assert r.status_code == 200, r.text
        dna = r.json()
        assert dna["trade_id"] == str(tid)
        assert dna["confidence"]["raw"] == 71
        assert dna["expected_value_r"] == 0.22
        assert "four_questions" in dna and "attestation" in dna
        assert dna["four_questions"]["outcome_vs_expectation"]["verdict"] == \
            "beat expectation"
    finally:
        mongo.trades.delete_one({"_id": tid})
        mongo.signals.delete_one({"_id": sid})
        mongo.trade_evaluations.delete_many({"trade_id": str(tid)})


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
