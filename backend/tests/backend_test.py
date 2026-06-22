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
