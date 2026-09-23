"""Iter-52 · Round 14 live regression — Scalp Fast Path hardening.

Verifies over the external REACT_APP_BACKEND_URL:
  • GET /api/scalp/status broker_state / commission_check shape for the
    enabled OnEquity EURUSD runner
  • GET /api/scalp/metrics window / lifetime / CI / model.model_scope /
    commission_check (with and without account_id)
  • P0 snapshot propagation: heartbeat resets broker_state.heartbeat_age_sec
    to ~0 without any tick batch in between
  • Sanity: /api/health, /api/bot/pulse regressions
  • Sanity: block_bootstrap_ci brackets the sample mean, find_account
    signature has the injected `oid_parser` param
"""
from __future__ import annotations
from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

import os
import random
import time
from typing import Any

import pytest
import requests

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
ADMIN = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json=ADMIN, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    assert "access_token" in s.cookies.get_dict(), "access_token cookie missing"
    return s


@pytest.fixture(scope="module")
def scalp_status(admin_session: requests.Session) -> dict[str, Any]:
    r = admin_session.get(f"{BASE_URL}/api/scalp/status", timeout=20)
    assert r.status_code == 200, r.text[:400]
    return r.json()


@pytest.fixture(scope="module")
def enabled_runner(scalp_status: dict[str, Any]) -> dict[str, Any]:
    runners = [
        r for r in scalp_status.get("runners", [])
        if r.get("enabled") and r.get("symbol", "").upper() == "EURUSD"
    ]
    if not runners:
        pytest.skip("No enabled EURUSD runner in the current environment")
    # Prefer the runner that is actively streaming (broker_state.heartbeat_at
    # is populated) — this is the OnEquity demo terminal in this env.
    def _pref(x: dict) -> tuple:
        bs = x.get("broker_state") or {}
        hb_at = bs.get("heartbeat_at")
        age = bs.get("heartbeat_age_sec")
        streaming = 0 if (hb_at and age is not None and age < 120) else 1
        return (streaming, age if age is not None else 1e9)
    runners.sort(key=_pref)
    best = runners[0]
    bs = best.get("broker_state") or {}
    age = bs.get("heartbeat_age_sec")
    # These are live-evidence tests: they only prove anything when the EA is
    # actively streaming AND the runner has completed its lazy risk restore
    # (which happens on the first EA report after a backend restart). An
    # unconverged environment is a skip, not a failure.
    if age is None or age > 60 or not best.get("risk_restored"):
        pytest.skip(
            f"live environment not converged (heartbeat_age={age}, "
            f"risk_restored={best.get('risk_restored')}) — EA must stream "
            f"and deliver one report after backend start")
    return best


# --------------------------------------------------------------------------
# Sanity — health, pulse, block_bootstrap_ci, find_account signature
# --------------------------------------------------------------------------
class TestSanityRegression:
    def test_api_health_ok(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/health", timeout=10)
        assert r.status_code == 200
        body = r.json()
        # accept several common shapes
        assert body.get("status") in {"ok", "healthy", "OK"} or body.get("ok") is True, body

    def test_bot_pulse_items(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert "items" in body and isinstance(body["items"], list), body

    def test_block_bootstrap_ci_brackets_mean(self):
        from scalp.stats import block_bootstrap_ci
        random.seed(1234)
        samples = [random.gauss(2.5, 3.0) for _ in range(300)]
        res = block_bootstrap_ci(samples)
        assert res is not None and len(res) == 2
        lo, hi = res
        mean = sum(samples) / len(samples)
        assert lo <= mean <= hi, f"CI [{lo}, {hi}] must bracket mean {mean}"

    def test_block_bootstrap_ci_small_sample_none(self):
        from scalp.stats import block_bootstrap_ci
        assert block_bootstrap_ci([1.0, 2.0, 3.0]) is None

    def test_find_account_oid_parser_signature(self):
        import inspect
        from protection_guard import find_account
        params = list(inspect.signature(find_account).parameters)
        assert "oid_parser" in params, f"missing oid_parser: {params}"


# --------------------------------------------------------------------------
# Item 3 — status().broker_state per-aspect freshness + commission_check
# --------------------------------------------------------------------------
class TestStatusBrokerState:
    def test_enabled_runner_has_broker_state(self, enabled_runner):
        bs = enabled_runner.get("broker_state")
        assert isinstance(bs, dict), f"broker_state missing/wrong type: {bs}"
        # Required keys
        for k in (
            "heartbeat_at", "heartbeat_age_sec", "equity_age_sec",
            "symbol_specs_updated_at", "symbol_specs_age_sec",
            "spreads_age_sec", "connection_status", "last_order_ack_ms",
        ):
            assert k in bs, f"broker_state missing key {k}: {list(bs)}"

    def test_enabled_runner_heartbeat_fresh(self, enabled_runner):
        bs = enabled_runner["broker_state"]
        age = bs.get("heartbeat_age_sec")
        # EA heartbeats every ~5s; allow generous slack for CI jitter
        assert age is not None, "heartbeat_age_sec must not be None while EA streams"
        assert age < 60, f"heartbeat too stale for a live runner: {age}s"

    def test_enabled_runner_connection_status_connected(self, enabled_runner):
        bs = enabled_runner["broker_state"]
        assert bs.get("connection_status") == "connected", bs

    def test_enabled_runner_commission_check(self, enabled_runner):
        cc = enabled_runner.get("commission_check")
        # commission_check is set on restore/reconcile; must be a dict on
        # a runner that has restored risk (enabled + streaming)
        assert isinstance(cc, dict), f"commission_check missing: {cc}"
        for k in ("configured_usd_per_lot_side", "observed_deals",
                  "observed_median_usd_per_lot", "mismatch"):
            assert k in cc, f"commission_check missing {k}: {list(cc)}"
        assert isinstance(cc["observed_deals"], int)
        assert isinstance(cc["mismatch"], bool)


# --------------------------------------------------------------------------
# Items 5/6/7 — metrics window / lifetime / CI / model_scope / commission
# --------------------------------------------------------------------------
class TestMetricsRound14:
    def test_metrics_symbol_only_has_symbol_scope(self, admin_session):
        r = admin_session.get(
            f"{BASE_URL}/api/scalp/metrics",
            params={"symbol": "EURUSD"}, timeout=20,
        )
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        # Either n=0 (no decisions) — accept and skip strict checks — or a
        # full response with model.model_scope == 'symbol_only'.
        if body.get("n", 0) == 0:
            pytest.skip("no scalp decisions yet — skipping deep metric checks")
        model = body.get("model") or {}
        assert model.get("model_scope") == "symbol_only", model

    def test_metrics_with_account_id_scopes_model_key(
            self, admin_session, enabled_runner):
        acct_id = enabled_runner["account_id"]
        r = admin_session.get(
            f"{BASE_URL}/api/scalp/metrics",
            params={"symbol": "EURUSD", "account_id": acct_id}, timeout=20,
        )
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        # window/lifetime/CI keys must be present whenever n > 0
        if body.get("n", 0) == 0:
            pytest.skip("no decisions for this account_id — accepted (n=0)")
        window = body.get("window")
        assert isinstance(window, dict)
        assert window.get("type") == "rolling"
        assert window.get("max_samples") == 5000
        assert "n" in window and "from_ts_ms" in window and "to_ts_ms" in window
        lifetime = body.get("lifetime")
        assert isinstance(lifetime, dict)
        assert "n" in lifetime and "net_expectancy_pips" in lifetime
        # alpha.net_expectancy_ci95_pips exists (None ok below 20 samples)
        alpha = body.get("alpha") or {}
        assert "net_expectancy_ci95_pips" in alpha, alpha
        ci = alpha["net_expectancy_ci95_pips"]
        if ci is not None:
            assert isinstance(ci, list) and len(ci) == 2
            assert ci[0] <= ci[1]
        # model_scope must be the runner model_key (broker|type|symbol) NOT
        # the string 'symbol_only'
        model = body.get("model") or {}
        scope = model.get("model_scope")
        assert scope and scope != "symbol_only", model
        # commission_check present (mirrors runner.commission_check)
        assert "commission_check" in body


# --------------------------------------------------------------------------
# P0 — snapshot propagation: HB immediately refreshes heartbeat_age_sec
# --------------------------------------------------------------------------
class TestP0SnapshotPropagation:
    def _find_bridge_token_for(self, account_id: str) -> str | None:
        """Load bridge_token for a given runner.account_id from Mongo.
        We do NOT modify anything — just look up the token that the live EA
        is already using so our test heartbeat lands on the same doc."""
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

        async def _lookup():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            try:
                db = client[os.environ["DB_NAME"]]
                # account_id is the string form of the Mongo _id
                from bson import ObjectId
                doc = await db.accounts.find_one(
                    {"_id": ObjectId(account_id)},
                    {"bridge_token": 1, "balance": 1, "equity": 1,
                     "margin": 1, "free_margin": 1},
                )
                return doc
            finally:
                client.close()

        return asyncio.run(_lookup())

    def test_heartbeat_resets_heartbeat_age(self, admin_session, enabled_runner):
        acct_id = enabled_runner["account_id"]
        doc = self._find_bridge_token_for(acct_id)
        if not doc or not doc.get("bridge_token"):
            pytest.skip(f"no bridge_token for enabled runner {acct_id}")

        # Craft a minimal HB using the account's own current numbers so we
        # never distort live equity/margin values.
        payload = {
            "bridge_token": doc["bridge_token"],
            "balance": float(doc.get("balance") or 0.0),
            "equity": float(doc.get("equity") or 0.0),
            "margin": float(doc.get("margin") or 0.0),
            "free_margin": float(doc.get("free_margin") or 0.0),
            "margin_level": 0.0,
        }
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat", json=payload, timeout=15,
        )
        assert r.status_code == 200, f"heartbeat failed: {r.status_code} {r.text[:300]}"

        # Immediately re-fetch scalp/status — the runner's broker_state
        # heartbeat_age_sec should be very small (a couple of seconds at most)
        # WITHOUT any tick batch having landed in between.
        time.sleep(0.5)
        s = admin_session.get(
            f"{BASE_URL}/api/scalp/status",
            params={"account_id": acct_id}, timeout=15,
        )
        assert s.status_code == 200, s.text[:300]
        runners = s.json().get("runners") or []
        # pick the same enabled EURUSD runner
        r_state = next(
            (x for x in runners
             if x.get("account_id") == acct_id
             and x.get("symbol", "").upper() == "EURUSD"),
            None,
        )
        assert r_state is not None, f"runner disappeared for {acct_id}"
        age = (r_state.get("broker_state") or {}).get("heartbeat_age_sec")
        assert age is not None, "heartbeat_age_sec None after fresh HB"
        # Loose bound: 8s (RTT + backend jitter). Fresh HB should be ~<2s.
        assert age < 8, f"snapshot didn't propagate — heartbeat_age_sec={age}s"

    def test_broker_state_stale_reason_cleared_for_enabled(
            self, admin_session, enabled_runner):
        # If the P0 propagation works, an enabled + streaming runner must
        # not carry the 'broker state unknown / stale' block.
        stale = enabled_runner.get("broker_state_stale")
        assert stale is None or stale == "", (
            f"enabled runner has broker_state_stale reason: {stale!r}")


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
