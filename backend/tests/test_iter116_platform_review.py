"""iter-116 · Platform-wide review test suite

Complements iteration_38 (which verified the 11 intelligence layers).
This file exercises the CORE trading-platform surface: auth, bot config
CRUD (incl. iter-105..114 fields), signals/trades/history, EA
distribution (v1.43 w/ SendDom+SendCandles), bridge endpoints (candles /
dom / invalid-token), analytics, admin moderation, and Stripe checkout.

STRICT SAFETY: no bot_config `active` flag is ever set true, no trades
are executed / closed / modified, no bridge poll responses are sent.
Bridge candles + dom use the synthetic symbol 'TESTSYM' so nothing feeds
into any live-symbol pipeline.
"""
import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as f:
            for ln in f:
                if ln.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = ln.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except OSError:
        pass

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"
BRIDGE_TOKEN = "-14t8rVrFxnX7AK4vWHveL9QAqNFhO-0AeV-yxShWyQ"


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    assert "access_token" in s.cookies
    return s


@pytest.fixture(scope="module")
def new_user_ctx():
    """Register → activate → login a throwaway user. Returns (session, email, user_id)."""
    email = f"TEST_iter116_{uuid.uuid4().hex[:8]}@example.com"
    password = "TestPass123!"
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})

    r = s.post(f"{BASE_URL}/api/auth/register",
               json={"email": email, "password": password,
                     "name": "iter-116 test",
                     "terms_agreed": True},
               timeout=30)
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text[:200]}"
    reg = r.json()
    user_id = reg["id"]
    assert reg["email_verified"] is False

    # Pull the activation_token from Mongo (dev bypass — the Resend key may
    # be sandboxed and not deliver mail to arbitrary addresses).
    token = None
    try:
        from pymongo import MongoClient
        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "ai_trading_bot")
        cli = MongoClient(mongo_url)
        udoc = cli[db_name].users.find_one({"email": email.lower()})
        assert udoc, f"user not written to DB (email={email.lower()})"
        token = udoc.get("activation_token")
        # Fallback: flip verify flag directly (matches iter-79 pattern).
        if not token:
            cli[db_name].users.update_one(
                {"_id": udoc["_id"]}, {"$set": {"email_verified": True}}
            )
        cli.close()
    except Exception as e:
        pytest.skip(f"MongoDB direct access unavailable — cannot activate test user: {e}")

    if token:
        vr = s.post(f"{BASE_URL}/api/auth/verify-email",
                    json={"token": token}, timeout=15)
        assert vr.status_code == 200, f"verify failed: {vr.status_code} {vr.text[:200]}"

    lr = s.post(f"{BASE_URL}/api/auth/login",
                json={"email": email, "password": password}, timeout=15)
    assert lr.status_code == 200, f"login after verify failed: {lr.status_code} {lr.text[:200]}"
    assert "access_token" in s.cookies

    yield s, email, user_id

    # Teardown — remove the throwaway user + their bot_config so the DB stays clean
    try:
        from pymongo import MongoClient
        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "ai_trading_bot")
        cli = MongoClient(mongo_url)
        cli[db_name].users.delete_one({"email": email.lower()})
        cli[db_name].bot_configs.delete_many({"user_id": user_id})
        cli[db_name].payment_transactions.delete_many({"user_id": user_id})
        cli.close()
    except Exception:
        pass


# ── 1. Auth flows ───────────────────────────────────────────────────────
class TestAuth:
    def test_admin_login_and_me(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=15)
        assert r.status_code == 200
        me = r.json()
        assert me["email"] == ADMIN_EMAIL
        assert me["role"] == "admin"

    def test_new_user_register_verify_login(self, new_user_ctx):
        s, email, _ = new_user_ctx
        r = s.get(f"{BASE_URL}/api/auth/me", timeout=15)
        assert r.status_code == 200
        me = r.json()
        assert me["email"] == email.lower()
        assert me["role"] == "user"
        assert me["email_verified"] is True

    def test_logout_clears_session(self):
        # Use a dedicated session so we don't nuke module-scoped fixtures.
        s = requests.Session()
        s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
        assert "access_token" in s.cookies
        r = s.post(f"{BASE_URL}/api/auth/logout", timeout=15)
        assert r.status_code in (200, 204)
        # Subsequent /me should now fail
        r2 = s.get(f"{BASE_URL}/api/auth/me", timeout=15)
        assert r2.status_code in (401, 403)

    def test_register_requires_terms(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/auth/register",
                   json={"terms_agreed": False, "email": f"TEST_noterms_{uuid.uuid4().hex[:6]}@e.com",
                         "password": "x" * 8},
                   timeout=15)
        assert r.status_code == 400, r.text[:200]


# ── 2. Bot config CRUD w/ iter-105..114 fields ──────────────────────────
NEW_CFG_FIELDS = [
    "liquidity_gate_mode", "news_gate_mode", "calendar_intel_mode",
    "ml_ensemble_mode", "meta_strategy_enabled", "uncertainty_gate_mode",
    "min_calibrated_confidence", "adaptive_sizing_enabled",
    "monte_carlo_mode", "risk_engine_enabled",
    "monthly_drawdown_pct", "cvar_budget_pct",
]


def _mongo_config_for(user_id: str) -> dict:
    """Direct read of the default (account_id=None) bot_config doc."""
    from pymongo import MongoClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "ai_trading_bot")
    cli = MongoClient(mongo_url)
    doc = cli[db_name].bot_configs.find_one({
        "user_id": user_id,
        "$or": [{"account_id": None}, {"account_id": {"$exists": False}}],
    })
    cli.close()
    return doc or {}


class TestBotConfig:
    def test_get_config_ok(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15)
        assert r.status_code == 200, r.text[:200]
        cfg = r.json()
        assert cfg.get("user_id") and cfg.get("id")

    def test_KNOWN_BUG_get_hides_iter_105_114_fields(self, admin_session):
        """BUG: bot_routes._serialize() whitelists an old field set and does
        NOT include iter-105..114 fields — so the API never exposes them,
        even though they exist in the DB. The frontend BotConfig cannot
        read them via /api/bot/config. See critical_code_review_comments.
        """
        r = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15).json()
        missing = [f for f in NEW_CFG_FIELDS if f not in r]
        # Explicitly assert the bug — this test PASSES when the bug is
        # present so the report cleanly flags every hidden field. Once the
        # bug is fixed, flip to `assert not missing`.
        assert missing, (
            "iter-105..114 fields are now surfaced by /api/bot/config — "
            "flip this assertion to `assert not missing`."
        )

    def test_patch_monte_carlo_paths_persists_in_db_and_reverts(self, admin_session):
        r0 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15).json()
        user_id = r0["user_id"]
        db_before = _mongo_config_for(user_id)
        original = int(db_before.get("monte_carlo_paths") or 10000)

        new_val = 5000 if original != 5000 else 4500
        rp = admin_session.put(f"{BASE_URL}/api/bot/config",
                               json={"monte_carlo_paths": new_val}, timeout=15)
        assert rp.status_code == 200, rp.text[:200]

        # DB-level verification (API strips this field — bug documented above)
        db_after = _mongo_config_for(user_id)
        assert int(db_after.get("monte_carlo_paths")) == new_val

        # Revert
        admin_session.put(f"{BASE_URL}/api/bot/config",
                          json={"monte_carlo_paths": original}, timeout=15)
        db_reverted = _mongo_config_for(user_id)
        assert int(db_reverted.get("monte_carlo_paths")) == original

    def test_patch_accepts_all_iter_105_114_fields(self, admin_session):
        """All iter-105..114 fields must be *accepted* by PUT (no 422)
        and persist to the DB even if the API-level serializer hides
        them from the response."""
        r0 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=15).json()
        user_id = r0["user_id"]
        db_before = _mongo_config_for(user_id)
        payload = {
            "liquidity_gate_mode": "advisory",
            "news_gate_mode": "advisory",
            "calendar_intel_mode": "advisory",
            "ml_ensemble_mode": "advisory",
            "meta_strategy_enabled": True,
            "uncertainty_gate_mode": "advisory",
            "min_calibrated_confidence": 55,
            "adaptive_sizing_enabled": True,
            "monte_carlo_mode": "advisory",
            "risk_engine_enabled": True,
            "monthly_drawdown_pct": 15.0,
            "cvar_budget_pct": 9.0,
        }
        rp = admin_session.put(f"{BASE_URL}/api/bot/config",
                               json=payload, timeout=15)
        assert rp.status_code == 200, rp.text[:200]

        db_after = _mongo_config_for(user_id)
        for k, v in payload.items():
            assert db_after.get(k) == v, (
                f"iter-105..114 field {k} did not persist to DB: sent {v} "
                f"got {db_after.get(k)}"
            )

        # Revert every field we touched to the value that existed before.
        revert = {k: db_before.get(k) for k in payload
                  if db_before.get(k) is not None}
        if revert:
            admin_session.put(f"{BASE_URL}/api/bot/config",
                              json=revert, timeout=15)


# ── 3. Signals & trades read APIs ───────────────────────────────────────
class TestSignalsAndTrades:
    def test_get_signals(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/signals", timeout=20)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        # Endpoint may return list or wrapped object — accept both.
        assert isinstance(body, (list, dict))

    def test_get_trades(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/trades?limit=20", timeout=20)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert isinstance(body, (list, dict))

    def test_get_trade_stats(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/trades/stats", timeout=20)
        assert r.status_code == 200, r.text[:200]

    def test_get_trade_history(self, admin_session):
        # Endpoint requires date range params (regression-safe pagination).
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        r = admin_session.get(
            f"{BASE_URL}/api/trades/history",
            params={
                "date_from": (now - timedelta(days=30)).date().isoformat(),
                "date_to": now.date().isoformat(),
                "limit": 20,
            },
            timeout=25,
        )
        assert r.status_code == 200, r.text[:200]


# ── 4. EA distribution ──────────────────────────────────────────────────
class TestEADistribution:
    def test_ea_script_v143_with_dom_and_candles(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ea-script", timeout=15)
        assert r.status_code == 200
        text = r.text
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from ea_version import current_ea_version
        v = current_ea_version()
        assert f'#property version   "{v}"' in text, f"EA version {v} marker missing"
        assert "SendDom" in text, "SendDom function missing from EA"
        assert "SendCandles" in text, "SendCandles function missing from EA"

    def test_bridge_download_ea_same_content(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bridge/download-ea", timeout=15)
        assert r.status_code == 200
        assert '1.43' in r.text


# ── 5. Bridge endpoints ─────────────────────────────────────────────────
class TestBridge:
    def test_candles_valid_token_synthetic_symbol(self):
        bars = [
            {"t": int(time.time()) - (39 - i) * 900,
             "o": 100 + i * 0.1, "h": 100.5 + i * 0.1,
             "l": 99.5 + i * 0.1, "c": 100.2 + i * 0.1, "v": 10}
            for i in range(40)
        ]
        r = requests.post(f"{BASE_URL}/api/bridge/candles",
                          json={"bridge_token": BRIDGE_TOKEN,
                                "symbol": "TESTSYM",
                                "timeframe": "M15",
                                "bars": bars},
                          timeout=20)
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        body = r.json()
        assert body["status"] == "ok"
        # iter-125: server accumulates history (cap 800) — stored is the
        # TOTAL retained bar count, not this batch's size.
        assert 40 <= int(body["stored"]) <= 800

    def test_candles_invalid_token_401(self):
        r = requests.post(f"{BASE_URL}/api/bridge/candles",
                          json={"bridge_token": "not-a-real-token",
                                "symbol": "TESTSYM",
                                "timeframe": "M15",
                                "bars": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}]},
                          timeout=15)
        assert 400 <= r.status_code < 500, r.status_code

    def test_dom_valid_token_synthetic_symbol(self):
        bids = [{"p": 100.0 - i * 0.01, "v": 5} for i in range(5)]
        asks = [{"p": 100.5 + i * 0.01, "v": 5} for i in range(5)]
        r = requests.post(f"{BASE_URL}/api/bridge/dom",
                          json={"bridge_token": BRIDGE_TOKEN,
                                "symbol": "TESTSYM",
                                "bids": bids, "asks": asks},
                          timeout=15)
        assert r.status_code == 200, r.text[:200]
        b = r.json()
        assert b["status"] == "ok"
        assert int(b["stored"]) == 10

    def test_dom_invalid_token(self):
        r = requests.post(f"{BASE_URL}/api/bridge/dom",
                          json={"bridge_token": "bogus",
                                "symbol": "TESTSYM",
                                "bids": [{"p": 1, "v": 1}], "asks": []},
                          timeout=15)
        assert 400 <= r.status_code < 500

    def test_heartbeat_invalid_token(self):
        # NOTE: intentionally do NOT test heartbeat with the real token as
        # it would mutate a live user's account 'last_heartbeat' & balance.
        r = requests.post(f"{BASE_URL}/api/bridge/heartbeat",
                          json={"bridge_token": "definitely-not-real",
                                "balance": 0, "equity": 0,
                                "open_positions": 0},
                          timeout=15)
        assert 400 <= r.status_code < 500


# ── 6. Intelligence smoke re-check ──────────────────────────────────────
class TestIntelligenceSmoke:
    def test_architecture_11_stages(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/architecture", timeout=15)
        assert r.status_code == 200
        body = r.json()
        stages = body.get("stages") or body
        # Whatever container it uses, length must be 11
        if isinstance(stages, dict) and "stages" in stages:
            stages = stages["stages"]
        assert isinstance(stages, list)
        assert len(stages) == 11, f"Expected 11 stages, got {len(stages)}"

    def test_posture_has_symbols(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/posture", timeout=20)
        assert r.status_code == 200
        body = r.json()
        assert "symbols" in body or isinstance(body, list)

    def test_ml_ensemble(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ml/ensemble", timeout=15)
        assert r.status_code == 200
        b = r.json()
        # Must include either 'trained' or model summary keys
        assert isinstance(b, dict)


# ── 7. Risk / analytics / bot-health ────────────────────────────────────
class TestAnalytics:
    def test_bot_health_score(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
        assert r.status_code == 200

    def test_risk_gauge(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=15)
        assert r.status_code == 200

    def test_analytics_sessions(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/analytics/sessions", timeout=20)
        assert r.status_code == 200

    def test_analytics_attribution(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/analytics/attribution", timeout=20)
        assert r.status_code == 200

    def test_portfolio_snapshot(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/portfolio/snapshot", timeout=20)
        assert r.status_code == 200


# ── 8. Admin moderation ─────────────────────────────────────────────────
class TestAdminModeration:
    def test_admin_users_list_as_admin(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/users?limit=5", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, (list, dict))

    def test_admin_users_list_denied_for_regular_user(self, new_user_ctx):
        s, _, _ = new_user_ctx
        r = s.get(f"{BASE_URL}/api/admin/users?limit=5", timeout=15)
        assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"


# ── 9. Stripe subscription checkout ─────────────────────────────────────
class TestSubscription:
    def test_plans_list(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/subscription/plans", timeout=15)
        assert r.status_code == 200
        body = r.json()
        # Must contain at least one plan
        plans = body if isinstance(body, list) else body.get("plans") or body.get("items") or []
        assert plans, "No subscription plans exposed"

    def test_admin_checkout_blocked(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/subscription/checkout",
                               json={"plan_id": "pro_monthly",
                                     "origin": BASE_URL},
                               timeout=20)
        assert r.status_code == 400, f"admin should be blocked: {r.status_code}"

    def test_new_user_checkout_returns_url(self, new_user_ctx, admin_session):
        s, _, _ = new_user_ctx
        # First figure out a valid plan_id
        plans_r = admin_session.get(f"{BASE_URL}/api/subscription/plans", timeout=15).json()
        plans = plans_r if isinstance(plans_r, list) else plans_r.get("plans") or plans_r.get("items") or []
        assert plans, "no plans"
        plan_id = plans[0].get("id") or plans[0].get("plan_id")
        r = s.post(f"{BASE_URL}/api/subscription/checkout",
                   json={"plan_id": plan_id, "origin": BASE_URL},
                   timeout=25)
        # sk_test_emergent may or may not be accepted by the emergent-integrations
        # stripe stub. Accept 200 (URL returned) OR 502 (upstream fake key rejected)
        # but must NOT be 500 crashes or 403.
        assert r.status_code in (200, 502), f"unexpected: {r.status_code} {r.text[:200]}"
        if r.status_code == 200:
            body = r.json()
            assert "checkout_url" in body and body["checkout_url"].startswith("http")
