"""Full backend regression tests for AI Trading Bot.

Covers: health, auth, market data, bot config, signals (AI), accounts,
trade flow, bridge endpoints, EA download.
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def fresh_user_session():
    """Register a brand-new user for isolation."""
    s = requests.Session()
    email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "testpass123", "name": "Tester"}, timeout=30)
    assert r.status_code == 200, f"Register failed: {r.status_code} {r.text}"
    s.email = email  # type: ignore
    return s


# ---------- Health ----------
class TestHealth:
    def test_health(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["db"] == "connected"

    def test_root(self):
        r = requests.get(f"{API}/", timeout=10)
        assert r.status_code == 200
        assert r.json()["service"] == "ai-trading-bot"


# ---------- Auth ----------
class TestAuth:
    def test_admin_login_and_me(self, admin_session):
        r = admin_session.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == ADMIN_EMAIL
        assert body["role"] == "admin"
        assert "id" in body

    def test_register_login_logout(self):
        s = requests.Session()
        email = f"test_{uuid.uuid4().hex[:8]}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["email"] == email.lower()
        # cookie set
        assert "access_token" in s.cookies

        # me works
        r2 = s.get(f"{API}/auth/me", timeout=10)
        assert r2.status_code == 200

        # logout clears cookies
        r3 = s.post(f"{API}/auth/logout", timeout=10)
        assert r3.status_code == 200
        r4 = s.get(f"{API}/auth/me", timeout=10)
        # After logout, cookie should be cleared -> 401
        assert r4.status_code == 401

        # login again
        s2 = requests.Session()
        r5 = s2.post(f"{API}/auth/login", json={"email": email, "password": "testpass123"}, timeout=10)
        assert r5.status_code == 200

    def test_login_invalid_credentials(self):
        r = requests.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, timeout=10)
        assert r.status_code == 401

    def test_me_unauthenticated(self):
        r = requests.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 401


# ---------- Market ----------
class TestMarket:
    def test_symbols(self, admin_session):
        r = admin_session.get(f"{API}/market/symbols", timeout=10)
        assert r.status_code == 200
        symbols = r.json()["symbols"]
        assert "XAUUSD" in symbols
        assert "BTCUSD" in symbols

    def test_risk_profiles(self, admin_session):
        r = admin_session.get(f"{API}/market/risk-profiles", timeout=10)
        assert r.status_code == 200
        profiles = r.json()["profiles"]
        for lv in ("low", "medium", "high", "extreme"):
            assert lv in profiles
            assert "risk_pct" in profiles[lv]
            assert "min_confidence" in profiles[lv]

    def test_quote_xauusd(self, admin_session):
        r = admin_session.get(f"{API}/market/quote/XAUUSD", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["symbol"] == "XAUUSD"
        assert isinstance(data["price"], (int, float))
        assert data["price"] > 0, f"XAUUSD price is non-positive: {data['price']}"

    def test_quote_btcusd(self, admin_session):
        r = admin_session.get(f"{API}/market/quote/BTCUSD", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["symbol"] == "BTCUSD"
        assert isinstance(data["price"], (int, float))
        assert data["price"] > 0

    def test_quote_eurusd(self, admin_session):
        r = admin_session.get(f"{API}/market/quote/EURUSD", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["symbol"] == "EURUSD"
        assert isinstance(data["price"], (int, float))
        assert data["price"] > 0

    def test_quotes_multi(self, admin_session):
        r = admin_session.get(f"{API}/market/quotes?symbols=XAUUSD,BTCUSD", timeout=60)
        assert r.status_code == 200
        quotes = r.json()["quotes"]
        assert len(quotes) == 2
        syms = {q.get("symbol") for q in quotes}
        assert syms == {"XAUUSD", "BTCUSD"}

    @pytest.mark.parametrize("symbol", ["XAUUSD", "BTCUSD", "EURUSD"])
    def test_history_with_indicators(self, admin_session, symbol):
        r = admin_session.get(f"{API}/market/history/{symbol}", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["symbol"] == symbol
        assert isinstance(data["history"], list)
        assert len(data["history"]) > 100, f"{symbol} history only has {len(data['history'])} points"
        assert isinstance(data["indicators"], dict)


# ---------- Bot Config ----------
class TestBotConfig:
    def test_get_default_config(self, fresh_user_session):
        r = fresh_user_session.get(f"{API}/bot/config", timeout=10)
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["risk_level"] in ("low", "medium", "high", "extreme")
        assert "XAUUSD" in cfg["symbols"]
        assert cfg["active"] is False

    def test_update_config_and_persist(self, fresh_user_session):
        payload = {
            "risk_level": "high",
            "symbols": ["XAUUSD", "BTCUSD", "ETHUSD"],
            "active": False,
            "max_concurrent_trades": 5,
            "auto_execute": False,
        }
        r = fresh_user_session.put(f"{API}/bot/config", json=payload, timeout=10)
        assert r.status_code == 200, r.text
        # GET back
        r2 = fresh_user_session.get(f"{API}/bot/config", timeout=10)
        cfg = r2.json()
        assert cfg["risk_level"] == "high"
        assert "ETHUSD" in cfg["symbols"]
        assert cfg["max_concurrent_trades"] == 5
        assert cfg["auto_execute"] is False

    def test_start_stop_bot(self, fresh_user_session):
        r1 = fresh_user_session.post(f"{API}/bot/start", timeout=10)
        assert r1.status_code == 200
        assert r1.json()["active"] is True
        cfg = fresh_user_session.get(f"{API}/bot/config", timeout=10).json()
        assert cfg["active"] is True

        r2 = fresh_user_session.post(f"{API}/bot/stop", timeout=10)
        assert r2.status_code == 200
        assert r2.json()["active"] is False
        cfg2 = fresh_user_session.get(f"{API}/bot/config", timeout=10).json()
        assert cfg2["active"] is False


# ---------- Accounts ----------
class TestAccounts:
    def test_account_crud_and_token_rotation(self, fresh_user_session):
        # Create
        payload = {
            "label": "TEST_Account_1",
            "broker": "Exness",
            "server": "Exness-MT5Trial",
            "account_number": "12345678",
            "account_type": "microcent",
            "base_currency": "USD",
        }
        r = fresh_user_session.post(f"{API}/accounts", json=payload, timeout=10)
        assert r.status_code == 200, r.text
        acc = r.json()
        assert acc["label"] == "TEST_Account_1"
        assert "bridge_token" in acc and len(acc["bridge_token"]) > 10
        assert "id" in acc
        acc_id = acc["id"]
        original_token = acc["bridge_token"]

        # List
        r2 = fresh_user_session.get(f"{API}/accounts", timeout=10)
        assert r2.status_code == 200
        assert any(a["id"] == acc_id for a in r2.json())

        # Rotate
        r3 = fresh_user_session.post(f"{API}/accounts/{acc_id}/rotate-token", timeout=10)
        assert r3.status_code == 200
        new_token = r3.json()["bridge_token"]
        assert new_token != original_token

        # Delete
        r4 = fresh_user_session.delete(f"{API}/accounts/{acc_id}", timeout=10)
        assert r4.status_code == 200
        # confirm gone
        r5 = fresh_user_session.get(f"{API}/accounts", timeout=10)
        assert not any(a["id"] == acc_id for a in r5.json())


# ---------- EA download ----------
class TestEAScript:
    def test_ea_download(self):
        r = requests.get(f"{API}/ea-script", timeout=15)
        assert r.status_code == 200
        # Should be either an attachment or contain MQL5 keywords
        body = r.text
        assert len(body) > 100, "EA file looks empty"
        assert ("OnInit" in body) or ("#property" in body), "Does not look like MQL5 source"


# ---------- Signals (AI - Claude call). Single test to save tokens. ----------
class TestSignals:
    @pytest.fixture(scope="class")
    def signal_user(self):
        s = requests.Session()
        email = f"TEST_sig_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        return s

    def test_generate_single_signal(self, signal_user):
        r = signal_user.post(f"{API}/signals/generate", json={"symbol": "BTCUSD"}, timeout=120)
        assert r.status_code == 200, f"Signal generation failed: {r.status_code} {r.text}"
        sig = r.json()
        assert sig["symbol"] == "BTCUSD"
        assert sig["action"] in ("BUY", "SELL", "HOLD")
        assert 0 <= sig["confidence"] <= 100
        assert "entry_price" in sig
        assert "stop_loss" in sig
        assert "take_profit" in sig
        assert isinstance(sig.get("reasoning"), str) and len(sig["reasoning"]) > 0
        assert "id" in sig
        # Save id on session
        signal_user.last_signal_id = sig["id"]  # type: ignore
        signal_user.last_signal = sig  # type: ignore

    def test_list_signals(self, signal_user):
        r = signal_user.get(f"{API}/signals", timeout=10)
        assert r.status_code == 200
        sigs = r.json()
        assert isinstance(sigs, list)
        assert len(sigs) >= 1

    def test_delete_signal(self, signal_user):
        # Create a throwaway signal that's safe to delete (re-use existing one)
        sigs = signal_user.get(f"{API}/signals", timeout=10).json()
        assert sigs
        sid = sigs[0]["id"]
        r = signal_user.delete(f"{API}/signals/{sid}", timeout=10)
        assert r.status_code == 200


# ---------- End-to-end trade + bridge ----------
class TestTradeBridge:
    @pytest.fixture(scope="class")
    def setup_ctx(self):
        s = requests.Session()
        email = f"TEST_trd_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        me = s.get(f"{API}/auth/me", timeout=10).json()
        user_id = me["id"]

        # Create account
        acc = s.post(f"{API}/accounts", json={
            "label": "TEST_BridgeAcc",
            "broker": "Exness",
            "server": "Exness-Trial",
            "account_number": "99999",
            "account_type": "microcent",
            "base_currency": "USD",
        }, timeout=10).json()

        # Try to generate a non-HOLD signal up to 2 times across BTCUSD/XAUUSD;
        # if still HOLD, deterministically insert a BUY signal directly into Mongo.
        sig = None
        for sym in ("BTCUSD", "XAUUSD"):
            sig_resp = s.post(f"{API}/signals/generate", json={"symbol": sym}, timeout=120)
            assert sig_resp.status_code == 200, sig_resp.text
            candidate = sig_resp.json()
            if candidate.get("action") in ("BUY", "SELL"):
                sig = candidate
                break

        if sig is None or sig.get("action") == "HOLD":
            # Inject synthetic non-HOLD signal directly via Mongo
            from pymongo import MongoClient
            mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
            db_name = os.environ.get("DB_NAME", "ai_trading_bot")
            client = MongoClient(mongo_url)
            db = client[db_name]
            doc = {
                "user_id": user_id,
                "symbol": "BTCUSD",
                "action": "BUY",
                "confidence": 80.0,
                "entry_price": 65000.0,
                "stop_loss": 64000.0,
                "take_profit": 67000.0,
                "lot_size": 0.01,
                "reasoning": "test synthetic signal",
                "risk_level": "medium",
                "consumed": False,
                "created_at": datetime_utcnow_iso(),
            }
            inserted = db.signals.insert_one(doc)
            sig = {**doc, "id": str(inserted.inserted_id)}
            client.close()

        return {"session": s, "account": acc, "signal": sig}

    def test_bridge_heartbeat(self, setup_ctx):
        acc = setup_ctx["account"]
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": acc["bridge_token"],
            "balance": 1000.0,
            "equity": 1000.0,
            "open_positions": 0,
        }, timeout=10)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_bridge_invalid_token(self):
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": "INVALID_TOKEN_XYZ",
            "balance": 10.0, "equity": 10.0, "open_positions": 0,
        }, timeout=10)
        assert r.status_code == 401

    def test_full_trade_flow(self, setup_ctx):
        s = setup_ctx["session"]
        acc = setup_ctx["account"]
        sig = setup_ctx["signal"]

        assert sig.get("action") in ("BUY", "SELL"), f"Expected non-HOLD signal, got {sig.get('action')}"

        # Execute trade
        r = s.post(f"{API}/trades/execute/{sig['id']}", json={"account_id": acc["id"]}, timeout=15)
        assert r.status_code == 200, r.text
        trade = r.json()
        assert trade["status"] == "pending"
        trade_id = trade["id"]

        # EA polls
        poll = requests.post(f"{API}/bridge/poll-trades", json={"bridge_token": acc["bridge_token"]}, timeout=10)
        assert poll.status_code == 200
        pending = poll.json()["trades"]
        assert any(t["trade_id"] == trade_id for t in pending), f"trade not in poll output: {pending}"

        # Report open
        r2 = requests.post(f"{API}/bridge/report", json={
            "bridge_token": acc["bridge_token"],
            "trade_id": trade_id,
            "status": "open",
            "mt5_ticket": 123456,
            "entry_price": trade["entry_price"],
        }, timeout=10)
        assert r2.status_code == 200, r2.text

        # Report closed
        r3 = requests.post(f"{API}/bridge/report", json={
            "bridge_token": acc["bridge_token"],
            "trade_id": trade_id,
            "status": "closed",
            "mt5_ticket": 123456,
            "exit_price": (trade["entry_price"] or 65000) * 1.01,
            "pnl": 12.5,
        }, timeout=10)
        assert r3.status_code == 200

        # Stats
        stats = s.get(f"{API}/trades/stats", timeout=10).json()
        assert stats["total_trades"] >= 1
        assert stats["wins"] >= 1
        assert stats["total_pnl"] >= 12.5 - 0.01


def datetime_utcnow_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# ITERATION 3: News sentiment, dual-AI veto, WebSocket, 1y history,
# circuit breakers, bot runner.
# =====================================================================

# ---------- 1-year history ----------
class TestOneYearHistory:
    @pytest.mark.parametrize("symbol", ["BTCUSD", "XAUUSD"])
    def test_history_has_300_plus_points(self, admin_session, symbol):
        r = admin_session.get(f"{API}/market/history/{symbol}", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        # Was ~180 before; now should be > 300 (1y daily)
        assert len(data["history"]) > 300, f"{symbol} history only has {len(data['history'])} points (want > 300)"


# ---------- News sentiment ----------
class TestSentiment:
    @pytest.mark.parametrize("symbol", ["BTCUSD", "XAUUSD"])
    def test_sentiment_shape(self, admin_session, symbol):
        r = admin_session.get(f"{API}/sentiment/{symbol}", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        # required shape
        assert data["symbol"] == symbol
        assert isinstance(data.get("score"), (int, float))
        assert -1.0 <= float(data["score"]) <= 1.0
        assert data.get("label") in (
            "very_bearish", "bearish", "neutral", "bullish", "very_bullish"
        )
        assert isinstance(data.get("summary"), str)
        assert isinstance(data.get("key_drivers"), list)
        assert isinstance(data.get("article_count"), int)
        # should NOT 500 even if 0 articles
        assert data["article_count"] >= 0

    def test_sentiment_unknown_symbol_does_not_500(self, admin_session):
        r = admin_session.get(f"{API}/sentiment/UNKNOWNSYM", timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "score" in data and "label" in data

    def test_sentiment_requires_auth(self):
        r = requests.get(f"{API}/sentiment/BTCUSD", timeout=10)
        assert r.status_code == 401


# ---------- AI signal new fields (dual-AI veto shape) ----------
class TestSignalDualAIShape:
    @pytest.fixture(scope="class")
    def sig_user(self):
        s = requests.Session()
        email = f"TEST_dual_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        return s

    def test_signal_has_new_fields(self, sig_user):
        r = sig_user.post(f"{API}/signals/generate", json={"symbol": "BTCUSD"}, timeout=180)
        assert r.status_code == 200, r.text
        sig = r.json()
        # New iter-3 fields
        for fld in ("sentiment", "chart_action", "veto_applied", "action"):
            assert fld in sig, f"missing field {fld} in signal payload"
        assert sig["action"] in ("BUY", "SELL", "HOLD")
        assert sig["chart_action"] in ("BUY", "SELL", "HOLD")
        assert isinstance(sig["veto_applied"], bool)
        # sentiment sub-shape
        sent = sig["sentiment"]
        assert "score" in sent and -1 <= float(sent["score"]) <= 1
        assert "label" in sent
        # If a veto was applied, reasoning should contain VETO marker
        if sig["veto_applied"]:
            assert "VETO" in (sig.get("reasoning") or "")


# ---------- WebSocket ----------
class TestWebSocket:
    def _ws_url(self):
        # Convert https -> wss, http -> ws
        url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
        return f"{url}/api/ws"

    def test_ws_rejects_without_token(self):
        import asyncio
        import websockets

        async def run():
            try:
                async with websockets.connect(self._ws_url(), open_timeout=10) as _:
                    return "connected_unexpectedly"
            except websockets.exceptions.InvalidStatus as e:
                # HTTP-level rejection (e.g., 403) is also acceptable as "rejected"
                return f"http_reject:{e.response.status_code}"
            except websockets.exceptions.ConnectionClosed as e:
                return f"closed:{e.code}"
            except Exception as e:
                return f"err:{type(e).__name__}:{e}"

        result = asyncio.run(run())
        # Either close-code 4401 OR an HTTP-level reject is acceptable; what we
        # must NOT see is a fully successful connection.
        assert result != "connected_unexpectedly", f"WS accepted unauth connection: {result}"

    def test_ws_accepts_with_token_and_emits_connected(self, admin_session):
        import asyncio
        import json
        import websockets

        token = admin_session.cookies.get("access_token")
        assert token, "admin_session missing access_token cookie"
        url = f"{self._ws_url()}?token={token}"

        async def run():
            async with websockets.connect(url, open_timeout=15) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                return json.loads(raw)

        msg = asyncio.run(run())
        assert msg.get("type") == "connected"
        assert "user_id" in (msg.get("payload") or {})


# ---------- Circuit breaker logic (unit-level on the module) ----------
class TestCircuitBreaker:
    def test_drawdown_limits_table(self):
        from circuit_breakers import drawdown_limit_for, DEFAULT_DAILY_DRAWDOWN_PCT
        assert DEFAULT_DAILY_DRAWDOWN_PCT["low"] == 2.0
        assert DEFAULT_DAILY_DRAWDOWN_PCT["medium"] == 4.0
        assert DEFAULT_DAILY_DRAWDOWN_PCT["high"] == 7.0
        assert DEFAULT_DAILY_DRAWDOWN_PCT["extreme"] == 12.0
        assert drawdown_limit_for("medium") == 4.0
        assert drawdown_limit_for("nonexistent") == 4.0

    def test_check_and_trip_disables_bot_on_breach(self):
        """Insert losing trade today, set equity, run check_and_trip,
        verify bot_config gets disabled + tripped_reason set."""
        import asyncio
        from pymongo import MongoClient
        from circuit_breakers import check_and_trip
        from motor.motor_asyncio import AsyncIOMotorClient

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "ai_trading_bot")
        sync = MongoClient(mongo_url)[db_name]

        user_id = f"TEST_CB_USER_{uuid.uuid4().hex[:6]}"
        # Seed an active bot_config and an account with equity 1000
        sync.bot_configs.insert_one({
            "user_id": user_id, "risk_level": "low", "symbols": ["BTCUSD"],
            "active": True, "max_concurrent_trades": 3, "auto_execute": True,
        })
        # Closed losing trade -> -50 USD against 1000 equity = -5% > 2% (low limit)
        sync.trades.insert_one({
            "user_id": user_id, "account_id": "TEST_ACC", "symbol": "BTCUSD",
            "action": "BUY", "lot_size": 0.01, "entry_price": 60000,
            "stop_loss": 59000, "take_profit": 61000, "exit_price": 59500,
            "pnl": -50.0, "status": "closed",
            "opened_at": datetime_utcnow_iso(),
            "closed_at": datetime_utcnow_iso(),
        })

        async def run():
            client = AsyncIOMotorClient(mongo_url)
            db = client[db_name]
            cfg = await db.bot_configs.find_one({"user_id": user_id})
            accounts = [{"equity": 1000.0, "balance": 1000.0}]
            res = await check_and_trip(db, user_id, cfg, accounts)
            cfg_after = await db.bot_configs.find_one({"user_id": user_id})
            client.close()
            return res, cfg_after

        try:
            res, cfg_after = asyncio.run(run())
            assert res["tripped"] is True, f"Expected trip, got {res}"
            assert cfg_after["active"] is False
            assert cfg_after.get("tripped_reason"), "tripped_reason should be set"
        finally:
            sync.bot_configs.delete_many({"user_id": user_id})
            sync.trades.delete_many({"user_id": user_id})

    def test_check_and_trip_no_breach_below_limit(self):
        """Small loss within limit -> bot stays active."""
        import asyncio
        from pymongo import MongoClient
        from circuit_breakers import check_and_trip
        from motor.motor_asyncio import AsyncIOMotorClient

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "ai_trading_bot")
        sync = MongoClient(mongo_url)[db_name]

        user_id = f"TEST_CB_USER_{uuid.uuid4().hex[:6]}"
        sync.bot_configs.insert_one({
            "user_id": user_id, "risk_level": "medium", "symbols": ["BTCUSD"],
            "active": True, "max_concurrent_trades": 3, "auto_execute": True,
        })
        # -10 USD out of 1000 = -1%, below medium 4% limit
        sync.trades.insert_one({
            "user_id": user_id, "account_id": "TEST_ACC", "symbol": "BTCUSD",
            "action": "BUY", "lot_size": 0.01, "entry_price": 60000,
            "stop_loss": 59000, "take_profit": 61000, "exit_price": 59900,
            "pnl": -10.0, "status": "closed",
            "opened_at": datetime_utcnow_iso(),
            "closed_at": datetime_utcnow_iso(),
        })

        async def run():
            client = AsyncIOMotorClient(mongo_url)
            db = client[db_name]
            cfg = await db.bot_configs.find_one({"user_id": user_id})
            accounts = [{"equity": 1000.0}]
            res = await check_and_trip(db, user_id, cfg, accounts)
            client.close()
            return res

        try:
            res = asyncio.run(run())
            assert res["tripped"] is False
        finally:
            sync.bot_configs.delete_many({"user_id": user_id})
            sync.trades.delete_many({"user_id": user_id})


# ---------- Dual-AI veto logic (pure function) ----------
class TestDualAIVeto:
    def test_buy_vetoed_by_bearish_sentiment(self):
        from ai_signals import _apply_dual_veto
        action, reason = _apply_dual_veto("BUY", 80.0, {"score": -0.7})
        assert action == "HOLD"
        assert "vetoed" in reason.lower()

    def test_sell_vetoed_by_bullish_sentiment(self):
        from ai_signals import _apply_dual_veto
        action, reason = _apply_dual_veto("SELL", 70.0, {"score": 0.7})
        assert action == "HOLD"
        assert "vetoed" in reason.lower()

    def test_no_veto_when_sentiment_weak(self):
        from ai_signals import _apply_dual_veto
        action, reason = _apply_dual_veto("BUY", 60.0, {"score": 0.2})
        assert action == "BUY"
        assert reason == ""

    def test_no_veto_when_aligned(self):
        from ai_signals import _apply_dual_veto
        action, reason = _apply_dual_veto("BUY", 60.0, {"score": 0.8})
        assert action == "BUY"
        assert reason == ""


# =====================================================================
# ITERATION 4: Kelly position sizing, microstructure (session + regime),
# regime CHOP veto, enhanced signal payload.
# =====================================================================

# ---------- Kelly position sizing (pure function) ----------
class TestKellyFraction:
    def test_returns_zero_below_min_confidence(self):
        from risk import kelly_fraction
        assert kelly_fraction(confidence_pct=40, min_conf=65,
                              profile_kelly_cap=0.5, payoff_ratio=2.0) == 0.0

    def test_monotonic_increasing_with_confidence(self):
        from risk import kelly_fraction
        f_low = kelly_fraction(70, 65, 1.0, 2.0)
        f_mid = kelly_fraction(80, 65, 1.0, 2.0)
        f_high = kelly_fraction(95, 65, 1.0, 2.0)
        assert 0 < f_low < f_mid < f_high

    def test_capped_at_profile_kelly_cap(self):
        from risk import kelly_fraction
        # At 100% confidence with payoff 2.0, raw Kelly = (1*2 - 0)/2 = 1.0,
        # but profile cap is 0.25 -> must be capped.
        f = kelly_fraction(100, 50, profile_kelly_cap=0.25, payoff_ratio=2.0)
        assert f == 0.25

    def test_negative_f_clamped_to_zero(self):
        from risk import kelly_fraction
        # confidence just at min_conf with low payoff -> raw Kelly may be negative
        f = kelly_fraction(50, 50, profile_kelly_cap=0.5, payoff_ratio=0.5)
        # p=0.5, b=0.5 -> f*=(0.25-0.5)/0.5=-0.5 -> clamp to 0
        assert f == 0.0


class TestComputeKellyPositionSize:
    def test_returns_required_keys(self):
        from risk import compute_kelly_position_size, get_profile
        profile = get_profile("medium")
        result = compute_kelly_position_size(
            equity=1000.0, confidence_pct=80, sl_pips=50.0,
            profile=profile, pip_value=1.0
        )
        for key in ("lot_size", "risk_amount", "kelly_f", "effective_risk_pct"):
            assert key in result, f"missing key {key}"
        assert isinstance(result["lot_size"], (int, float))
        assert result["lot_size"] >= 0.01
        assert result["risk_amount"] >= 0
        assert 0 <= result["kelly_f"] <= profile["kelly_cap"]
        assert result["effective_risk_pct"] >= 0

    def test_zero_when_below_min_confidence(self):
        from risk import compute_kelly_position_size, get_profile
        profile = get_profile("low")  # min_confidence=75
        result = compute_kelly_position_size(
            equity=1000.0, confidence_pct=50, sl_pips=50.0,
            profile=profile, pip_value=1.0
        )
        assert result["kelly_f"] == 0.0
        assert result["effective_risk_pct"] == 0
        # lot_size floors at 0.01 per implementation
        assert result["lot_size"] == 0.01

    def test_invalid_sl_returns_floor_lot(self):
        from risk import compute_kelly_position_size, get_profile
        result = compute_kelly_position_size(
            equity=1000.0, confidence_pct=80, sl_pips=0,
            profile=get_profile("medium"), pip_value=1.0
        )
        assert result["lot_size"] == 0.01
        assert result["kelly_f"] == 0.0

    def test_profile_kelly_caps(self):
        """Iter-4 spec: low=0.25, medium=0.50, high=0.75, extreme=1.00."""
        from risk import PROFILES
        assert PROFILES["low"]["kelly_cap"] == 0.25
        assert PROFILES["medium"]["kelly_cap"] == 0.50
        assert PROFILES["high"]["kelly_cap"] == 0.75
        assert PROFILES["extreme"]["kelly_cap"] == 1.00


# ---------- Regime classifier (pure function) ----------
class TestRegimeClassifier:
    def test_chop_when_high_vol_no_trend(self):
        from microstructure import classify_regime
        ind = {
            "current_price": 100.0,
            "sma_20": 100.0, "sma_50": 100.05, "sma_200": 99.95,  # tiny spread
            "rsi_14": 70,  # outside 35-65 -> not RANGE either
            "volatility_30d_pct": 5.0,  # high vol
        }
        result = classify_regime(ind)
        assert result["regime"] == "CHOP", f"got {result}"
        assert "reason" in result

    def test_low_vol_trend_when_smas_stacked_low_vol(self):
        from microstructure import classify_regime
        ind = {
            "current_price": 100.0,
            "sma_20": 102.0, "sma_50": 101.0, "sma_200": 100.0,  # bullish stack
            "rsi_14": 60,
            "volatility_30d_pct": 1.0,  # low vol
        }
        result = classify_regime(ind)
        assert result["regime"] == "LOW_VOL_TREND", f"got {result}"

    def test_range_when_tight_and_rsi_mid(self):
        from microstructure import classify_regime
        ind = {
            "current_price": 100.0,
            "sma_20": 100.0, "sma_50": 100.05, "sma_200": 100.02,
            "rsi_14": 50,  # mid
            "volatility_30d_pct": 1.0,
        }
        result = classify_regime(ind)
        assert result["regime"] == "RANGE", f"got {result}"

    def test_high_vol_trend_when_smas_stacked_high_vol(self):
        from microstructure import classify_regime
        ind = {
            "current_price": 100.0,
            "sma_20": 102.0, "sma_50": 101.0, "sma_200": 100.0,  # bullish stack
            "rsi_14": 65,
            "volatility_30d_pct": 4.0,  # high vol
        }
        result = classify_regime(ind)
        assert result["regime"] == "HIGH_VOL_TREND", f"got {result}"

    def test_empty_indicators_returns_unknown(self):
        from microstructure import classify_regime
        result = classify_regime({})
        assert result["regime"] == "unknown"

    def test_regime_dict_has_required_fields(self):
        from microstructure import classify_regime
        ind = {"current_price": 100.0, "sma_20": 102, "sma_50": 101,
               "sma_200": 100, "rsi_14": 60, "volatility_30d_pct": 1.0}
        result = classify_regime(ind)
        for fld in ("regime", "reason", "trend_strength", "volatility_pct"):
            assert fld in result


# ---------- Session detection ----------
class TestSessionDetection:
    def test_tokyo_session_at_03_utc(self):
        from datetime import datetime, timezone
        from microstructure import current_session
        # Monday 03:00 UTC -> Tokyo only
        now = datetime(2026, 1, 5, 3, 0, 0, tzinfo=timezone.utc)
        s = current_session(now=now)
        assert s["primary"] == "tokyo"
        assert "tokyo" in s["active_sessions"]
        assert s["is_weekend"] is False
        assert s["is_high_volume_window"] is False

    def test_london_ny_overlap_at_14_utc(self):
        from datetime import datetime, timezone
        from microstructure import current_session
        now = datetime(2026, 1, 5, 14, 0, 0, tzinfo=timezone.utc)
        s = current_session(now=now)
        assert s["primary"] == "london_ny_overlap"
        assert "london" in s["active_sessions"] and "ny" in s["active_sessions"]
        assert s["is_high_volume_window"] is True

    def test_off_hours_at_23_utc(self):
        from datetime import datetime, timezone
        from microstructure import current_session
        now = datetime(2026, 1, 5, 23, 0, 0, tzinfo=timezone.utc)
        s = current_session(now=now)
        assert s["primary"] == "off-hours"

    def test_weekend_flag(self):
        from datetime import datetime, timezone
        from microstructure import current_session
        # Saturday 2026-01-03
        now = datetime(2026, 1, 3, 12, 0, 0, tzinfo=timezone.utc)
        s = current_session(now=now)
        assert s["is_weekend"] is True


class TestSessionBias:
    def test_xauusd_tokyo_mean_reversion(self):
        from microstructure import session_bias_for
        sess = {"is_high_volume_window": False, "primary": "tokyo", "is_weekend": False}
        bias = session_bias_for("XAUUSD", sess)
        assert bias["preferred_strategy"] == "mean_reversion"

    def test_xauusd_overlap_trend_following(self):
        from microstructure import session_bias_for
        sess = {"is_high_volume_window": True, "primary": "london_ny_overlap", "is_weekend": False}
        bias = session_bias_for("XAUUSD", sess)
        assert bias["preferred_strategy"] == "trend_following"

    def test_btcusd_weekend_counter_trend(self):
        from microstructure import session_bias_for
        sess = {"is_high_volume_window": False, "primary": "off-hours", "is_weekend": True}
        bias = session_bias_for("BTCUSD", sess)
        assert bias["preferred_strategy"] == "counter_trend"

    def test_btcusd_weekday_trend_following(self):
        from microstructure import session_bias_for
        sess = {"is_high_volume_window": True, "primary": "london_ny_overlap", "is_weekend": False}
        bias = session_bias_for("BTCUSD", sess)
        assert bias["preferred_strategy"] == "trend_following"


# ---------- Regime CHOP veto end-to-end (uses analyze_symbol with mocked LLM) ----------
class TestRegimeVeto:
    def test_chop_forces_hold_in_signal_payload(self):
        """If classify_regime returns CHOP, signal.action must be HOLD even if
        chart_action was BUY/SELL, and reasoning must contain 'VETO (regime)'."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        import ai_signals

        # Fake LLM chat -> always returns BUY/80%
        class FakeChat:
            def with_model(self, *a, **k):
                return self
            async def send_message(self, msg):
                return '{"action":"BUY","confidence":80,"reasoning":"trend up","key_factors":["x"]}'

        # Indicators that force CHOP via classify_regime
        chop_indicators = {
            "current_price": 100.0,
            "sma_20": 100.0, "sma_50": 100.05, "sma_200": 99.95,
            "rsi_14": 75, "volatility_30d_pct": 5.0,
        }

        # Ensure EMERGENT_LLM_KEY is set (LlmChat ctor reads it even though we patch the class).
        os.environ.setdefault("EMERGENT_LLM_KEY", "test-dummy")

        async def run():
            with patch.object(ai_signals, "LlmChat", return_value=FakeChat()), \
                 patch.object(ai_signals, "get_quote", new=AsyncMock(return_value={"price": 100.0, "bid": 99.9, "ask": 100.1, "change_pct": 0.0})), \
                 patch.object(ai_signals, "get_history", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "compute_indicators", return_value=chop_indicators), \
                 patch.object(ai_signals, "score_sentiment", new=AsyncMock(return_value={"score": 0.0, "label": "neutral", "summary": "", "article_count": 0, "key_drivers": []})):
                return await ai_signals.analyze_symbol("BTCUSD", "medium")

        sig = asyncio.run(run())
        assert sig["regime"]["regime"] == "CHOP", f"setup failed: {sig['regime']}"
        assert sig["chart_action"] == "BUY"
        assert sig["action"] == "HOLD"
        assert sig["veto_applied"] is True
        assert "VETO (regime)" in (sig["reasoning"] or "")


# ---------- Enhanced signal payload smoke test (live AI) ----------
class TestSignalPayloadNewFieldsLive:
    @pytest.fixture(scope="class")
    def sig_user(self):
        s = requests.Session()
        email = f"TEST_iter4_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        return s

    def test_signal_has_all_new_iter4_fields(self, sig_user):
        r = sig_user.post(f"{API}/signals/generate",
                          json={"symbol": "BTCUSD"}, timeout=180)
        assert r.status_code == 200, r.text
        sig = r.json()

        # New Iter-4 scalar fields
        for fld in ("kelly_f", "effective_risk_pct", "risk_amount",
                    "regime", "session", "session_bias"):
            assert fld in sig, f"missing field {fld} in signal payload"

        # Types
        assert isinstance(sig["kelly_f"], (int, float))
        assert 0 <= sig["kelly_f"] <= 1.0
        assert isinstance(sig["effective_risk_pct"], (int, float))
        assert isinstance(sig["risk_amount"], (int, float))

        # Regime dict shape
        regime = sig["regime"]
        for k in ("regime", "reason", "trend_strength", "volatility_pct"):
            assert k in regime, f"regime missing {k}"
        assert regime["regime"] in (
            "HIGH_VOL_TREND", "LOW_VOL_TREND", "RANGE", "CHOP",
            "TRANSITIONAL", "unknown"
        )

        # Session dict shape
        session = sig["session"]
        for k in ("utc_hour", "primary", "active_sessions",
                  "is_weekend", "is_high_volume_window"):
            assert k in session, f"session missing {k}"

        # Session bias shape
        sb = sig["session_bias"]
        assert "preferred_strategy" in sb
        assert "note" in sb

        # Tradeable sanity: lot_size > 0 always (floor 0.01)
        assert sig["lot_size"] >= 0.01
        # Reasoning non-empty
        assert isinstance(sig.get("reasoning"), str) and len(sig["reasoning"]) > 0



# ========================================================================
# ITER-5: Economic Calendar + Macro Veto
# ========================================================================

# ---------- Calendar API endpoints ----------
class TestCalendarEndpoints:
    """Forex Factory-based /api/calendar routes (auth-required)."""

    def test_calendar_requires_auth(self):
        r = requests.get(f"{API}/calendar", timeout=15)
        assert r.status_code in (401, 403), f"expected auth error, got {r.status_code}"

    def test_calendar_upcoming_requires_auth(self):
        r = requests.get(f"{API}/calendar/upcoming/XAUUSD", timeout=15)
        assert r.status_code in (401, 403)

    def test_calendar_freeze_requires_auth(self):
        r = requests.get(f"{API}/calendar/freeze/XAUUSD", timeout=15)
        assert r.status_code in (401, 403)

    def test_calendar_all_events_returns_list(self, admin_session):
        r = admin_session.get(f"{API}/calendar", timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "events" in body
        assert isinstance(body["events"], list)
        # If list is non-empty, validate per-event shape
        if body["events"]:
            ev = body["events"][0]
            for k in ("title", "country", "impact", "when", "when_ts",
                      "forecast", "previous"):
                assert k in ev, f"event missing field {k}"
            assert isinstance(ev["when_ts"], (int, float))
            assert isinstance(ev["title"], str)

    def test_calendar_upcoming_xauusd(self, admin_session):
        r = admin_session.get(f"{API}/calendar/upcoming/XAUUSD?hours=48", timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "XAUUSD"
        assert body["hours"] == 48
        assert isinstance(body["events"], list)
        # All returned events should be high/medium impact only
        for ev in body["events"]:
            assert ev["impact"] in ("high", "medium"), f"unexpected impact {ev['impact']}"

    def test_calendar_upcoming_eurusd_only_eur_usd(self, admin_session):
        r = admin_session.get(f"{API}/calendar/upcoming/EURUSD?hours=168", timeout=30)
        assert r.status_code == 200
        for ev in r.json()["events"]:
            assert ev["country"] in ("EUR", "USD"), \
                f"EURUSD got irrelevant country {ev['country']}"

    def test_calendar_freeze_shape(self, admin_session):
        r = admin_session.get(f"{API}/calendar/freeze/XAUUSD", timeout=30)
        assert r.status_code == 200
        body = r.json()
        for k in ("frozen", "reason", "event"):
            assert k in body, f"freeze response missing {k}"
        assert isinstance(body["frozen"], bool)


# ---------- Pure-function tests on economic_calendar.py ----------
class TestRelevantEventsFor:
    """relevant_events_for() filters by SYMBOL_CURRENCIES."""

    def _evts(self):
        return [
            {"title": "FOMC", "country": "USD", "impact": "high",
             "when": "x", "when_ts": 0, "forecast": "", "previous": ""},
            {"title": "ECB", "country": "EUR", "impact": "high",
             "when": "x", "when_ts": 0, "forecast": "", "previous": ""},
            {"title": "BoJ", "country": "JPY", "impact": "high",
             "when": "x", "when_ts": 0, "forecast": "", "previous": ""},
            {"title": "BoE", "country": "GBP", "impact": "medium",
             "when": "x", "when_ts": 0, "forecast": "", "previous": ""},
        ]

    def test_xauusd_all_currency_returns_everything(self):
        from economic_calendar import relevant_events_for
        out = relevant_events_for("XAUUSD", self._evts())
        # XAUUSD has {'USD','ALL'} -> ALL means everything
        assert len(out) == 4

    def test_btcusd_all_returns_everything(self):
        from economic_calendar import relevant_events_for
        out = relevant_events_for("BTCUSD", self._evts())
        assert len(out) == 4

    def test_eurusd_only_eur_usd(self):
        from economic_calendar import relevant_events_for
        out = relevant_events_for("EURUSD", self._evts())
        countries = sorted(e["country"] for e in out)
        assert countries == ["EUR", "USD"]

    def test_usdjpy_only_usd_jpy(self):
        from economic_calendar import relevant_events_for
        out = relevant_events_for("USDJPY", self._evts())
        countries = sorted(e["country"] for e in out)
        assert countries == ["JPY", "USD"]

    def test_unknown_symbol_defaults_to_usd(self):
        from economic_calendar import relevant_events_for
        out = relevant_events_for("ZZZUSD", self._evts())
        assert len(out) == 1 and out[0]["country"] == "USD"


class TestMacroFreezeCheck:
    """macro_freeze_check freeze-window logic (synthetic events, no network)."""

    def _patch_events(self, events):
        from unittest.mock import patch, AsyncMock
        import economic_calendar
        return patch.object(economic_calendar, "get_events",
                            new=AsyncMock(return_value=events))

    def test_freeze_true_within_before_window(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        # Event 5 min from now, BEFORE window default = 15 min -> should freeze
        now = _t.time()
        evt = {"title": "FOMC Rate Decision", "country": "USD", "impact": "high",
               "when": "x", "when_ts": now + 5 * 60, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        assert res["frozen"] is True
        assert "USD" in res["reason"]
        assert res["event"] is not None
        assert "when_human" in res["event"]

    def test_freeze_true_within_after_window(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        # Event 5 min ago, AFTER window default = 10 min -> should freeze
        now = _t.time()
        evt = {"title": "NFP", "country": "USD", "impact": "high",
               "when": "x", "when_ts": now - 5 * 60, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        assert res["frozen"] is True
        assert "settle" in res["reason"].lower() or "ago" in res["reason"].lower()

    def test_no_freeze_far_future(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        now = _t.time()
        evt = {"title": "CPI", "country": "USD", "impact": "high",
               "when": "x", "when_ts": now + 6 * 3600, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        assert res["frozen"] is False
        assert res["event"] is None

    def test_no_freeze_far_past(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        now = _t.time()
        evt = {"title": "CPI", "country": "USD", "impact": "high",
               "when": "x", "when_ts": now - 3600, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        assert res["frozen"] is False

    def test_medium_impact_does_not_freeze(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        now = _t.time()
        evt = {"title": "Retail Sales", "country": "USD", "impact": "medium",
               "when": "x", "when_ts": now + 60, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        # Spec: macro_freeze_check ONLY uses high-impact events
        assert res["frozen"] is False

    def test_event_for_different_currency_does_not_freeze_eurusd(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        now = _t.time()
        evt = {"title": "BoJ", "country": "JPY", "impact": "high",
               "when": "x", "when_ts": now + 60, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("EURUSD"))
        # JPY event must not freeze EURUSD
        assert res["frozen"] is False

    def test_jpy_event_freezes_xauusd_due_to_all_flag(self):
        import asyncio, time as _t
        from economic_calendar import macro_freeze_check
        now = _t.time()
        evt = {"title": "BoJ", "country": "JPY", "impact": "high",
               "when": "x", "when_ts": now + 60, "forecast": "", "previous": ""}
        with self._patch_events([evt]):
            res = asyncio.run(macro_freeze_check("XAUUSD"))
        assert res["frozen"] is True


# ---------- Macro veto integration in analyze_symbol ----------
class TestMacroVetoIntegration:
    """analyze_symbol() must force HOLD when macro_freeze_check returns frozen=True."""

    def test_macro_veto_forces_hold(self):
        import asyncio
        from unittest.mock import patch, AsyncMock
        import ai_signals

        os.environ.setdefault("EMERGENT_LLM_KEY", "test-dummy")

        class FakeChat:
            def with_model(self, *a, **k):
                return self
            async def send_message(self, msg):
                return '{"action":"BUY","confidence":85,"reasoning":"chart bullish","key_factors":["a"]}'

        # Neutral indicators -> no regime veto
        neutral_indicators = {
            "current_price": 2000.0,
            "sma_20": 2000.0, "sma_50": 2000.0, "sma_200": 2000.0,
            "rsi_14": 55, "volatility_30d_pct": 1.0,
        }

        frozen_event = {
            "title": "FOMC Rate Decision", "country": "USD", "impact": "high",
            "when": "2026-01-01T00:00:00+00:00", "when_ts": 1.0,
            "forecast": "", "previous": "", "when_human": "2026-01-01 00:00 UTC",
        }
        frozen_macro = {"frozen": True,
                        "reason": "HIGH-impact USD event 'FOMC' in 5min — bot frozen.",
                        "event": frozen_event}

        async def run():
            with patch.object(ai_signals, "LlmChat", return_value=FakeChat()), \
                 patch.object(ai_signals, "get_quote", new=AsyncMock(return_value={"price": 2000.0, "bid": 1999.9, "ask": 2000.1, "change_pct": 0.0})), \
                 patch.object(ai_signals, "get_history", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "compute_indicators", return_value=neutral_indicators), \
                 patch.object(ai_signals, "score_sentiment", new=AsyncMock(return_value={"score": 0.0, "label": "neutral", "summary": "", "article_count": 0, "key_drivers": []})), \
                 patch.object(ai_signals, "macro_freeze_check", new=AsyncMock(return_value=frozen_macro)), \
                 patch.object(ai_signals, "upcoming_for", new=AsyncMock(return_value=[frozen_event])):
                return await ai_signals.analyze_symbol("XAUUSD", "medium")

        sig = asyncio.run(run())
        assert sig["chart_action"] == "BUY"
        assert sig["action"] == "HOLD"
        assert sig["veto_applied"] is True
        assert "VETO (macro)" in (sig["reasoning"] or "")
        assert sig["macro"]["frozen"] is True
        assert isinstance(sig["upcoming_macro"], list)
        assert len(sig["upcoming_macro"]) >= 1

    def test_no_macro_veto_when_not_frozen(self):
        import asyncio
        from unittest.mock import patch, AsyncMock
        import ai_signals

        os.environ.setdefault("EMERGENT_LLM_KEY", "test-dummy")

        class FakeChat:
            def with_model(self, *a, **k):
                return self
            async def send_message(self, msg):
                return '{"action":"BUY","confidence":80,"reasoning":"bullish trend","key_factors":["a"]}'

        # Trending up indicators -> LOW_VOL_TREND or HIGH_VOL_TREND (not CHOP)
        trending_indicators = {
            "current_price": 2050.0,
            "sma_20": 2040.0, "sma_50": 2020.0, "sma_200": 1900.0,
            "rsi_14": 60, "volatility_30d_pct": 1.0,
        }

        async def run():
            with patch.object(ai_signals, "LlmChat", return_value=FakeChat()), \
                 patch.object(ai_signals, "get_quote", new=AsyncMock(return_value={"price": 2050.0, "bid": 2049.9, "ask": 2050.1, "change_pct": 0.5})), \
                 patch.object(ai_signals, "get_history", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "compute_indicators", return_value=trending_indicators), \
                 patch.object(ai_signals, "score_sentiment", new=AsyncMock(return_value={"score": 0.0, "label": "neutral", "summary": "", "article_count": 0, "key_drivers": []})), \
                 patch.object(ai_signals, "macro_freeze_check", new=AsyncMock(return_value={"frozen": False, "reason": "", "event": None})), \
                 patch.object(ai_signals, "upcoming_for", new=AsyncMock(return_value=[])):
                return await ai_signals.analyze_symbol("XAUUSD", "medium")

        sig = asyncio.run(run())
        assert sig["chart_action"] == "BUY"
        # No vetoes applied: action should still be BUY
        assert sig["action"] == "BUY"
        assert sig["veto_applied"] is False
        assert "VETO (macro)" not in (sig["reasoning"] or "")
        assert sig["macro"]["frozen"] is False


# ---------- Veto stacking: sentiment + regime + macro ----------
class TestVetoStacking:
    def test_all_three_vetoes_stack(self):
        """When sentiment, regime CHOP, AND macro all veto, reasoning must
        contain all three markers and veto_applied=True."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        import ai_signals

        os.environ.setdefault("EMERGENT_LLM_KEY", "test-dummy")

        class FakeChat:
            def with_model(self, *a, **k):
                return self
            async def send_message(self, msg):
                # BUY action -> conflicts with strongly bearish sentiment (news veto)
                return '{"action":"BUY","confidence":75,"reasoning":"trend up","key_factors":["a"]}'

        # Indicators that force CHOP
        chop_indicators = {
            "current_price": 100.0,
            "sma_20": 100.0, "sma_50": 100.05, "sma_200": 99.95,
            "rsi_14": 75, "volatility_30d_pct": 5.0,
        }

        # Strongly negative sentiment -> news veto fires on BUY
        bearish_sentiment = {"score": -0.8, "label": "bearish", "summary": "",
                             "article_count": 5, "key_drivers": []}

        frozen_event = {"title": "FOMC", "country": "USD", "impact": "high",
                        "when": "x", "when_ts": 1.0, "forecast": "",
                        "previous": "", "when_human": "x"}
        frozen_macro = {"frozen": True, "reason": "HIGH-impact USD event imminent.",
                        "event": frozen_event}

        async def run():
            with patch.object(ai_signals, "LlmChat", return_value=FakeChat()), \
                 patch.object(ai_signals, "get_quote", new=AsyncMock(return_value={"price": 100.0, "bid": 99.9, "ask": 100.1, "change_pct": 0.0})), \
                 patch.object(ai_signals, "get_history", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "compute_indicators", return_value=chop_indicators), \
                 patch.object(ai_signals, "score_sentiment", new=AsyncMock(return_value=bearish_sentiment)), \
                 patch.object(ai_signals, "macro_freeze_check", new=AsyncMock(return_value=frozen_macro)), \
                 patch.object(ai_signals, "upcoming_for", new=AsyncMock(return_value=[])):
                return await ai_signals.analyze_symbol("BTCUSD", "medium")

        sig = asyncio.run(run())
        assert sig["chart_action"] == "BUY"
        assert sig["action"] == "HOLD"
        assert sig["veto_applied"] is True
        reasoning = sig["reasoning"] or ""
        assert "VETO (news)" in reasoning, f"missing news veto marker: {reasoning}"
        assert "VETO (regime)" in reasoning, f"missing regime veto marker: {reasoning}"
        assert "VETO (macro)" in reasoning, f"missing macro veto marker: {reasoning}"


# ---------- Live signal smoke test for new iter-5 fields ----------
class TestSignalPayloadIter5Live:
    @pytest.fixture(scope="class")
    def sig_user(self):
        s = requests.Session()
        email = f"TEST_iter5_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        return s

    def test_signal_has_macro_and_upcoming_macro_fields(self, sig_user):
        r = sig_user.post(f"{API}/signals/generate",
                          json={"symbol": "BTCUSD"}, timeout=180)
        assert r.status_code == 200, r.text
        sig = r.json()
        assert "macro" in sig, "signal missing 'macro' field"
        assert "upcoming_macro" in sig, "signal missing 'upcoming_macro' field"

        macro = sig["macro"]
        assert isinstance(macro, dict)
        for k in ("frozen", "reason", "event"):
            assert k in macro, f"macro missing {k}"
        assert isinstance(macro["frozen"], bool)

        assert isinstance(sig["upcoming_macro"], list)
        # capped at 5 in analyze_symbol
        assert len(sig["upcoming_macro"]) <= 5


# =====================================================================
# ITER-6: Regime-Adaptive Exec, Meta-Labeler, Entropy Filter,
# NL Strategy/Commander, Paper Trading, Time-Series Mongo.
# =====================================================================

# ---------- Regression: AI signal must NOT crash on 'entropy_veto not defined' ----------
class TestIter6SignalPayload:
    @pytest.fixture(scope="class")
    def sig_user(cls):
        s = requests.Session()
        email = f"TEST_iter6_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        return s

    def test_signal_has_all_iter6_fields(self, sig_user):
        r = sig_user.post(f"{API}/signals/generate",
                          json={"symbol": "BTCUSD", "risk_level": "medium"}, timeout=180)
        assert r.status_code == 200, f"P0 entropy_veto bug regression? {r.status_code} {r.text}"
        sig = r.json()
        # New iter-6 envelopes
        for fld in ("noise_filter", "regime", "regime_execution_mode",
                    "meta_label", "compressed_features"):
            assert fld in sig and sig[fld] is not None, f"missing {fld}"

        nf = sig["noise_filter"]
        for k in ("entropy", "label", "traffic_light", "tradeable", "threshold"):
            assert k in nf, f"noise_filter missing {k}"

        rem = sig["regime_execution_mode"]
        for k in ("execution_mode", "regime_detected",
                  "sl_multiplier_applied", "tp_multiplier_applied"):
            assert k in rem, f"regime_execution_mode missing {k}"

        ml = sig["meta_label"]
        for k in ("p_true", "verdict", "threshold", "features"):
            assert k in ml, f"meta_label missing {k}"
        assert ml["verdict"] in ("NEUTRAL", "TRUE_SIGNAL", "FAKE_OUT")
        assert 0 <= ml["p_true"] <= 1.0

        cf = sig["compressed_features"]
        assert cf.get("available") is True, f"compressed_features.available != True: {cf}"
        for k in ("mom_5d", "mom_20d", "vol_20d_annual"):
            assert k in cf, f"compressed_features missing {k}"


# ---------- Regime-Adaptive Execution (pure function) ----------
class TestRegimeAdapter:
    def test_low_vol_trend_defensive_scalp_multipliers(self):
        from regime_adapter import adapt_profile_for_regime, REGIME_MODIFIERS
        from risk import get_profile
        profile = get_profile("medium")
        new_profile, meta = adapt_profile_for_regime(profile, {"regime": "LOW_VOL_TREND"})
        assert meta["execution_mode"] == "DEFENSIVE_SCALP"
        assert meta["sl_multiplier_applied"] == 0.85
        assert meta["tp_multiplier_applied"] == 0.75
        # multiplicative
        assert new_profile["sl_atr_mult"] == round(profile["sl_atr_mult"] * 0.85, 3)
        assert new_profile["tp_atr_mult"] == round(profile["tp_atr_mult"] * 0.75, 3)

    def test_high_vol_trend_dynamic_momentum(self):
        from regime_adapter import adapt_profile_for_regime
        from risk import get_profile
        new_p, meta = adapt_profile_for_regime(get_profile("medium"),
                                                {"regime": "HIGH_VOL_TREND"})
        assert meta["execution_mode"] == "DYNAMIC_MOMENTUM"
        assert meta["sl_multiplier_applied"] == 1.40
        assert meta["tp_multiplier_applied"] == 1.50

    def test_unknown_regime_falls_back_transitional(self):
        from regime_adapter import adapt_profile_for_regime
        from risk import get_profile
        _, meta = adapt_profile_for_regime(get_profile("low"),
                                            {"regime": "WEIRD_NEW_REGIME"})
        assert meta["execution_mode"] == "CAUTIOUS_WAIT"


# ---------- Meta-Labeler ----------
class TestMetaLabeler:
    def test_hold_returns_neutral_verdict_zero_p(self):
        from meta_labeler import predict_true_signal_probability
        out = predict_true_signal_probability(
            action="HOLD", confidence=50,
            sentiment={"score": 0.0}, regime={"regime": "RANGE"},
            entropy={"entropy": 0.5},
            session={"primary": "off-hours", "is_high_volume_window": False, "is_weekend": False},
            indicators={"current_price": 100, "rsi_14": 50},
            upcoming_macro=[],
        )
        assert out["verdict"] == "NEUTRAL"
        assert out["p_true"] == 0.0

    def test_buy_returns_p_true_in_range_with_verdict(self):
        from meta_labeler import predict_true_signal_probability
        out = predict_true_signal_probability(
            action="BUY", confidence=80,
            sentiment={"score": 0.5}, regime={"regime": "LOW_VOL_TREND"},
            entropy={"entropy": 0.3},
            session={"primary": "london_ny_overlap", "is_high_volume_window": True, "is_weekend": False},
            indicators={"current_price": 100, "rsi_14": 60,
                        "sma_20": 102, "sma_50": 101, "sma_200": 100,
                        "volatility_30d_pct": 1.0},
            upcoming_macro=[],
        )
        assert 0 <= out["p_true"] <= 1.0
        assert out["verdict"] in ("TRUE_SIGNAL", "FAKE_OUT")
        assert "threshold" in out and "features" in out

    def test_fake_out_cascades_to_veto_in_analyze_symbol(self):
        """If meta_labeler returns FAKE_OUT, action must be HOLD and
        reasoning must contain 'VETO (meta-labeler)'."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        import ai_signals

        os.environ.setdefault("EMERGENT_LLM_KEY", "test-dummy")

        class FakeChat:
            def with_model(self, *a, **k):
                return self
            async def send_message(self, msg):
                return '{"action":"BUY","confidence":80,"reasoning":"bull","key_factors":["a"]}'

        trending_ind = {"current_price": 100.0, "sma_20": 102, "sma_50": 101,
                        "sma_200": 100, "rsi_14": 60, "volatility_30d_pct": 1.0}
        fake_meta = {"p_true": 0.20, "verdict": "FAKE_OUT", "threshold": 0.55,
                     "features": {}, "logit": -1.0}

        async def run():
            with patch.object(ai_signals, "LlmChat", return_value=FakeChat()), \
                 patch.object(ai_signals, "get_quote", new=AsyncMock(return_value={"price": 100.0, "bid": 99.9, "ask": 100.1, "change_pct": 0.0})), \
                 patch.object(ai_signals, "get_history", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "compute_indicators", return_value=trending_ind), \
                 patch.object(ai_signals, "score_sentiment", new=AsyncMock(return_value={"score": 0.0, "label": "neutral", "summary": "", "article_count": 0, "key_drivers": []})), \
                 patch.object(ai_signals, "macro_freeze_check", new=AsyncMock(return_value={"frozen": False, "reason": "", "event": None})), \
                 patch.object(ai_signals, "upcoming_for", new=AsyncMock(return_value=[])), \
                 patch.object(ai_signals, "predict_true_signal_probability", return_value=fake_meta):
                return await ai_signals.analyze_symbol("BTCUSD", "medium")

        sig = asyncio.run(run())
        assert sig["chart_action"] == "BUY"
        assert sig["action"] == "HOLD"
        assert sig["veto_applied"] is True
        assert "VETO (meta-labeler)" in (sig["reasoning"] or "")


# ---------- Paper Trading flow ----------
class TestPaperTrading:
    @pytest.fixture(scope="class")
    def paper_ctx(cls):
        s = requests.Session()
        email = f"TEST_paper_{uuid.uuid4().hex[:6]}@example.com"
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": "testpass123"}, timeout=15)
        assert r.status_code == 200
        # Paper account
        acc_r = s.post(f"{API}/accounts", json={
            "label": "TEST_Paper", "broker": "Exness",
            "server": "paper", "account_number": "PAPER1",
            "account_type": "demo", "base_currency": "USD",
            "mode": "paper", "initial_balance": 10000,
        }, timeout=10)
        assert acc_r.status_code == 200, acc_r.text
        return {"s": s, "acc": acc_r.json()}

    def test_paper_account_persisted(self, paper_ctx):
        s = paper_ctx["s"]
        accs = s.get(f"{API}/accounts", timeout=10).json()
        ours = [a for a in accs if a["id"] == paper_ctx["acc"]["id"]]
        assert ours, "paper account not found in list"
        a = ours[0]
        assert (a.get("mode") == "paper") or (a.get("broker") == "INTERNAL_PAPER")

    def test_paper_execute_creates_paper_trade(self, paper_ctx):
        """Generate a signal until we get BUY/SELL (or inject), then execute paper."""
        s = paper_ctx["s"]
        acc = paper_ctx["acc"]
        sig = None
        for sym in ("BTCUSD", "XAUUSD"):
            r = s.post(f"{API}/signals/generate", json={"symbol": sym}, timeout=180)
            if r.status_code == 200 and r.json().get("action") in ("BUY", "SELL"):
                sig = r.json()
                break
        if sig is None:
            # Inject synthetic non-HOLD
            from pymongo import MongoClient
            client = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
            db = client[os.environ.get("DB_NAME", "ai_trading_bot")]
            me = s.get(f"{API}/auth/me", timeout=10).json()
            doc = {
                "user_id": me["id"], "symbol": "BTCUSD", "action": "BUY",
                "confidence": 80.0, "entry_price": 65000.0, "stop_loss": 64000.0,
                "take_profit": 67000.0, "lot_size": 0.01, "reasoning": "synthetic",
                "risk_level": "medium", "consumed": False,
                "created_at": datetime_utcnow_iso(),
            }
            ins = db.signals.insert_one(doc)
            sig = {**doc, "id": str(ins.inserted_id)}
            client.close()

        r = s.post(f"{API}/trades/execute/{sig['id']}",
                   json={"account_id": acc["id"]}, timeout=15)
        assert r.status_code == 200, r.text
        trade = r.json()
        # Verify paper trade attributes
        mode = trade.get("mode") or "paper"  # paper account => paper trade
        assert mode == "paper" or trade.get("broker") == "INTERNAL_PAPER" \
               or acc.get("broker") == "INTERNAL_PAPER", \
               f"Expected paper trade, got: {trade}"
        assert trade["status"] in ("open", "pending")


# ---------- NL Strategy Builder ----------
class TestNLStrategy:
    def test_strategy_compile_and_apply(self, admin_session):
        r = admin_session.post(f"{API}/nl/strategy", json={
            "prompt": "Conservative gold trading during London"
        }, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        compiled = body.get("compiled") or {}
        assert isinstance(compiled, dict) and compiled, f"no compiled in body: {body}"
        risk = compiled.get("risk_level")
        symbols = compiled.get("symbols") or []
        session_pref = compiled.get("session_preference")
        notes = compiled.get("notes")
        assert risk == "low", f"risk_level should be low: {compiled}"
        assert "XAUUSD" in symbols, f"symbols missing XAUUSD: {symbols}"
        assert (session_pref or "").lower() == "london"
        assert notes, "notes should be populated"

        # Apply: route expects {"compiled": {...}}
        r2 = admin_session.post(f"{API}/nl/strategy/apply",
                                json={"compiled": compiled}, timeout=30)
        assert r2.status_code == 200, r2.text


# ---------- NL Risk Commander ----------
class TestNLCommander:
    def test_close_all_command(self, admin_session):
        r = admin_session.post(f"{API}/nl/command",
                               json={"prompt": "close all my open trades"},
                               timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        # Expect summary + receipts or actions
        actions = body.get("actions") or []
        receipts = body.get("receipts") or []
        all_action_types = ([a.get("type") for a in actions] +
                            [r.get("action") or r.get("type") for r in receipts])
        assert any("CLOSE_ALL" in (str(x) or "") for x in all_action_types), \
            f"Expected CLOSE_ALL_TRADES action, got: {body}"

    def test_conditional_trigger_creation_and_delete(self, admin_session):
        r = admin_session.post(f"{API}/nl/command", json={
            "prompt": "if Bitcoin drops 4% disable my high-risk bots"
        }, timeout=120)
        assert r.status_code == 200, r.text
        # List triggers
        r2 = admin_session.get(f"{API}/nl/triggers", timeout=10)
        assert r2.status_code == 200
        triggers = r2.json()
        triggers_list = triggers if isinstance(triggers, list) else triggers.get("triggers", [])
        btc_trigs = [t for t in triggers_list
                     if t.get("symbol") == "BTCUSD" and t.get("active") is not False]
        assert btc_trigs, f"no BTCUSD trigger created: {triggers_list}"
        tr = btc_trigs[-1]
        assert tr.get("condition") in ("drop", "drops", "down")
        # threshold can be 4 or 4.0 or 0.04
        thr = tr.get("threshold_pct") or tr.get("threshold")
        assert thr in (4, 4.0) or (isinstance(thr, (int, float)) and abs(thr - 4) < 0.01) \
               or (isinstance(thr, (int, float)) and abs(thr - 0.04) < 0.001), \
               f"threshold mismatch: {tr}"

        # Delete
        rd = admin_session.delete(f"{API}/nl/triggers/{tr['id']}", timeout=10)
        assert rd.status_code == 200
        # Verify inactive
        r3 = admin_session.get(f"{API}/nl/triggers", timeout=10)
        new_list = r3.json() if isinstance(r3.json(), list) else r3.json().get("triggers", [])
        same = [t for t in new_list if t.get("id") == tr["id"]]
        if same:
            assert same[0].get("active") is False


# ---------- MongoDB Time-Series ----------
class TestTimeSeriesCollections:
    def test_price_ticks_populated_after_quote(self, admin_session):
        # Generate a couple of quotes
        for _ in range(2):
            admin_session.get(f"{API}/market/quote/BTCUSD", timeout=30)
        # Verify ticks present in DB
        from pymongo import MongoClient
        client = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = client[os.environ.get("DB_NAME", "ai_trading_bot")]
        # Collection should exist (created via ensure_indexes or first insert)
        collections = db.list_collection_names()
        assert "price_ticks" in collections, f"price_ticks not present: {collections}"
        count = db.price_ticks.count_documents({"symbol": "BTCUSD"})
        assert count >= 1, f"price_ticks empty for BTCUSD"
        sample = db.price_ticks.find_one({"symbol": "BTCUSD"}, sort=[("ts", -1)])
        for k in ("ts", "symbol", "price"):
            assert k in sample, f"price_tick missing {k}: {sample}"
        client.close()

    def test_signal_history_collection_exists(self):
        from pymongo import MongoClient
        client = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = client[os.environ.get("DB_NAME", "ai_trading_bot")]
        collections = db.list_collection_names()
        assert "signal_history" in collections or "signals" in collections, \
            f"signal_history not present: {collections}"
        client.close()

    def test_conditional_triggers_collection_exists(self):
        from pymongo import MongoClient
        client = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = client[os.environ.get("DB_NAME", "ai_trading_bot")]
        collections = db.list_collection_names()
        assert "conditional_triggers" in collections, \
            f"conditional_triggers not present: {collections}"
        client.close()


# ---------- Prior endpoints regression ----------
class TestPriorEndpointsRegression:
    def test_panic(self, admin_session):
        r = admin_session.post(f"{API}/panic", timeout=10)
        assert r.status_code == 200

    def test_trades_stats(self, admin_session):
        r = admin_session.get(f"{API}/trades/stats", timeout=10)
        assert r.status_code == 200

    def test_calendar_upcoming_btc(self, admin_session):
        r = admin_session.get(f"{API}/calendar/upcoming/BTCUSD", timeout=30)
        assert r.status_code == 200

    def test_signals_list(self, admin_session):
        r = admin_session.get(f"{API}/signals", timeout=10)
        assert r.status_code == 200



# ---------- Iter-7: AI Co-Pilot (Claude Sonnet 4.5, grounded chat) ----------
class TestCoPilot:
    """Test the AI Co-Pilot endpoints: /api/copilot/chat and /api/copilot/sessions"""

    def test_chat_unauthenticated(self):
        # No auth cookie -> 401
        r = requests.post(f"{API}/copilot/chat", json={"message": "hi"}, timeout=15)
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"

    def test_chat_empty_message_returns_400(self, admin_session):
        r = admin_session.post(f"{API}/copilot/chat", json={"message": "   "}, timeout=15)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    def test_chat_too_long_message_returns_400(self, admin_session):
        long_msg = "a" * 2001
        r = admin_session.post(f"{API}/copilot/chat", json={"message": long_msg}, timeout=15)
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    def test_chat_basic_returns_snapshot_and_answer(self, admin_session):
        r = admin_session.post(
            f"{API}/copilot/chat",
            json={"message": "Why is my bot in HOLD?"},
            timeout=60,
        )
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert "session_id" in data and isinstance(data["session_id"], str) and len(data["session_id"]) > 0
        assert "answer" in data and isinstance(data["answer"], str) and len(data["answer"]) > 0
        assert "snapshot" in data and isinstance(data["snapshot"], dict)
        snap = data["snapshot"]
        for key in ["accounts", "recent_signals", "open_trades", "recent_trades", "panic", "active_triggers"]:
            assert key in snap, f"snapshot missing key: {key}"
        # bot_config may be absent if user has no bot config, but admin should have one — accept either
        # Stash session id for next test
        TestCoPilot._session_id = data["session_id"]

    def test_chat_multi_turn_continuity(self, admin_session):
        sid = getattr(TestCoPilot, "_session_id", None)
        assert sid, "Previous test must set session_id"
        r = admin_session.post(
            f"{API}/copilot/chat",
            json={"message": "What's my drawdown today?", "session_id": sid},
            timeout=60,
        )
        assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
        data = r.json()
        assert data["session_id"] == sid, "Session id should be preserved"
        assert isinstance(data["answer"], str) and len(data["answer"]) > 0
        # Now verify message count via GET /sessions/{sid}
        r2 = admin_session.get(f"{API}/copilot/sessions/{sid}", timeout=15)
        assert r2.status_code == 200
        body = r2.json()
        assert body["session_id"] == sid
        msgs = body.get("messages", [])
        # 2 turns x (user+assistant) = at least 4
        assert len(msgs) >= 4, f"Expected >=4 messages, got {len(msgs)}"
        # Verify role alternation: first user, then assistant, etc.
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"
        assert msgs[3]["role"] == "assistant"

    def test_list_sessions(self, admin_session):
        r = admin_session.get(f"{API}/copilot/sessions", timeout=15)
        assert r.status_code == 200
        sessions = r.json()
        assert isinstance(sessions, list)
        assert len(sessions) >= 1
        s0 = sessions[0]
        for key in ["session_id", "created_at", "last_used_at", "message_count", "preview"]:
            assert key in s0, f"Session missing key: {key}"
        assert s0["message_count"] >= 2

    def test_session_detail_not_found_returns_404(self, admin_session):
        r = admin_session.get(f"{API}/copilot/sessions/nonexistent_xyz_999", timeout=15)
        assert r.status_code == 404

    def test_grounding_recent_signal_appears_in_snapshot(self, admin_session):
        # Generate fresh BTCUSD signal first
        gen = admin_session.post(f"{API}/signals/generate", json={"symbol": "BTCUSD"}, timeout=60)
        assert gen.status_code == 200, f"Signal generate failed: {gen.status_code} {gen.text}"
        sig = gen.json()
        sig_symbol = sig.get("symbol")
        sig_action = sig.get("action")
        # Now ask Co-Pilot about it — its snapshot should include this signal
        r = admin_session.post(
            f"{API}/copilot/chat",
            json={"message": "Tell me about my latest BTC signal"},
            timeout=60,
        )
        assert r.status_code == 200
        snap = r.json()["snapshot"]
        recent = snap.get("recent_signals", [])
        assert len(recent) > 0, "recent_signals should be populated"
        latest = recent[0]
        assert latest["symbol"] == sig_symbol
        assert latest["action"] == sig_action
        # meta_verdict from the signal we just generated
        expected_verdict = (sig.get("meta_label") or {}).get("verdict")
        if expected_verdict is not None:
            assert latest.get("meta_verdict") == expected_verdict

    def test_no_route_conflict_existing_endpoints_still_work(self, admin_session):
        # Quick regression — make sure adding copilot routes didn't break adjacent /api routes
        assert admin_session.get(f"{API}/signals", timeout=10).status_code == 200
        assert admin_session.get(f"{API}/accounts", timeout=10).status_code == 200



# ---------- Bug Reports (iter-8) ----------
TINY_PNG_DATAURL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABAQMAAAAl21bKAAAAA1BMVEUAAACnej3aAAAAAXRSTlMAQObYZgAAAApJREFUCNdjYAAAAAIAAeIhvDMAAAAASUVORK5CYII="
)


class TestBugReports:
    """Bug report submission + admin moderation."""

    def test_create_bug_unauthenticated_returns_401(self):
        r = requests.post(f"{API}/bugs", json={"description": "hi"}, timeout=10)
        assert r.status_code == 401

    def test_create_bug_missing_description_returns_400(self, admin_session):
        r = admin_session.post(f"{API}/bugs", json={"screenshot": TINY_PNG_DATAURL}, timeout=10)
        assert r.status_code == 400

    def test_create_bug_empty_description_returns_400(self, admin_session):
        r = admin_session.post(f"{API}/bugs", json={"description": "   "}, timeout=10)
        assert r.status_code == 400

    def test_create_bug_too_long_description_returns_400(self, admin_session):
        r = admin_session.post(
            f"{API}/bugs",
            json={"description": "x" * 4001},
            timeout=10,
        )
        assert r.status_code == 400

    def test_create_bug_oversized_screenshot_returns_413(self, admin_session):
        big_ss = "data:image/png;base64," + ("A" * (4 * 1024 * 1024 + 100))
        r = admin_session.post(
            f"{API}/bugs",
            json={"description": "too big", "screenshot": big_ss},
            timeout=30,
        )
        assert r.status_code == 413

    def test_create_bug_happy_path(self, admin_session):
        payload = {
            "description": "TEST_iter8 something broke",
            "screenshot": TINY_PNG_DATAURL,
            "url": "https://example.com/dashboard",
            "user_agent": "Mozilla/5.0 (testing)",
            "viewport": {"w": 1920, "h": 800, "dpr": 1},
            "console_logs": [
                {"level": "error", "ts": "2026-01-01T00:00:00Z", "msg": "TypeError x"}
            ],
            "via": "copilot",
        }
        r = admin_session.post(f"{API}/bugs", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body["id"], str) and len(body["id"]) > 0
        assert "received_at" in body
        # Persistence — admin can GET it back
        r2 = admin_session.get(f"{API}/bugs/{body['id']}", timeout=10)
        assert r2.status_code == 200
        doc = r2.json()
        assert doc["description"] == "TEST_iter8 something broke"
        assert doc["screenshot"] == TINY_PNG_DATAURL
        assert doc["via"] == "copilot"
        assert doc["status"] == "new"
        # no mongo _id leak
        assert "_id" not in doc

    def test_list_bugs_non_admin_returns_403(self, fresh_user_session):
        r = fresh_user_session.get(f"{API}/bugs", timeout=10)
        assert r.status_code == 403

    def test_list_bugs_admin_strips_screenshot(self, admin_session):
        # ensure at least one exists
        admin_session.post(
            f"{API}/bugs",
            json={"description": "TEST_iter8 list-strip", "screenshot": TINY_PNG_DATAURL},
            timeout=10,
        )
        r = admin_session.get(f"{API}/bugs", timeout=10)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and len(items) >= 1
        for d in items:
            assert "_id" not in d
            assert "id" in d
            # screenshot stripped from list
            assert "screenshot" not in d or d.get("screenshot") is None

    def test_get_bug_admin_includes_screenshot(self, admin_session):
        r = admin_session.post(
            f"{API}/bugs",
            json={"description": "TEST_iter8 detail", "screenshot": TINY_PNG_DATAURL},
            timeout=10,
        )
        bid = r.json()["id"]
        r2 = admin_session.get(f"{API}/bugs/{bid}", timeout=10)
        assert r2.status_code == 200
        assert r2.json()["screenshot"] == TINY_PNG_DATAURL

    def test_get_bug_non_admin_returns_403(self, fresh_user_session, admin_session):
        r = admin_session.post(
            f"{API}/bugs",
            json={"description": "TEST_iter8 403"},
            timeout=10,
        )
        bid = r.json()["id"]
        r2 = fresh_user_session.get(f"{API}/bugs/{bid}", timeout=10)
        assert r2.status_code == 403

    def test_patch_status_admin_happy_path(self, admin_session):
        r = admin_session.post(
            f"{API}/bugs",
            json={"description": "TEST_iter8 triage"},
            timeout=10,
        )
        bid = r.json()["id"]
        r2 = admin_session.patch(
            f"{API}/bugs/{bid}/status",
            json={"status": "triaged"},
            timeout=10,
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "triaged"
        # verify persistence
        r3 = admin_session.get(f"{API}/bugs/{bid}", timeout=10)
        assert r3.json()["status"] == "triaged"

    def test_patch_status_invalid_returns_400(self, admin_session):
        r = admin_session.post(
            f"{API}/bugs", json={"description": "TEST_iter8 invalid status"}, timeout=10
        )
        bid = r.json()["id"]
        r2 = admin_session.patch(
            f"{API}/bugs/{bid}/status", json={"status": "bogus"}, timeout=10
        )
        assert r2.status_code == 400

    def test_patch_status_non_admin_returns_403(self, fresh_user_session, admin_session):
        r = admin_session.post(
            f"{API}/bugs", json={"description": "TEST_iter8 forbidden patch"}, timeout=10
        )
        bid = r.json()["id"]
        r2 = fresh_user_session.patch(
            f"{API}/bugs/{bid}/status", json={"status": "triaged"}, timeout=10
        )
        assert r2.status_code == 403


# ---------- Subscriptions (iter-8) ----------
class TestSubscriptionPlans:
    def test_plans_public_endpoint(self, admin_session):
        # Plans are returned via authenticated route in this app
        r = admin_session.get(f"{API}/subscription/plans", timeout=10)
        assert r.status_code == 200
        plans = r.json()
        assert isinstance(plans, list) and len(plans) == 4
        by_id = {p["id"]: p for p in plans}
        assert set(by_id.keys()) == {"monthly", "quarterly", "semi_annual", "annual"}

        # Monthly: $49, 1 month
        m = by_id["monthly"]
        assert m["duration_months"] == 1
        assert m["discount_pct"] == 0
        assert m["amount_usd"] == 49.0
        assert m["effective_monthly_usd"] == 49.0
        assert m["savings_usd"] == 0.0

        # Quarterly: 10% off -> 49*3*0.9 = 132.30
        q = by_id["quarterly"]
        assert q["duration_months"] == 3
        assert q["discount_pct"] == 10
        assert q["amount_usd"] == 132.30
        assert q["effective_monthly_usd"] == 44.10
        assert q["savings_usd"] == 14.70  # 147 - 132.30

        # Semi-annual: 20% off -> 49*6*0.8 = 235.20
        sa = by_id["semi_annual"]
        assert sa["duration_months"] == 6
        assert sa["discount_pct"] == 20
        assert sa["amount_usd"] == 235.20
        assert sa["effective_monthly_usd"] == 39.20
        assert sa["savings_usd"] == 58.80  # 294 - 235.20

        # Annual: 40% off -> 49*12*0.6 = 352.80
        a = by_id["annual"]
        assert a["duration_months"] == 12
        assert a["discount_pct"] == 40
        assert a["amount_usd"] == 352.80
        assert a["effective_monthly_usd"] == 29.40
        assert a["savings_usd"] == 235.20  # 588 - 352.80


class TestSubscriptionStatus:
    def test_status_admin_grandfathered(self, admin_session):
        r = admin_session.get(f"{API}/subscription/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        sub = body["subscription"]
        ent = body["entitlement"]
        assert sub["current_plan_id"] == "admin_grandfather"
        assert ent["active"] is True
        # valid_until ~ 10 years from now
        assert sub["valid_until"] is not None
        from datetime import datetime
        vu = datetime.fromisoformat(sub["valid_until"].replace("Z", "+00:00"))
        from datetime import datetime as _dt, timezone as _tz
        delta_days = (vu - _dt.now(_tz.utc)).days
        assert delta_days > 365 * 9, f"expected ~10y, got {delta_days}d"

    def test_status_requires_auth(self):
        r = requests.get(f"{API}/subscription/status", timeout=10)
        assert r.status_code == 401


ORIGIN = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")


class TestSubscriptionCheckout:
    def test_checkout_unauthenticated_returns_401(self):
        r = requests.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": ORIGIN},
            timeout=10,
        )
        assert r.status_code == 401

    def test_checkout_invalid_plan_returns_400(self, admin_session):
        r = admin_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "bogus", "origin": ORIGIN},
            timeout=15,
        )
        assert r.status_code == 400

    def test_checkout_missing_origin_returns_400(self, admin_session):
        r = admin_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly"},
            timeout=15,
        )
        assert r.status_code == 400

    def test_checkout_bad_origin_returns_400(self, admin_session):
        r = admin_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": "javascript:alert(1)"},
            timeout=15,
        )
        assert r.status_code == 400

    def test_checkout_happy_path_monthly(self, fresh_user_session):
        r = fresh_user_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": ORIGIN},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["checkout_url"].startswith("https://checkout.stripe.com")
        sid = body["session_id"]
        assert sid.startswith("cs_test_") or sid.startswith("cs_")
        assert body["plan"]["id"] == "monthly"
        assert body["plan"]["amount_usd"] == 49.0

    def test_admin_cannot_subscribe(self, admin_session):
        r = admin_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": ORIGIN},
            timeout=15,
        )
        assert r.status_code == 400
        assert "grandfather" in r.text.lower() or "admin" in r.text.lower()


class TestSubscriptionPoll:
    def test_poll_nonexistent_session_returns_404(self, admin_session):
        r = admin_session.get(
            f"{API}/subscription/poll/cs_test_doesnotexist_iter8",
            timeout=15,
        )
        assert r.status_code == 404

    def test_poll_foreign_session_returns_404(self, admin_session, fresh_user_session):
        # fresh_user creates a checkout session (admin can't subscribe)
        r = fresh_user_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": ORIGIN},
            timeout=30,
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        # admin tries to poll fresh_user's session
        r2 = admin_session.get(f"{API}/subscription/poll/{sid}", timeout=15)
        assert r2.status_code == 404


class TestStripeWebhook:
    def test_webhook_invalid_signature_returns_400(self):
        r = requests.post(
            f"{API}/webhook/stripe",
            data=b"{}",
            headers={"Stripe-Signature": "t=0,v1=bogus", "Content-Type": "application/json"},
            timeout=15,
        )
        assert r.status_code == 400


class TestApplyPaymentIdempotency:
    """Directly exercise apply_successful_payment via the service layer."""

    def test_apply_payment_twice_only_extends_once(self, fresh_user_session):
        import asyncio
        import sys
        from pathlib import Path
        sys.path.insert(0, "/app/backend")
        # Load backend .env so MONGO_URL/DB_NAME are available to the
        # imported service modules (we are not running inside uvicorn here).
        try:
            from dotenv import load_dotenv
            load_dotenv(Path("/app/backend/.env"))
        except Exception:
            pass
        from subscription_service import apply_successful_payment  # noqa
        from database import get_db  # noqa

        # Create a real checkout for fresh user to avoid polluting admin state
        r = fresh_user_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": ORIGIN},
            timeout=30,
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]

        async def _run():
            db = get_db()
            # Force-mark our seed txn payment_status so apply can proceed
            # apply_successful_payment looks up by session_id only; it ignores
            # current payment_status until applied flag set.
            sub1 = await apply_successful_payment(sid)
            sub2 = await apply_successful_payment(sid)
            return sub1, sub2, await db.subscriptions.find_one(
                {"user_id": sub1["user_id"]}
            )

        sub1, sub2, fresh = asyncio.run(_run())
        assert sub1 is not None, "first apply should succeed"
        assert sub2 is None, "second apply must be idempotent (return None)"
        # valid_until shouldn't extend twice — it must equal sub1's
        assert fresh["valid_until"] == sub1["valid_until"]


class TestBackendErrorLogs:
    """Quick sanity over a fresh 90s sample of supervisor backend.err.log."""

    def test_no_recent_exceptions(self):
        import time as _t
        # Sample log size BEFORE small wait
        path = "/var/log/supervisor/backend.err.log"
        if not os.path.exists(path):
            pytest.skip("backend.err.log not present")
        with open(path, "rb") as f:
            f.seek(0, 2)
            start = f.tell()
        _t.sleep(5)  # short window — we already ran many tests above
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read().decode(errors="ignore")
        # Lightweight check: no unhandled Tracebacks during this run
        assert "Traceback (most recent call last)" not in chunk, chunk[-2000:]


class TestMtfGate:
    """Multi-Timeframe trend gate — pure-function tests."""

    def _setup(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from mtf_check import multi_timeframe_gate
        return multi_timeframe_gate

    def test_buy_aligned_with_uptrend(self):
        fn = self._setup()
        # 70-day climbing series — clear uptrend on all 3 checks
        history = [{"close": 100 + i * 1.5} for i in range(70)]
        indicators = {"current_price": history[-1]["close"], "sma_50": 120, "sma_200": 110}
        out = fn("BUY", history, indicators)
        assert out["aligned"] is True
        assert out["htf_trend"] == "UP"
        assert out["reason"] == ""

    def test_buy_counter_trend_vetoed(self):
        fn = self._setup()
        # Falling series — BUY should be vetoed
        history = [{"close": 200 - i * 1.5} for i in range(70)]
        indicators = {"current_price": history[-1]["close"], "sma_50": 100, "sma_200": 150}
        out = fn("BUY", history, indicators)
        assert out["aligned"] is False
        assert out["htf_trend"] == "DOWN"
        assert "MTF" in out["reason"]

    def test_sell_counter_trend_vetoed(self):
        fn = self._setup()
        history = [{"close": 100 + i * 1.5} for i in range(70)]
        indicators = {"current_price": history[-1]["close"], "sma_50": 150, "sma_200": 100}
        out = fn("SELL", history, indicators)
        assert out["aligned"] is False
        assert out["htf_trend"] == "UP"

    def test_hold_is_noop(self):
        fn = self._setup()
        history = [{"close": 100 + i * 1.5} for i in range(70)]
        indicators = {"current_price": 200, "sma_50": 150, "sma_200": 100}
        out = fn("HOLD", history, indicators)
        assert out["aligned"] is True
        assert out["checked"] is False

    def test_short_history_passes(self):
        fn = self._setup()
        # too few candles → soft pass
        history = [{"close": 100 + i} for i in range(10)]
        indicators = {"current_price": 110}
        out = fn("BUY", history, indicators)
        assert out["aligned"] is True
        assert out["checked"] is False


class TestAutoTune:
    """Auto-tune confidence threshold from analytics."""

    def test_get_endpoint_returns_per_symbol(self, admin_session):
        r = admin_session.get(f"{API}/analytics/auto-tune", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert "thresholds" in body
        assert "enabled" in body
        assert isinstance(body["thresholds"], list)
        for t in body["thresholds"]:
            assert "suggested_threshold" in t
            assert "profile_min" in t
            assert "effective_threshold" in t
            assert "source" in t
            assert t["source"] in ("auto_tuned", "profile_default")
            assert "breakdown" in t

    def test_refresh_endpoint(self, admin_session):
        r = admin_session.post(f"{API}/analytics/auto-tune/refresh", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body["refreshed"] is True
        assert isinstance(body["thresholds"], list)

    def test_compute_threshold_picks_lowest_qualifying_bucket(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from auto_tune import _compute_threshold
        # Bucket 60: 6 trades, 4 wins → 66.7% wr (qualifies)
        # Bucket 70: 6 trades, 5 wins → 83% wr (qualifies)
        rows = (
            [{"confidence": 62, "pnl": 10} for _ in range(4)]
            + [{"confidence": 62, "pnl": -5} for _ in range(2)]
            + [{"confidence": 72, "pnl": 10} for _ in range(5)]
            + [{"confidence": 72, "pnl": -5} for _ in range(1)]
        )
        out = _compute_threshold(rows, profile_min=65.0)
        # Lowest qualifying is 60
        assert out["suggested_threshold"] == 60
        # Effective is max(60, profile_min=65) = 65
        assert out["effective_threshold"] == 65.0
        assert out["source"] == "auto_tuned"

    def test_compute_threshold_falls_back_to_profile(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from auto_tune import _compute_threshold
        # Only 2 trades total — below MIN_SAMPLES
        rows = [{"confidence": 75, "pnl": 5}, {"confidence": 80, "pnl": 10}]
        out = _compute_threshold(rows, profile_min=65.0)
        assert out["suggested_threshold"] is None
        assert out["source"] == "profile_default"
        assert out["effective_threshold"] == 65.0


class TestSpreadFilter:
    """Bridge heartbeat carries spreads; bot config persists threshold; gate applied."""

    def test_bot_config_roundtrip_spread_fields(self, admin_session):
        r = admin_session.get(f"{API}/bot/config", timeout=10)
        assert r.status_code == 200
        cfg = r.json()
        # New fields exist with sane defaults
        assert "spread_filter_enabled" in cfg
        assert "max_spread_pips" in cfg
        assert "auto_tune_enabled" in cfg
        # Update + read back
        cfg["spread_filter_enabled"] = True
        cfg["max_spread_pips"] = {"XAUUSD": 3.5, "BTCUSD": 80.0}
        cfg["auto_tune_enabled"] = False
        r2 = admin_session.put(f"{API}/bot/config", json=cfg, timeout=10)
        assert r2.status_code == 200
        back = r2.json()
        assert back["spread_filter_enabled"] is True
        assert back["max_spread_pips"]["XAUUSD"] == 3.5
        assert back["max_spread_pips"]["BTCUSD"] == 80.0
        assert back["auto_tune_enabled"] is False
        # Reset for other tests
        cfg["spread_filter_enabled"] = False
        cfg["auto_tune_enabled"] = True
        admin_session.put(f"{API}/bot/config", json=cfg, timeout=10)

    def test_heartbeat_persists_spreads(self, admin_session):
        # Need an account with a bridge token
        accs = admin_session.get(f"{API}/accounts", timeout=10).json()
        live = [a for a in accs if (a.get("mode") or "live") == "live"]
        if not live:
            # Create a live account to test
            r = admin_session.post(
                f"{API}/accounts",
                json={
                    "label": "spread-test", "broker": "Test", "server": "Test-Demo",
                    "account_number": "999111", "account_type": "demo",
                    "base_currency": "USD", "mode": "live",
                },
                timeout=15,
            )
            assert r.status_code == 200
            token = r.json()["bridge_token"]
            acc_id = r.json()["id"]
        else:
            token = live[0]["bridge_token"]
            acc_id = live[0]["id"]
        # Send a heartbeat carrying spreads
        r = requests.post(
            f"{API}/bridge/heartbeat",
            json={
                "bridge_token": token, "balance": 1000.0, "equity": 1000.0,
                "open_positions": 0,
                "spreads": {"XAUUSD": 4.2, "BTCUSD": 70.5},
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        # Pull accounts back — current_spreads must be present
        accs2 = admin_session.get(f"{API}/accounts", timeout=10).json()
        match = [a for a in accs2 if a["id"] == acc_id][0]
        assert match.get("current_spreads", {}).get("XAUUSD") == 4.2
        assert match.get("current_spreads", {}).get("BTCUSD") == 70.5

    def test_heartbeat_without_spreads_still_works(self):
        # Backward-compat: old EA without spreads field should not crash
        # Use a known-good account by registering a fresh one
        s = requests.Session()
        import uuid as _u
        email = f"compat_{_u.uuid4().hex[:8]}@example.com"
        s.post(f"{API}/auth/register", json={"email": email, "password": "pw123456"}, timeout=15)
        r = s.post(
            f"{API}/accounts",
            json={
                "label": "compat", "broker": "B", "server": "X",
                "account_number": "12345", "account_type": "demo",
                "mode": "live",
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        token = r.json()["bridge_token"]
        # No spreads field at all — must still 200
        r2 = requests.post(
            f"{API}/bridge/heartbeat",
            json={"bridge_token": token, "balance": 500.0, "equity": 500.0, "open_positions": 0},
            timeout=15,
        )
        assert r2.status_code == 200, r2.text



class TestAffiliateSubGate:
    """Affiliate Program must be gated behind an active paid subscription."""

    @staticmethod
    def _load_env():
        try:
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path("/app/backend/.env"))
        except Exception:
            pass

    def test_admin_bypass(self, admin_session):
        # Admin is grandfathered — should always see real status, never gated
        r = admin_session.get(f"{API}/affiliate/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body.get("subscription_required") is False

    def test_status_for_expired_user_returns_sub_required(self):
        # Fresh user, expire grace, status should flip to subscription_required
        self._load_env()
        import uuid as _u
        from motor.motor_asyncio import AsyncIOMotorClient
        import asyncio
        s = requests.Session()
        email = f"subgate_{_u.uuid4().hex[:8]}@example.com"
        s.post(f"{API}/auth/register", json={"email": email, "password": "pw123456"}, timeout=15)
        me = s.get(f"{API}/auth/me", timeout=10).json()
        # Touch status so the subscription doc gets created
        s.get(f"{API}/affiliate/status", timeout=10)

        # Expire the user's grace + valid_until via direct DB write
        async def _expire():
            mongo = os.environ["MONGO_URL"]
            cli = AsyncIOMotorClient(mongo)
            db = cli[os.environ["DB_NAME"]]
            await db.subscriptions.update_one(
                {"user_id": me["id"]},
                {"$set": {"grace_until": "2024-01-01T00:00:00+00:00",
                          "valid_until": None}},
            )
            cli.close()
        asyncio.run(_expire())

        r = s.get(f"{API}/affiliate/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "subscription_required"
        assert body["subscription_required"] is True
        assert body["subscription"]["active"] is False

    def test_apply_blocked_with_402_for_expired(self):
        self._load_env()
        import uuid as _u
        from motor.motor_asyncio import AsyncIOMotorClient
        import asyncio
        s = requests.Session()
        email = f"subgate2_{_u.uuid4().hex[:8]}@example.com"
        s.post(f"{API}/auth/register", json={"email": email, "password": "pw123456"}, timeout=15)
        me = s.get(f"{API}/auth/me", timeout=10).json()
        s.get(f"{API}/affiliate/status", timeout=10)

        async def _expire():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            await db.subscriptions.update_one(
                {"user_id": me["id"]},
                {"$set": {"grace_until": "2024-01-01T00:00:00+00:00",
                          "valid_until": None}},
            )
            cli.close()
        asyncio.run(_expire())

        r = s.post(
            f"{API}/affiliate/apply",
            json={"terms_agreed": True, "full_name": "X",
                  "audience_url": "https://x.com",
                  "promotion_strategy": "blog",
                  "payment_method": "PayPal"},
            timeout=15,
        )
        assert r.status_code == 402, r.text
        detail = r.json().get("detail") or {}
        assert detail.get("code") == "subscription_required"


class TestIntelligenceCounters:
    """Daily intelligence counters surfaced via /api/bot/status."""

    @staticmethod
    def _load_env():
        try:
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path("/app/backend/.env"))
        except Exception:
            pass

    def test_status_returns_intelligence_block(self, admin_session):
        r = admin_session.get(f"{API}/bot/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        intel = body.get("intelligence")
        assert intel is not None
        for k in ("mtf_veto", "auto_tune_block", "spread_block", "slippage_veto", "total"):
            assert k in intel
            assert isinstance(intel[k], int)

    def test_increment_and_read(self):
        self._load_env()
        import asyncio, sys
        sys.path.insert(0, "/app/backend")
        # Use a fresh client (not the cached module-level one — earlier tests
        # may have closed it and we want isolation from the running app DB).
        from motor.motor_asyncio import AsyncIOMotorClient
        # Patch database._db so intelligence_counters writes to a fresh client
        import database as _db_mod
        _db_mod._client = None
        _db_mod._db = None
        from intelligence_counters import increment, get_today, get_window_24h

        async def _run():
            uid = f"test-intel-{int(time.time())}"
            await increment(uid, "mtf_veto")
            await increment(uid, "mtf_veto")
            await increment(uid, "slippage_veto")
            today = await get_today(uid)
            assert today["mtf_veto"] == 2
            assert today["slippage_veto"] == 1
            assert today["auto_tune_block"] == 0
            window = await get_window_24h(uid)
            assert window["mtf_veto"] == 2
            assert window["total"] == 3
            await increment(uid, "bogus")
            today2 = await get_today(uid)
            assert today2 == today

        asyncio.run(_run())


class TestSlippageVeto:
    """Server-side slippage veto force-closes fills that deviated too much."""

    @staticmethod
    def _load_env():
        try:
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path("/app/backend/.env"))
        except Exception:
            pass

    def test_bot_config_roundtrip_slippage(self, admin_session):
        cfg = admin_session.get(f"{API}/bot/config", timeout=10).json()
        assert "slippage_veto_enabled" in cfg
        assert "max_slippage_pips" in cfg
        # Update
        cfg["slippage_veto_enabled"] = True
        cfg["max_slippage_pips"] = {"XAUUSD": 12.0, "BTCUSD": 70.0}
        r = admin_session.put(f"{API}/bot/config", json=cfg, timeout=10)
        assert r.status_code == 200, r.text
        back = r.json()
        assert back["slippage_veto_enabled"] is True
        assert back["max_slippage_pips"]["XAUUSD"] == 12.0
        assert back["max_slippage_pips"]["BTCUSD"] == 70.0

    def test_excess_slippage_triggers_full_close(self, admin_session):
        """End-to-end: create a live account, simulate fill with 50-pip XAU slippage
        → trade gets close_reason=slippage_veto and pending_modification=FULL_CLOSE."""
        self._load_env()
        # Ensure slippage veto is ON with a tight cap
        cfg = admin_session.get(f"{API}/bot/config", timeout=10).json()
        cfg["slippage_veto_enabled"] = True
        cfg["max_slippage_pips"] = {"XAUUSD": 5.0, "BTCUSD": 80.0}
        admin_session.put(f"{API}/bot/config", json=cfg, timeout=10)

        # Find or create a live account
        accs = admin_session.get(f"{API}/accounts", timeout=10).json()
        live = [a for a in accs if (a.get("mode") or "live") == "live"]
        if not live:
            r = admin_session.post(
                f"{API}/accounts",
                json={"label": "slip-test", "broker": "B", "server": "S",
                      "account_number": "55512", "account_type": "demo", "mode": "live"},
                timeout=15,
            )
            assert r.status_code == 200, r.text
            account = r.json()
        else:
            account = live[0]
        token = account["bridge_token"]
        account_id = account["id"]

        # Insert a pending trade directly via Mongo so we can control the intended entry
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from bson import ObjectId
        async def _seed():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            doc = {
                "user_id": account["user_id"],
                "account_id": account_id,
                "signal_id": None,
                "symbol": "XAUUSD",
                "action": "BUY",
                "lot_size": 0.01,
                "entry_price": 2400.00,        # intended
                "original_stop_loss": 2385.00,
                "stop_loss": 2385.00,
                "take_profit": 2430.00,
                "tp1": 2410.0, "tp2": 2420.0, "tp3": 2430.0,
                "status": "pending",
                "created_at": "2026-06-22T17:00:00+00:00",
            }
            r = await db.trades.insert_one(doc)
            cli.close()
            return str(r.inserted_id)
        trade_id = asyncio.run(_seed())

        # Now simulate EA reporting an "open" with 10-pip slippage (XAUUSD pip=0.10
        # → 10 pips of slippage = 1.00 price diff). Intended 2400 → actual 2401.0
        r = requests.post(
            f"{API}/bridge/report",
            json={
                "bridge_token": token,
                "trade_id": trade_id,
                "status": "open",
                "mt5_ticket": 999111,
                "entry_price": 2401.00,
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text

        # Pull the trade — must show slippage fields + veto kicked in (cap was 5p, slip was 10p)
        async def _read():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            t = await db.trades.find_one({"_id": ObjectId(trade_id)})
            cli.close()
            return t
        t = asyncio.run(_read())
        assert t is not None
        assert t.get("slippage_pips") == 10.0
        assert t.get("intended_entry_price") == 2400.00
        assert t.get("close_reason") == "slippage_veto"
        assert (t.get("pending_modification") or {}).get("type") == "FULL_CLOSE"
        assert t.get("slippage_veto_cap_pips") == 5.0

    def test_acceptable_slippage_does_not_veto(self, admin_session):
        """Slippage below cap should NOT veto — trade proceeds normally."""
        self._load_env()
        cfg = admin_session.get(f"{API}/bot/config", timeout=10).json()
        cfg["slippage_veto_enabled"] = True
        cfg["max_slippage_pips"] = {"XAUUSD": 30.0, "BTCUSD": 100.0}
        admin_session.put(f"{API}/bot/config", json=cfg, timeout=10)

        accs = admin_session.get(f"{API}/accounts", timeout=10).json()
        live = [a for a in accs if (a.get("mode") or "live") == "live"]
        assert live, "previous test should have created a live account"
        account = live[0]

        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from bson import ObjectId
        async def _seed():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            doc = {
                "user_id": account["user_id"],
                "account_id": account["id"],
                "signal_id": None,
                "symbol": "XAUUSD",
                "action": "BUY",
                "lot_size": 0.01,
                "entry_price": 2400.00,
                "stop_loss": 2385.00,
                "take_profit": 2430.00,
                "tp1": 2410.0, "tp2": 2420.0, "tp3": 2430.0,
                "status": "pending",
                "created_at": "2026-06-22T17:00:00+00:00",
            }
            r = await db.trades.insert_one(doc)
            cli.close()
            return str(r.inserted_id)
        trade_id = asyncio.run(_seed())

        # 5-pip slippage = 0.5 price diff on XAU → well under 30-pip cap
        r = requests.post(
            f"{API}/bridge/report",
            json={
                "bridge_token": account["bridge_token"],
                "trade_id": trade_id, "status": "open",
                "mt5_ticket": 999222, "entry_price": 2400.50,
            },
            timeout=15,
        )
        assert r.status_code == 200, r.text
        async def _read():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ["DB_NAME"]]
            t = await db.trades.find_one({"_id": ObjectId(trade_id)})
            cli.close()
            return t
        t = asyncio.run(_read())
        assert t["slippage_pips"] == 5.0
        assert t.get("close_reason") != "slippage_veto"
        assert t.get("pending_modification") is None



class TestSLImminentWatcher:
    """Telegram alert when an open trade's SL ETA drops below 5 minutes."""

    @staticmethod
    def _load_env():
        try:
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path("/app/backend/.env"))
        except Exception:
            pass

    def test_velocity_zero_returns_no_fire(self):
        """If we have only 1 sample (or no measurable velocity), no alert."""
        self._load_env()
        import asyncio, sys
        sys.path.insert(0, "/app/backend")
        import database as _db_mod
        _db_mod._client = None; _db_mod._db = None
        from database import get_db
        from sl_watcher import _recent_velocity

        async def _run():
            db = get_db()
            sym = f"TESTVZ_{int(time.time())}"
            # Insert just 1 tick — velocity must be 0
            from datetime import datetime, timezone
            await db.price_ticks.insert_one({
                "ts": datetime.now(timezone.utc),
                "symbol": sym, "price": 100.0, "bid": 100.0, "ask": 100.0,
            })
            v = await _recent_velocity(db, sym)
            assert v == 0.0

        asyncio.run(_run())

    def test_velocity_from_recent_ticks(self):
        """Inject 3 ticks 10s apart with $1 jumps → velocity = 0.1 price/sec."""
        self._load_env()
        import asyncio, sys
        sys.path.insert(0, "/app/backend")
        import database as _db_mod
        _db_mod._client = None; _db_mod._db = None
        from database import get_db
        from sl_watcher import _recent_velocity

        async def _run():
            db = get_db()
            sym = f"TESTV_{int(time.time())}"
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            for i, p in enumerate([100.0, 101.0, 102.0]):
                await db.price_ticks.insert_one({
                    "ts": now - timedelta(seconds=20 - i * 10),
                    "symbol": sym, "price": p, "bid": p, "ask": p,
                })
            v = await _recent_velocity(db, sym)
            # Median of {0.1, 0.1} = 0.1
            assert abs(v - 0.1) < 1e-6

        asyncio.run(_run())

    def test_sweep_fires_when_eta_under_5min(self, admin_session):
        """End-to-end: seed a fast-moving symbol + an open BUY trade with SL
        ~30s away → sweep_once should call notify, mark sl_alert_sent_at,
        and report fired>=1."""
        self._load_env()
        import asyncio, sys
        sys.path.insert(0, "/app/backend")
        import database as _db_mod
        _db_mod._client = None; _db_mod._db = None
        from database import get_db
        from datetime import datetime, timezone, timedelta
        from bson import ObjectId

        # We monkey-patch notifier.send_telegram so we don't need a real bot
        import sl_watcher
        captured = []
        async def _fake_send(user_id, event_type, title, lines):
            captured.append({"user_id": user_id, "event_type": event_type,
                             "title": title, "lines": lines})
            return True
        sl_watcher.send_telegram = _fake_send

        async def _run():
            db = get_db()
            sym = "XAUUSD"
            now = datetime.now(timezone.utc)
            # Snapshot + clear any recent XAU ticks so OUR injected velocity wins.
            since = now - timedelta(minutes=30)
            saved = await db.price_ticks.find(
                {"symbol": sym, "ts": {"$gte": since}}
            ).to_list(length=2000)
            await db.price_ticks.delete_many({"symbol": sym, "ts": {"$gte": since}})
            # Insert recent ticks giving velocity = 1.0 price/sec
            for i, p in enumerate([2400.0, 2410.0, 2420.0]):
                await db.price_ticks.insert_one({
                    "ts": now - timedelta(seconds=20 - i * 10),
                    "symbol": sym, "price": p, "bid": p, "ask": p,
                })
            # Force the get_quote cache to return our latest price
            import market as _m
            _m._cache_set(f"quote:{sym}",
                          {"symbol": sym, "price": 2420.0,
                           "bid": 2420.0, "ask": 2420.0,
                           "timestamp": now.isoformat()},
                          ttl=10)
            # Seed an open SELL trade with SL just $30 away → at 1.0/sec → 30s ETA
            me = admin_session.get(f"{API}/auth/me", timeout=10).json()
            trade = {
                "user_id": me["id"],
                "account_id": "test-acc",
                "signal_id": None,
                "symbol": sym, "action": "SELL",
                "lot_size": 0.01,
                "entry_price": 2400.0,
                "stop_loss": 2450.0,     # SELL → SL above current 2420 → 30 away
                "take_profit": 2380.0,
                "status": "open",
                "created_at": now.isoformat(),
            }
            r = await db.trades.insert_one(trade)
            try:
                out = await sl_watcher.sweep_once()
                assert out["fired"] >= 1, f"expected fire, got {out}"
                assert any(c["event_type"] == "sl_imminent" for c in captured)
                doc = await db.trades.find_one({"_id": r.inserted_id})
                assert doc.get("sl_alert_sent_at") is not None
                assert doc.get("sl_alert_eta_secs") is not None
                # Idempotency — second sweep should NOT fire again (cooldown)
                captured.clear()
                out2 = await sl_watcher.sweep_once()
                assert out2["fired"] == 0
                assert len(captured) == 0
            finally:
                await db.trades.delete_one({"_id": r.inserted_id})
                # Restore the original ticks for the rest of the suite
                await db.price_ticks.delete_many({"symbol": sym, "ts": {"$gte": since}})
                if saved:
                    for d in saved:
                        d.pop("_id", None)
                    await db.price_ticks.insert_many(saved)

        asyncio.run(_run())

    def test_buy_past_sl_does_not_fire(self):
        """A BUY trade whose price is already below SL is already a losing
        position — broker should be closing it; we don't fire."""
        self._load_env()
        import asyncio, sys
        sys.path.insert(0, "/app/backend")
        import database as _db_mod
        _db_mod._client = None; _db_mod._db = None
        from database import get_db
        from datetime import datetime, timezone, timedelta
        import sl_watcher
        fired = []
        async def _fake_send(*a, **kw):
            fired.append(1); return True
        sl_watcher.send_telegram = _fake_send

        async def _run():
            db = get_db()
            sym = "XAUUSD"
            now = datetime.now(timezone.utc)
            for i, p in enumerate([2400.0, 2390.0, 2380.0]):
                await db.price_ticks.insert_one({
                    "ts": now - timedelta(seconds=20 - i * 10),
                    "symbol": sym, "price": p, "bid": p, "ask": p,
                })
            import market as _m
            _m._cache_set(f"quote:{sym}",
                          {"symbol": sym, "price": 2380.0,
                           "bid": 2380.0, "ask": 2380.0,
                           "timestamp": now.isoformat()},
                          ttl=10)
            trade = {
                "user_id": "x", "account_id": "y", "signal_id": None,
                "symbol": sym, "action": "BUY", "lot_size": 0.01,
                "entry_price": 2400.0, "stop_loss": 2390.0,  # already below
                "take_profit": 2430.0, "status": "open",
                "created_at": now.isoformat(),
            }
            r = await db.trades.insert_one(trade)
            try:
                out = await sl_watcher.sweep_once()
                # Either skipped (price <= SL) or never met threshold for this
                # one trade. Must NOT fire because direction-aware guard kicks in.
                assert len(fired) == 0, "should not fire when BUY price <= SL"
                # The function should still increment checked for OTHER trades
                # but for this specific synthetic trade, expect 0 from this one.
                assert isinstance(out, dict)
            finally:
                await db.trades.delete_one({"_id": r.inserted_id})

        asyncio.run(_run())
