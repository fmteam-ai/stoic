"""Iter-14: Gold Edge pack — DXY gate, weekly drawdown, session-LR models.

Covers:
  * macro.dxy.get_dxy_snapshot + dxy_gate_check
  * ai_signals.analyze_symbol — DXY veto fields on XAU
  * circuit_breakers.check_and_trip — weekly drawdown + daily precedence
  * learned_meta.session_bucket + retrain (global + per-session) + predict_p_win
  * PUT/GET /api/bot/config persistence of weekly_drawdown_pct + weekly_drawdown_enabled

Cleanup: a module-level autouse fixture removes any qa_iter14_* test data.
"""
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

# Make backend importable (its modules live at /app/backend/...)
sys.path.insert(0, "/app/backend")
# Load backend .env so in-process calls (database.get_db, learned_meta, etc.)
# see MONGO_URL / DB_NAME exactly as the running server does.
load_dotenv("/app/backend/.env", override=False)


def _reset_motor():
    """Motor caches its event loop on first use; pytest's asyncio.run() creates
    a fresh loop per test → reset module-level singletons so each call gets a
    new motor client bound to the current loop."""
    import database as _dbmod
    _dbmod._client = None
    _dbmod._db = None


def _arun(coro_fn):
    """Run an async function in a fresh loop, ensuring motor is reset first."""
    import asyncio
    _reset_motor()
    return asyncio.run(coro_fn)

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")


# ---------- admin session helper (HTTP layer) -----------------------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed: {r.status_code} {r.text}")
    return s


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    try:
        client = MongoClient(MONGO_URL)
        db = client[DB_NAME]
        # Remove any synthetic test users/configs we created
        db.users.delete_many({"email": {"$regex": "^qa_iter14_"}})
        db.bot_configs.delete_many({"user_id": {"$regex": "^qa_iter14_"}})
        db.trades.delete_many({"user_id": {"$regex": "^qa_iter14_"}})
        client.close()
    except Exception:
        pass


# =====================================================================
# 1. macro.dxy — snapshot + gate
# =====================================================================
class TestDXY:
    def test_dxy_snapshot_shape(self):
        """get_dxy_snapshot(force_refresh=True) → dict with required keys.

        Yahoo can occasionally 429 from the container — skip on hard failure.
        """
        import asyncio
        from macro import dxy as dxy_mod

        snap = _arun(dxy_mod.get_dxy_snapshot(force_refresh=True))
        if snap is None:
            pytest.skip("DXY upstream unavailable (rate-limited)")

        required = {"current_price", "ema_20", "slope_5d_pct", "above_ema",
                    "regime", "as_of_date", "fetched_at"}
        assert required.issubset(snap.keys()), f"missing keys: {required - set(snap.keys())}"
        assert snap["regime"] in ("bullish_usd", "bearish_usd", "neutral")
        assert isinstance(snap["current_price"], (int, float))
        assert isinstance(snap["above_ema"], bool)

        # Cached doc should be persisted at _id='latest'
        client = MongoClient(MONGO_URL)
        try:
            cached = client[DB_NAME].dxy_cache.find_one({"_id": "latest"})
            assert cached is not None, "dxy_cache.latest not persisted"
            assert cached.get("regime") == snap["regime"]
        finally:
            client.close()

    def test_dxy_gate_bullish_usd_blocks_xau_buy(self):
        from macro.dxy import dxy_gate_check
        snap = {"current_price": 106.5, "ema_20": 104.0, "slope_5d_pct": 0.8, "regime": "bullish_usd"}
        out = dxy_gate_check("BUY", "XAUUSD", snap)
        assert out["passed"] is False
        assert "fight" in out["reason"].lower() or "veto" in out["reason"].lower()

    def test_dxy_gate_bullish_usd_passes_xau_sell(self):
        from macro.dxy import dxy_gate_check
        snap = {"current_price": 106.5, "ema_20": 104.0, "slope_5d_pct": 0.8, "regime": "bullish_usd"}
        out = dxy_gate_check("SELL", "XAUUSD", snap)
        assert out["passed"] is True
        assert out["aligned"] is True

    def test_dxy_gate_bearish_usd_passes_xau_buy(self):
        from macro.dxy import dxy_gate_check
        snap = {"current_price": 100.0, "ema_20": 102.5, "slope_5d_pct": -0.6, "regime": "bearish_usd"}
        out = dxy_gate_check("BUY", "XAUUSD", snap)
        assert out["passed"] is True

    def test_dxy_gate_neutral_passes_both(self):
        from macro.dxy import dxy_gate_check
        snap = {"current_price": 103.0, "ema_20": 103.0, "slope_5d_pct": 0.0, "regime": "neutral"}
        assert dxy_gate_check("BUY", "XAUUSD", snap)["passed"] is True
        assert dxy_gate_check("SELL", "XAUUSD", snap)["passed"] is True

    def test_dxy_gate_non_xau_symbols_pass(self):
        from macro.dxy import dxy_gate_check
        snap = {"current_price": 106.5, "ema_20": 104.0, "slope_5d_pct": 0.8, "regime": "bullish_usd"}
        for sym in ("BTCUSD", "EURUSD", "GBPJPY"):
            out = dxy_gate_check("BUY", sym, snap)
            assert out["passed"] is True, f"{sym} should bypass DXY gate"

    def test_dxy_gate_no_snapshot_passes_with_reason(self):
        from macro.dxy import dxy_gate_check
        out = dxy_gate_check("BUY", "XAUUSD", None)
        assert out["passed"] is True
        assert "unavailable" in out["reason"].lower() or "skipped" in out["reason"].lower()


# =====================================================================
# 2. ai_signals.analyze_symbol — DXY plumbing (smoke via /api/signals/run-now)
# =====================================================================
class TestSignalsDXYWiring:
    def test_xauusd_signal_has_dxy_fields(self, admin_session):
        """Trigger an on-demand XAUUSD analysis and assert DXY fields are present."""
        r = admin_session.post(f"{API}/signals/generate",
                               json={"symbol": "XAUUSD"}, timeout=90)
        if r.status_code not in (200, 201):
            pytest.skip(f"signals/generate unavailable: {r.status_code} {r.text[:200]}")
        body = r.json()
        # Response may be {signal: {...}} or the signal dict itself
        sig = body.get("signal", body)
        if not isinstance(sig, dict) or "action" not in sig:
            pytest.skip(f"signal payload not parseable: {str(body)[:200]}")
        assert "dxy_gate" in sig, "dxy_gate missing from XAUUSD signal"
        # dxy_gate retired into setup_score factors — None is valid now;
        # the DXY feature itself must still be plumbed through.
        assert "dxy" in sig, "dxy feature missing from XAUUSD signal"


# =====================================================================
# 3. circuit_breakers — weekly + daily precedence
# =====================================================================
class TestWeeklyDrawdown:
    @pytest.fixture(autouse=True)
    def _seed(self):
        """Insert a synthetic user with bot_config + 6 days of small losses."""
        self.uid = f"qa_iter14_user_{uuid.uuid4().hex[:8]}"
        client = MongoClient(MONGO_URL)
        db = client[DB_NAME]
        # Clear any leftovers for this uid
        db.bot_configs.delete_many({"user_id": self.uid})
        db.trades.delete_many({"user_id": self.uid})

        now = datetime.now(timezone.utc)
        # 4 trades, all closed >24h ago (so they don't count toward "today"),
        # within 7 days, summing to -80. Equity=1000 → -8% rolling weekly.
        for i in range(4):
            closed_at = (now - timedelta(days=2 + i)).date().isoformat()
            db.trades.insert_one({
                "user_id": self.uid,
                "status": "closed",
                "closed_at": closed_at,
                "pnl": -20.0,
                "origin": "auto",
                "symbol": "XAUUSD",
                "_qa": True,
            })
        self.db = db
        self.client = client
        yield
        db.trades.delete_many({"user_id": self.uid})
        db.bot_configs.delete_many({"user_id": self.uid})
        client.close()

    def test_weekly_trips_when_enabled(self):
        import asyncio
        from circuit_breakers import check_and_trip
        from database import get_db

        cfg = {
            "user_id": self.uid, "active": True, "risk_level": "medium",
            "daily_drawdown_pct": 5.0, "daily_drawdown_enabled": True,
            "weekly_drawdown_pct": 7.0, "weekly_drawdown_enabled": True,
        }
        self.db.bot_configs.insert_one(dict(cfg))
        accounts = [{"equity": 1000.0, "balance": 1000.0}]

        _reset_motor()
        from database import get_db as _gd
        result = _arun(check_and_trip(_gd(), self.uid, cfg, accounts))

        assert result["tripped"] is True, f"expected weekly trip, got {result}"
        assert result["kind"] == "weekly", f"expected kind=weekly, got {result['kind']}"
        assert result["drawdown_week_pct"] <= -7.0

        # bot_configs.active force-set to false
        cfg_doc = self.db.bot_configs.find_one({"user_id": self.uid})
        assert cfg_doc["active"] is False

    def test_weekly_disabled_does_not_trip(self):
        import asyncio
        from circuit_breakers import check_and_trip
        from database import get_db

        cfg = {
            "user_id": self.uid, "active": True, "risk_level": "medium",
            "daily_drawdown_pct": 5.0, "daily_drawdown_enabled": True,
            "weekly_drawdown_pct": 7.0, "weekly_drawdown_enabled": False,
        }
        self.db.bot_configs.insert_one(dict(cfg))
        accounts = [{"equity": 1000.0, "balance": 1000.0}]

        _reset_motor()
        result = _arun(check_and_trip(get_db(), self.uid, cfg, accounts))
        assert result["tripped"] is False, f"expected NO trip with weekly disabled, got {result}"
        assert result["kind"] == ""

    def test_daily_precedes_weekly(self):
        """Insert a -150 closed TODAY → daily breaches first, kind='daily'."""
        import asyncio
        from circuit_breakers import check_and_trip
        from database import get_db

        today = datetime.now(timezone.utc).date().isoformat()
        self.db.trades.insert_one({
            "user_id": self.uid, "status": "closed",
            "closed_at": today, "pnl": -150.0, "origin": "auto",
            "symbol": "XAUUSD", "_qa": True,
        })
        cfg = {
            "user_id": self.uid, "active": True, "risk_level": "medium",
            "daily_drawdown_pct": 5.0, "daily_drawdown_enabled": True,
            "weekly_drawdown_pct": 7.0, "weekly_drawdown_enabled": True,
        }
        self.db.bot_configs.insert_one(dict(cfg))
        accounts = [{"equity": 1000.0, "balance": 1000.0}]

        _reset_motor()
        result = _arun(check_and_trip(get_db(), self.uid, cfg, accounts))
        assert result["tripped"] is True
        assert result["kind"] == "daily", f"daily should win precedence, got {result}"


# =====================================================================
# 4. learned_meta — session_bucket + retrain + predict_p_win
# =====================================================================
class TestLearnedMetaSessions:
    def test_session_bucket_hours(self):
        from learned_meta import session_bucket
        for h in range(0, 7):
            dt = datetime(2026, 1, 15, h, 30, tzinfo=timezone.utc)
            assert session_bucket(dt) == "ASIA", f"hour {h} should be ASIA"
        for h in range(7, 13):
            dt = datetime(2026, 1, 15, h, 30, tzinfo=timezone.utc)
            assert session_bucket(dt) == "LONDON", f"hour {h} should be LONDON"
        for h in range(13, 22):
            dt = datetime(2026, 1, 15, h, 30, tzinfo=timezone.utc)
            assert session_bucket(dt) == "NY", f"hour {h} should be NY"
        for h in (22, 23):
            dt = datetime(2026, 1, 15, h, 30, tzinfo=timezone.utc)
            assert session_bucket(dt) == "OFF", f"hour {h} should be OFF"

    def test_session_bucket_accepts_iso_and_none(self):
        from learned_meta import session_bucket
        assert session_bucket(None) == "OFF"
        assert session_bucket("2026-01-15T10:00:00Z") == "LONDON"
        assert session_bucket("2026-01-15T03:00:00+00:00") == "ASIA"
        assert session_bucket("2026-01-15T15:00:00") == "NY"  # naive → assumed UTC

    def test_retrain_returns_global_plus_per_session(self):
        import asyncio
        from learned_meta import retrain

        result = _arun(retrain())
        # Could legitimately fail with n<30; if so skip.
        if not result.get("trained"):
            pytest.skip(f"retrain not trained: {result.get('reason')}")
        assert "global" in result
        assert "per_session" in result
        for label in ("ASIA", "LONDON", "NY"):
            assert label in result["per_session"], f"missing {label} in per_session"
            entry = result["per_session"][label]
            assert "trained" in entry
            if entry["trained"]:
                assert "train_auc" in entry and "threshold" in entry
            else:
                assert "reason" in entry

        # Artifacts persisted
        client = MongoClient(MONGO_URL)
        try:
            arts = client[DB_NAME].learned_meta_artifacts
            assert arts.find_one({"key": "learned_meta_v1"}) is not None
            # NY artifact is the one expected to exist per agent context
            if result["per_session"].get("NY", {}).get("trained"):
                assert arts.find_one({"key": "learned_meta_v1_session_NY"}) is not None
        finally:
            client.close()

    def test_predict_p_win_picks_session_or_falls_back(self, monkeypatch):
        import asyncio
        import learned_meta as lm
        from datetime import datetime as real_dt

        signal = {
            "action": "BUY", "confidence": 65, "entry_price": 2050.0,
            "kalman_filter": {"k_velocity": 0.5},
            "cot_positioning": {"overcrowded_long": False, "overcrowded_short": False},
            "real_yield_10y": {"regime": "neutral"},
            "mtf_gate": {"aligned": True},
            "upcoming_macro": [],
        }

        # Pin "now" to 14:00 UTC → NY
        class _NYTime:
            @staticmethod
            def now(tz=None):
                return real_dt(2026, 1, 15, 14, 0, tzinfo=tz or timezone.utc)
        monkeypatch.setattr(lm, "datetime", _NYTime)
        out = _arun(lm.predict_p_win(signal))
        if out is None:
            pytest.skip("no global artifact yet — retrain first")
        assert out["current_session"] == "NY"
        assert out["model_used"] in ("NY", "GLOBAL")  # NY if trained, else fallback

        # Pin "now" to 03:00 UTC → ASIA (likely no ASIA artifact → GLOBAL)
        class _ASIATime:
            @staticmethod
            def now(tz=None):
                return real_dt(2026, 1, 15, 3, 0, tzinfo=tz or timezone.utc)
        monkeypatch.setattr(lm, "datetime", _ASIATime)
        out2 = _arun(lm.predict_p_win(signal))
        assert out2 is not None
        assert out2["current_session"] == "ASIA"
        # Per agent context, ASIA artifact does NOT exist → must fall back to GLOBAL
        client = MongoClient(MONGO_URL)
        try:
            has_asia = client[DB_NAME].learned_meta_artifacts.find_one(
                {"key": "learned_meta_v1_session_ASIA"}) is not None
        finally:
            client.close()
        if not has_asia:
            assert out2["model_used"] == "GLOBAL"


# =====================================================================
# 5. /api/bot/config — weekly fields persistence
# =====================================================================
class TestBotConfigWeeklyFields:
    def test_put_and_get_weekly_fields(self, admin_session):
        payload = {"weekly_drawdown_pct": 9.5, "weekly_drawdown_enabled": True}
        r = admin_session.put(f"{API}/bot/config", json=payload, timeout=15)
        assert r.status_code == 200, r.text

        cfg = admin_session.get(f"{API}/bot/config", timeout=15).json()
        assert cfg.get("weekly_drawdown_pct") == 9.5
        assert cfg.get("weekly_drawdown_enabled") is True

        # Toggle off and re-verify
        r2 = admin_session.put(f"{API}/bot/config",
                               json={"weekly_drawdown_enabled": False}, timeout=15)
        assert r2.status_code == 200, r2.text
        cfg2 = admin_session.get(f"{API}/bot/config", timeout=15).json()
        assert cfg2.get("weekly_drawdown_enabled") is False
        # pct unchanged
        assert cfg2.get("weekly_drawdown_pct") == 9.5
