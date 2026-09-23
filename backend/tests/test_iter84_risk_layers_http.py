from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-84 HTTP-level tests for GET /api/risk/layers.

Independent verification (additional to test_iter84_risk_layers.py) of:
- admin session returns 12 layers with valid statuses + auto-picks account
- non-admin cross-account access returns 403
- bogus/nonexistent account_id returns 404
- portfolio stop full trip flow via HTTP layer status (backend-only trip
  using a synthetic account + config, then verify via API + DB, cleanup)
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from bson import ObjectId

from tests.helpers import base_url, register_and_login, mongo_db, mark_email_verified  # noqa: E402


API = f"{base_url()}/api"
pass  # ADMIN_EMAIL comes from live_target
ADMIN_PASS = ADMIN_PASSWORD
VALID_LAYER_STATUS = {"armed", "tripped", "degraded", "error"}
EXPECTED_LAYER_KEYS = {
    "strategy_stop", "position_stop", "portfolio_stop",
    "daily_loss_stop", "weekly_loss_stop", "monthly_drawdown_stop",
    "volatility_stop", "spread_protection", "liquidity_protection",
    "broker_anomaly_protection", "news_protection", "circuit_breaker",
}


def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, r.text
    return s


def _admin_id() -> str:
    u = mongo_db().users.find_one({"email": ADMIN_EMAIL})
    assert u, "admin user missing"
    return str(u["_id"])


# ---------- GET /api/risk/layers ----------
class TestRiskLayersEndpoint:
    def test_admin_gets_12_layers(self):
        s = _admin_session()
        r = s.get(f"{API}/risk/layers", timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "layers" in d
        assert len(d["layers"]) == 12
        keys = {l["layer"] for l in d["layers"]}
        assert keys == EXPECTED_LAYER_KEYS, f"missing/extra keys: {keys ^ EXPECTED_LAYER_KEYS}"
        for l in d["layers"]:
            assert set(l.keys()) >= {"layer", "label", "description", "status", "detail"}
            assert l["status"] in VALID_LAYER_STATUS
            assert l["label"] and l["description"] and l["detail"]
        # summary counters must be sane and match the layers
        assert d["tripped"] == sum(1 for l in d["layers"] if l["status"] == "tripped")
        assert d["degraded"] == sum(1 for l in d["layers"]
                                    if l["status"] in ("degraded", "error"))

    def test_bogus_account_id_404(self):
        s = _admin_session()
        fake = str(ObjectId())  # valid ObjectId shape, non-existent
        r = s.get(f"{API}/risk/layers?account_id={fake}", timeout=30)
        assert r.status_code == 404, r.text

    def test_non_admin_other_users_account_403(self):
        # Create a fresh non-admin user
        email = f"iter84-http-{uuid.uuid4().hex[:8]}@trading.bot"
        s_user = register_and_login(email)
        db = mongo_db()
        # Insert an account owned by admin
        admin_id = _admin_id()
        acc = db.accounts.insert_one({
            "user_id": admin_id, "label": f"TEST_iter84_http_{uuid.uuid4().hex[:6]}",
            "bridge_token": f"iter84-http-{uuid.uuid4().hex}",
            "balance": 1000.0, "equity": 1000.0,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "status": "active"})
        try:
            r = s_user.get(f"{API}/risk/layers?account_id={acc.inserted_id}", timeout=30)
            assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
        finally:
            db.accounts.delete_one({"_id": acc.inserted_id})
            db.users.delete_one({"email": email.lower()})

    def test_admin_can_read_any_account(self):
        """Admin bypasses ownership check per route implementation."""
        s = _admin_session()
        db = mongo_db()
        # Create an account owned by an unrelated fake user
        other = db.accounts.insert_one({
            "user_id": "iter84-http-other-user",
            "label": f"TEST_iter84_http_other_{uuid.uuid4().hex[:6]}",
            "bridge_token": f"iter84-http-other-{uuid.uuid4().hex}",
            "balance": 1000.0, "equity": 1000.0,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "status": "active"})
        try:
            r = s.get(f"{API}/risk/layers?account_id={other.inserted_id}", timeout=30)
            assert r.status_code == 200, r.text
            assert len(r.json()["layers"]) == 12
        finally:
            db.accounts.delete_one({"_id": other.inserted_id})


# ---------- Portfolio-stop trip via HTTP layer view ----------
class TestPortfolioStopEndToEnd:
    def test_trip_flow_shows_tripped_in_layers(self):
        """Backend trip via sweep + verify /api/risk/layers reports TRIPPED for
        the portfolio_stop layer for that specific account."""
        import asyncio
        from risk_layers import sweep_portfolio_stop
        from database import get_db

        from tests.conftest import run_async
        s = _admin_session()
        admin_id = _admin_id()
        db_sync = mongo_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        # 15% floating drawdown → past default 10%
        acc_res = db_sync.accounts.insert_one({
            "user_id": admin_id,
            "label": f"TEST_iter84_http_pstop_{uuid.uuid4().hex[:6]}",
            "bridge_token": f"iter84-http-pstop-{uuid.uuid4().hex}",
            "balance": 10000.0, "equity": 8500.0,
            "last_heartbeat": now_iso, "status": "active"})
        acc_id = str(acc_res.inserted_id)
        cfg_res = db_sync.bot_configs.insert_one({
            "user_id": admin_id, "account_id": acc_id, "active": True})
        try:
            # Run one sweep -> should trip
            async def _do():
                return await sweep_portfolio_stop(get_db())
            trips = run_async(_do())
            assert trips == 1, f"expected 1 trip, got {trips}"

            # Verify DB side-effects
            cfg = db_sync.bot_configs.find_one({"_id": cfg_res.inserted_id})
            assert cfg["active"] is False
            assert cfg["tripped_kind"] == "portfolio"
            alert = db_sync.ops_alerts.find_one({"dedup_key": f"portfolio_stop:{acc_id}"})
            assert alert and alert["severity"] == "critical"
            evt = db_sync.trade_events.find_one({
                "event_type": "PortfolioStopTripped", "account_id": acc_id})
            assert evt is not None

            # 2nd sweep must not re-trip (config is now inactive)
            trips2 = run_async(_do())
            assert trips2 == 0

            # HTTP layers endpoint should reflect the trip for this account
            r = s.get(f"{API}/risk/layers?account_id={acc_id}", timeout=30)
            assert r.status_code == 200
            d = r.json()
            pstop = next(l for l in d["layers"] if l["layer"] == "portfolio_stop")
            assert pstop["status"] == "tripped", pstop
        finally:
            db_sync.accounts.delete_one({"_id": acc_res.inserted_id})
            db_sync.bot_configs.delete_one({"_id": cfg_res.inserted_id})
            db_sync.ops_alerts.delete_many({"dedup_key": f"portfolio_stop:{acc_id}"})
            db_sync.trade_events.delete_many({
                "event_type": "PortfolioStopTripped", "account_id": acc_id})


# ---------- Portfolio stop background loop non-crashing ----------
def test_metrics_endpoint_ok():
    """Ensure /api/metrics is happy — proxy for 'no background loop crashed
    the app'. Requires METRICS_TOKEN from backend/.env."""
    tok = os.environ.get("METRICS_TOKEN")
    if not tok:
        pytest.skip("METRICS_TOKEN not set")
    r = requests.get(f"{API}/metrics",
                     headers={"X-Metrics-Token": tok}, timeout=30)
    assert r.status_code == 200, r.text
    # Prometheus text format
    assert "stoic_" in r.text or "process_" in r.text or "python_" in r.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
