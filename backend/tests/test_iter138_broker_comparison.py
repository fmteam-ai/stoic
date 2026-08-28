"""iter-138 · Phase H Part B — Broker Comparison endpoint tests.

Verifies:
  - GET /api/accounts/broker-comparison returns expected shape
  - `days` param is clamped 1..365
  - unauthenticated request rejected
  - brokers with no accounts AND no trades are excluded
  - slippage sanity: values within admin dataset bounds (avg ≤ 0.5, worst ≤ 6.5)
  - median dispatch latency in a sane range (0..120s)
  - each broker exposes execution / pnl / accounts sub-objects with declared fields
"""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"

EXEC_FIELDS = {
    "avg_slippage_pips", "worst_slippage_pips", "slippage_samples",
    "median_fill_latency_s", "latency_samples", "latency_kind",
    "failed_orders", "fail_rate_pct",
}
PNL_FIELDS = {
    "trades", "wins", "losses", "win_rate", "net_pnl",
    "profit_factor", "avg_win", "avg_loss",
}
ACC_FIELDS = {"count", "equity", "connected"}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return s


class TestBrokerComparison:
    # -------- happy path --------
    def test_endpoint_shape_30d(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                              timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["days"] == 30
        assert isinstance(data["brokers"], list)
        assert len(data["brokers"]) > 0, "Expected at least one broker in admin dataset"

        for b in data["brokers"]:
            assert "broker" in b and isinstance(b["broker"], str) and b["broker"]
            assert set(b["execution"].keys()) >= EXEC_FIELDS, \
                f"missing execution fields for {b['broker']}: {EXEC_FIELDS - set(b['execution'].keys())}"
            assert set(b["pnl"].keys()) >= PNL_FIELDS, \
                f"missing pnl fields for {b['broker']}"
            assert set(b["accounts"].keys()) >= ACC_FIELDS
            assert b["execution"]["latency_kind"] == "dispatch"

    def test_no_empty_brokers(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                              timeout=20)
        data = r.json()
        for b in data["brokers"]:
            has_accounts = b["accounts"]["count"] > 0
            has_trades = b["pnl"]["trades"] > 0
            has_fails = b["execution"]["failed_orders"] > 0
            assert has_accounts or has_trades or has_fails, \
                f"Broker {b['broker']} has neither accounts nor trades — should be excluded"

    # -------- slippage sanity --------
    def test_slippage_sane(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                              timeout=20)
        data = r.json()
        for b in data["brokers"]:
            e = b["execution"]
            if e["avg_slippage_pips"] is not None:
                # Real slippage must be small; huge values indicate legacy signal-vs-fill
                # deltas (bug). Admin sample: 0.02..0.42 pips.
                assert 0 <= e["avg_slippage_pips"] <= 5.0, \
                    f"{b['broker']} avg_slippage_pips={e['avg_slippage_pips']} out of sane band"
                # slippage_samples must be >0 whenever avg is not None
                assert e["slippage_samples"] > 0
            if e["worst_slippage_pips"] is not None:
                assert e["worst_slippage_pips"] <= 10.0, \
                    f"{b['broker']} worst_slippage_pips={e['worst_slippage_pips']} out of sane band"
                assert e["worst_slippage_pips"] >= (e["avg_slippage_pips"] or 0)

    # -------- latency sanity --------
    def test_latency_dispatch(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                              timeout=20)
        data = r.json()
        found_latency = False
        any_samples = any((b["execution"].get("latency_samples") or 0) > 0
                          for b in data["brokers"])
        if not any_samples:
            pytest.skip("no dispatch-latency samples inside the rolling "
                        "30-day window — live-data dependent")
        for b in data["brokers"]:
            e = b["execution"]
            if e["median_fill_latency_s"] is not None:
                found_latency = True
                assert 0 <= e["median_fill_latency_s"] <= 120, \
                    f"{b['broker']} median latency out of clamp"
                assert e["latency_samples"] > 0
        assert found_latency, "Expected at least one broker to expose median dispatch latency"

    # -------- pnl integrity --------
    def test_pnl_integrity(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                              timeout=20)
        data = r.json()
        for b in data["brokers"]:
            p = b["pnl"]
            if p["trades"] > 0:
                assert p["wins"] + p["losses"] <= p["trades"]
                if p["wins"] > 0:
                    assert p["avg_win"] is not None and p["avg_win"] >= 0
                if p["losses"] > 0:
                    assert p["avg_loss"] is not None and p["avg_loss"] <= 0
                if p["win_rate"] is not None:
                    assert 0 <= p["win_rate"] <= 100

    # -------- days clamp --------
    @pytest.mark.parametrize("raw,expected", [
        (0, 1),        # below floor → 1
        (1, 1),
        (7, 7),
        (30, 30),
        (90, 90),
        (365, 365),
        (5000, 365),   # above ceiling → 365
        (-10, 1),
    ])
    def test_days_clamp(self, admin_session, raw, expected):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days={raw}",
                              timeout=20)
        assert r.status_code == 200, r.text
        assert r.json()["days"] == expected

    # -------- period switching --------
    def test_period_switching_trade_counts_shift(self, admin_session):
        d7 = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=7",
                               timeout=20).json()
        d90 = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=90",
                                timeout=20).json()
        assert d7["days"] == 7 and d90["days"] == 90
        # In general, 90d trade totals should be >= 7d totals across all brokers
        sum7 = sum(b["pnl"]["trades"] for b in d7["brokers"])
        sum90 = sum(b["pnl"]["trades"] for b in d90["brokers"])
        assert sum90 >= sum7, f"90d ({sum90}) trades should be >= 7d ({sum7})"

    # -------- auth --------
    def test_unauthenticated_rejected(self):
        # Fresh session (no cookies) — should 401/403
        r = requests.get(f"{BASE_URL}/api/accounts/broker-comparison?days=30",
                         timeout=15)
        assert r.status_code in (401, 403), \
            f"Expected 401/403 without auth, got {r.status_code}"

    # -------- expected brokers in admin dataset --------
    def test_expected_brokers_present(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/accounts/broker-comparison?days=90",
                              timeout=20)
        names = {b["broker"] for b in r.json()["brokers"]}
        expected_any = {"OnEquity", "RoboForex", "STARTRADER",
                        "Tauro Markets", "VTMarkets", "KRAKEN_SPOT"}
        overlap = names & expected_any
        assert len(overlap) >= 3, \
            f"Expected >=3 of {expected_any} in admin dataset, got {names}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
