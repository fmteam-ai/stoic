"""Iter-66 — Full backend regression sweep.

Verifies:
  1. Dashboard widget endpoints (watch-status, cooldowns, risk-gauge, weekly-digest, exchanges)
  2. /api/bot/risk-gauge profit_target block lifecycle (enable, disable, invalid action)
  3. compute_lot_for_account locked_profit shrinks lot / floors at 0
  4. is_market_closed weekend window for XAUUSD + crypto bypass
  5. /api/crypto/accounts dispatch validation (okx passphrase, kraken routing)
"""
import os
import datetime as dt
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
    "https://algo-trade-135.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"

PROFILE = {
    "min_confidence": 50,
    "kelly_cap": 0.20,
    "risk_pct": 1.0,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 3.0,
}


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:300]}"
    return s


class TestWidgetEndpoints:
    def test_watch_status(self, session):
        r = session.get(f"{BASE_URL}/api/signals/watch-status", timeout=10)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "symbols" in d and isinstance(d["symbols"], list)
        assert "hold_streak" in d and isinstance(d["hold_streak"], int) and d["hold_streak"] >= 0
        rows = {s.get("symbol"): s for s in d["symbols"]}
        assert "XAUUSD" in rows, f"XAUUSD missing from watch-status symbols"
        # Required keys regardless of populated state
        for k in ("action",):
            assert k in rows["XAUUSD"]

    def test_cooldowns(self, session):
        r = session.get(f"{BASE_URL}/api/bot/cooldowns", timeout=10)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "items" in d and isinstance(d["items"], list) and len(d["items"]) >= 1
        for item in d["items"]:
            for k in ("label", "broker", "symbols", "loss_streak"):
                assert k in item, f"cooldown item missing {k}"
            for s in item["symbols"]:
                assert "symbol" in s and "market_closed" in s

    def test_risk_gauge(self, session):
        r = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=10)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "items" in d and isinstance(d["items"], list) and len(d["items"]) >= 1
        for it in d["items"]:
            assert "daily" in it and "weekly" in it and "equity" in it
            assert "limit_pct" in it["daily"]
            # Some accounts may use 'pnl' instead of 'used_pct' — accept either
            assert "pnl" in it["daily"] or "used_pct" in it["daily"]
            assert "profit_target" in it  # block must always exist (maybe disabled)

    def test_weekly_digest(self, session):
        r = session.get(f"{BASE_URL}/api/insights/weekly-digest", timeout=10)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert "stats" in d
        stats = d["stats"]
        for k in ("trades", "win_rate"):
            assert k in stats, f"stats missing {k}"

    def test_exchanges_has_reachability(self, session):
        r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=15)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        ex = d.get("exchanges", d) if isinstance(d, dict) else d
        assert isinstance(ex, list) and len(ex) >= 5
        ids = {e["id"]: e for e in ex}
        for needed in ("binance", "binanceus", "kraken", "okx", "kucoin"):
            assert needed in ids, f"{needed} missing from exchange list"
            assert "reachable" in ids[needed], f"reachability flag missing on {needed}"

    def test_exchanges_binance_unreachable_others_ok(self, session):
        # cache may make it instant on 2nd call — first call warms it
        session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=20)
        r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=10)
        d = r.json()
        ex = d.get("exchanges", d) if isinstance(d, dict) else d
        ids = {e["id"]: e for e in ex}
        # Binance global is expected geo-blocked from cluster
        assert ids["binance"]["reachable"] is False
        # At least one of the alt exchanges should be reachable
        reachables = [eid for eid in ("kraken", "binanceus", "okx", "kucoin")
                      if ids[eid].get("reachable")]
        assert len(reachables) >= 1, f"no reachable alternative exchange: {ids}"


class TestProfitTarget:
    def test_enable_profit_target_then_gauge_shows_enabled(self, session):
        put = session.put(f"{BASE_URL}/api/bot/config",
                          json={"daily_profit_target_r": 3.5,
                                "daily_profit_target_action": "stop"}, timeout=10)
        assert put.status_code in (200, 204), put.text[:300]

        g = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=10).json()
        default = next((it for it in g["items"] if it.get("account_id") is None), None)
        assert default is not None, "Default profile item not found"
        pt = default["profit_target"]
        assert pt.get("enabled") is True, f"profit_target should be enabled, got {pt}"
        assert pt.get("target_r") == 3.5
        assert pt.get("mode") == "stop"
        assert pt.get("target_amount", 0) >= 0

    def test_invalid_action_coerces_to_lock(self, session):
        put = session.put(f"{BASE_URL}/api/bot/config",
                          json={"daily_profit_target_r": 2.0,
                                "daily_profit_target_action": "TURBO_LOCK"}, timeout=10)
        assert put.status_code in (200, 204), put.text[:300]
        g = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=10).json()
        default = next((it for it in g["items"] if it.get("account_id") is None), None)
        assert default["profit_target"]["mode"] == "lock"

    def test_disable_when_r_is_zero(self, session):
        put = session.put(f"{BASE_URL}/api/bot/config",
                          json={"daily_profit_target_r": 0}, timeout=10)
        assert put.status_code in (200, 204), put.text[:300]
        g = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=10).json()
        default = next((it for it in g["items"] if it.get("account_id") is None), None)
        assert default["profit_target"]["enabled"] is False


class TestCryptoAccountValidation:
    def test_okx_without_passphrase_returns_422(self, session):
        # api_key must be >=10 chars to clear initial Pydantic min_length
        r = session.post(f"{BASE_URL}/api/crypto/accounts",
                         json={"exchange_id": "okx", "label": "TEST_okx",
                               "api_key": "fakekey1234567890",
                               "api_secret": "fakesecret1234567890"}, timeout=10)
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:200]}"
        assert "passphrase" in r.text.lower()

    def test_kraken_dispatches_with_kraken_error(self, session):
        r = session.post(f"{BASE_URL}/api/crypto/accounts",
                         json={"exchange_id": "kraken", "label": "TEST_kraken",
                               "api_key": "fakekey1234567890",
                               "api_secret": "fakesecret1234567890"}, timeout=20)
        assert r.status_code in (400, 422), f"got {r.status_code}: {r.text[:200]}"
        assert "kraken" in r.text.lower(), f"kraken not in error: {r.text[:200]}"


class TestUnitLayer:
    def test_compute_lot_locked_profit_shrinks_lot(self):
        from risk import compute_lot_for_account
        account = {"equity": 10000.0, "account_type": "standard"}
        base = compute_lot_for_account(account, "EURUSD", 1.0800, 1.0750,
                                       confidence_pct=75.0, profile=PROFILE,
                                       locked_profit=0.0)
        shrunk = compute_lot_for_account(account, "EURUSD", 1.0800, 1.0750,
                                         confidence_pct=75.0, profile=PROFILE,
                                         locked_profit=2000.0)
        assert base["lot_size"] >= shrunk["lot_size"], \
            f"locked profit must shrink lot: base={base}, shrunk={shrunk}"
        assert base["equity"] > shrunk["equity"], \
            "shrunk equity should be lower after subtracting locked"

    def test_compute_lot_locked_gt_equity_falls_back(self):
        from risk import compute_lot_for_account
        account = {"equity": 1000.0, "account_type": "standard"}
        out = compute_lot_for_account(account, "EURUSD", 1.0800, 1.0750,
                                      confidence_pct=75.0, profile=PROFILE,
                                      locked_profit=2000.0)
        assert out["method"] == "rejected_no_equity", \
            f"locked > equity must fail closed (C5), got {out}"
        assert out["lot_size"] == 0.0 and out["sizing_valid"] is False

    def test_market_closed_weekend_xauusd(self):
        from microstructure import is_market_closed
        sat = dt.datetime(2025, 12, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert is_market_closed("XAUUSD", now=sat) is not None
        sun = dt.datetime(2025, 12, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert is_market_closed("XAUUSD", now=sun) is not None
        fri_late = dt.datetime(2025, 12, 26, 23, 0, 0, tzinfo=dt.timezone.utc)
        assert is_market_closed("XAUUSD", now=fri_late) is not None
        wed = dt.datetime(2025, 12, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert is_market_closed("XAUUSD", now=wed) is None

    def test_market_closed_returns_none_for_crypto(self):
        from microstructure import is_market_closed
        sat = dt.datetime(2025, 12, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
        for sym in ("BTCUSD", "ETHUSD", "BTC/USDT", "SOLUSD"):
            assert is_market_closed(sym, now=sat) is None, f"crypto must bypass: {sym}"
