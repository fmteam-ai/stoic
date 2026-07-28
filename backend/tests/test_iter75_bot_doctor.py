"""iter-75 · Bot Doctor (self-diagnosis LITE) tests.

Heavily exercises the rule-based fallback so the dashboard tile is never
empty when the LLM is unavailable. The LLM path is integration-tested
separately via the HTTP smoke at the bottom.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import os
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from bot_doctor import (
    _rule_based_fallback,
    _collect_telemetry,
    CACHE_TTL_SECONDS,
    LOOKBACK_MINUTES,
)


BASE_URL = "https://stoic-trading-bot.preview.emergentagent.com"
try:
    with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                break
except Exception:
    pass
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _get_db_url():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    try:
        with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME="):
                    db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return mongo_url, db_name


# ──────────── Rule-based fallback: structural ────────────

def test_fallback_healthy_when_no_anomalies():
    """No failures + no blocked + good win rate → status='healthy'."""
    t = {
        "accounts": [{"label": "A", "trading_blocked": False, "heartbeat_age_seconds": 30}],
        "failed_trade_count": 0,
        "closed_trade_stats": {"count": 6, "wins": 4, "losses": 2, "win_rate_pct": 66.7},
        "signal_stats": {"total": 30, "actions": {"HOLD": 28, "BUY": 2}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] == "healthy"
    assert "nominal" in out["headline"].lower()
    assert isinstance(out["findings"], list)
    assert isinstance(out["recommendations"], list) and len(out["recommendations"]) >= 1


def test_fallback_critical_when_account_blocked():
    """Blocked account → critical + concrete action."""
    t = {
        "accounts": [
            {"label": "Tauro", "trading_blocked": True,
             "block_retcode_label": "SYMBOL_NOT_FOUND"},
            {"label": "VT",    "trading_blocked": False, "heartbeat_age_seconds": 5},
        ],
        "failed_trade_count": 3,
        "closed_trade_stats": {"count": 0, "wins": 0, "losses": 0, "win_rate_pct": None},
        "signal_stats": {"total": 10, "actions": {}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] == "critical"
    assert "Tauro" in out["headline"]
    assert "SYMBOL_NOT_FOUND" in out["headline"]
    assert any("Symbols" in r["action"] or "symbol_suffix" in r["action"]
               for r in out["recommendations"])
    assert out["evidence"]["accounts_blocked"] == 1


def test_fallback_degraded_on_stale_heartbeat():
    """No block but EA hasn't phoned home in 10min → degraded."""
    t = {
        "accounts": [{"label": "Mt5Demo", "trading_blocked": False,
                      "heartbeat_age_seconds": 600}],
        "failed_trade_count": 0,
        "closed_trade_stats": {"count": 2, "wins": 1, "losses": 1, "win_rate_pct": 50.0},
        "signal_stats": {"total": 0, "actions": {}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] == "degraded"
    assert "Mt5Demo" in str(out["findings"])
    assert out["evidence"]["accounts_stale"] == 1


def test_fallback_watch_on_low_win_rate():
    """Win rate <40% with ≥5 closed trades → at least 'watch'."""
    t = {
        "accounts": [{"label": "OK", "trading_blocked": False, "heartbeat_age_seconds": 5}],
        "failed_trade_count": 0,
        "closed_trade_stats": {"count": 8, "wins": 2, "losses": 6, "win_rate_pct": 25.0},
        "signal_stats": {"total": 30, "actions": {"HOLD": 28, "SELL": 2}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] in ("watch", "degraded")
    assert any("Win rate" in f or "win rate" in f for f in out["findings"])
    assert any("adaptive_risk" in r["action"].lower()
               for r in out["recommendations"])


def test_fallback_degraded_on_failure_spike():
    """3+ failed trades in last hour even without block → degraded."""
    t = {
        "accounts": [{"label": "OK", "trading_blocked": False, "heartbeat_age_seconds": 5}],
        "failed_trade_count": 5,
        "closed_trade_stats": {"count": 0, "wins": 0, "losses": 0, "win_rate_pct": None},
        "signal_stats": {"total": 10, "actions": {}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] == "degraded"
    assert "5" in out["headline"] or "failures" in out["headline"].lower()
    assert any("Trades page" in r["action"] or "error" in r["action"]
               for r in out["recommendations"])


def test_fallback_zero_signals_watch():
    """No signals in last hour → watch."""
    t = {
        "accounts": [{"label": "OK", "trading_blocked": False, "heartbeat_age_seconds": 30}],
        "failed_trade_count": 0,
        "closed_trade_stats": {"count": 0, "wins": 0, "losses": 0, "win_rate_pct": None},
        "signal_stats": {"total": 0, "actions": {}, "vetos": {}},
    }
    out = _rule_based_fallback(t)
    assert out["status"] == "watch"
    assert "signal" in out["headline"].lower()


def test_fallback_response_shape_always_valid():
    """Every fallback must return the full advertised shape."""
    out = _rule_based_fallback({"accounts": [], "failed_trade_count": 0,
                                "closed_trade_stats": {}, "signal_stats": {}})
    required = {"status", "headline", "findings", "root_cause_hypothesis",
                "recommendations", "evidence"}
    assert required.issubset(out.keys())
    assert isinstance(out["findings"], list) and len(out["findings"]) >= 1
    assert isinstance(out["recommendations"], list) and len(out["recommendations"]) >= 1
    for r in out["recommendations"]:
        assert {"action", "rationale", "effort", "destructive"}.issubset(r.keys())
        assert r["effort"] in ("low", "medium", "high")
        assert isinstance(r["destructive"], bool)


def test_fallback_marks_llm_failed_when_error_passed():
    """When the LLM error string is passed, the response flags it."""
    out = _rule_based_fallback({"accounts": [], "failed_trade_count": 0,
                                "closed_trade_stats": {}, "signal_stats": {}},
                               llm_error="Connection timeout")
    assert out["_llm_failed"] is True
    assert "LLM unavailable" in out["root_cause_hypothesis"]


def test_fallback_clean_state_without_llm_error_marker():
    """No LLM error → _llm_failed is False (or absent → falsy)."""
    out = _rule_based_fallback({"accounts": [], "failed_trade_count": 0,
                                "closed_trade_stats": {}, "signal_stats": {}})
    assert not out.get("_llm_failed")


# ──────────── Telemetry collector ────────────

@pytest.mark.asyncio
async def test_telemetry_collector_shape():
    """_collect_telemetry returns the full expected envelope."""
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    try:
        out = await _collect_telemetry(db, user_id="nonexistent-user-iter75")
        assert out["lookback_minutes"] == LOOKBACK_MINUTES
        assert "failed_trades" in out
        assert "accounts" in out
        assert "signal_stats" in out
        assert "closed_trade_stats" in out
    finally:
        client.close()


@pytest.mark.asyncio
async def test_telemetry_aggregates_recent_failures():
    """Seeds 4 failed trades and confirms they bubble up in telemetry."""
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    uid = "test-iter75-failures"
    now = datetime.now(timezone.utc)
    try:
        await db.trades.delete_many({"user_id": uid})
        for i in range(4):
            await db.trades.insert_one({
                "user_id": uid, "status": "failed",
                "symbol": "XAUUSD.c", "base_symbol": "XAUUSD",
                "symbol_suffix_applied": ".c",
                "error": "symbol_not_found:XAUUSD.c",
                "opened_at": (now - timedelta(minutes=i + 1)).isoformat(),
                "broker": "Tauro", "account_id": "acct-iter75",
            })
        out = await _collect_telemetry(db, user_id=uid)
        assert out["failed_trade_count"] == 4
        # newest first
        assert out["failed_trades"][0]["symbol"] == "XAUUSD.c"
        assert "symbol_not_found" in out["failed_trades"][0]["error"]
    finally:
        await db.trades.delete_many({"user_id": uid})
        client.close()


# ──────────── HTTP integration ────────────

@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def test_doctor_endpoint_returns_full_payload(admin_session):
    """GET /api/bot/doctor returns the canonical shape."""
    r = admin_session.get(f"{BASE_URL}/api/bot/doctor?force_refresh=true",
                          timeout=60)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    for k in ("status", "headline", "findings", "root_cause_hypothesis",
              "recommendations", "evidence", "generated_at"):
        assert k in body, f"missing key {k}"
    assert body["status"] in ("healthy", "watch", "degraded", "critical")


def test_doctor_cache_hit_on_second_call(admin_session):
    """First call generates, second within 5min should be a cache hit."""
    r1 = admin_session.get(f"{BASE_URL}/api/bot/doctor?force_refresh=true",
                           timeout=60)
    assert r1.status_code == 200
    r2 = admin_session.get(f"{BASE_URL}/api/bot/doctor", timeout=15)
    assert r2.status_code == 200
    assert r2.json().get("cache_hit") is True


def test_doctor_per_account_scoped(admin_session):
    """Passing account_id scopes the diagnosis to that account."""
    accts = admin_session.get(f"{BASE_URL}/api/accounts", timeout=15).json()
    if not isinstance(accts, list) or not accts:
        pytest.skip("no accounts on this test instance")
    mt5 = [a for a in accts if isinstance(a, dict) and not a.get("kind")]
    if not mt5:
        pytest.skip("no MT5 accounts on this test instance")
    aid = mt5[0]["id"]
    r = admin_session.get(
        f"{BASE_URL}/api/bot/doctor?account_id={aid}&force_refresh=true",
        timeout=60)
    assert r.status_code == 200
    assert r.json()["status"] in ("healthy", "watch", "degraded", "critical")
