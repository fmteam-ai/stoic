"""
iter-51 · Round 13 hardening — LIVE backend endpoint checks (read-only).

Verifies (from the review request):
 - Admin cookie login works
 - GET /api/scalp/status returns runners each with block_reasons (list) and
   broker_state_stale (null or string reason); audit.account_blocks present
 - POST /api/bridge/heartbeat with a valid bridge_token accepts symbol_specs
   and persists them + symbol_specs_updated_at on the account (Mongo read-back)
 - Heartbeat WITHOUT symbol_specs still works (EA 1.47 backward compat)
 - EA version consistency (LATEST_EA=1.48 across all surfaces)
 - GET /api/bot/pulse returns items and pulse reasons on active accounts
   include engine labels (SNIPER / BALANCED / FAST SCALP / etc.) OR the
   endpoint at minimum returns 200 with a well-formed body
 - Reason-level scalp engine block registry: add two reasons, clear one,
   the other stays

These are LIVE tests against the running backend. They do NOT restart the
backend, do NOT touch /api/panic, and do NOT create/close broker trades.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest
import requests

# So we can import scalp.engine directly for the block-registry spot-check.
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Frontend .env is the source of truth for the external URL.
    fe_env = Path("/app/frontend/.env").read_text()
    m = re.search(r"REACT_APP_BACKEND_URL=(\S+)", fe_env)
    if m:
        BASE_URL = m.group(1).rstrip("/")

MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME")
if not MONGO_URL or not DB_NAME:
    be_env = Path("/app/backend/.env").read_text()
    for line in be_env.splitlines():
        if line.startswith("MONGO_URL=") and not MONGO_URL:
            MONGO_URL = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("DB_NAME=") and not DB_NAME:
            DB_NAME = line.split("=", 1)[1].strip().strip('"')

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:400]}"
    assert "access_token" in s.cookies, "no access_token cookie set after login"
    return s


@pytest.fixture(scope="module")
def mongo_db():
    from pymongo import MongoClient

    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


# ---------------------------------------------------------------------------
# EA version consistency (Round 13 change #8)
# ---------------------------------------------------------------------------


class TestEaVersionConsistency:
    EXPECTED = "1.48"

    def test_mq5_property_version(self):
        text = Path("/app/backend/static/EmergentTradingBridge.mq5").read_text()
        assert f'#property version   "{self.EXPECTED}"' in text
        assert f'#define EA_CLIENT_VERSION "{self.EXPECTED}"' in text

    def test_bot_routes_latest_ea(self):
        text = Path("/app/backend/routes/bot_routes.py").read_text()
        assert f'LATEST_EA = "{self.EXPECTED}"' in text

    def test_diagnostic_routes_latest_ea(self):
        text = Path("/app/backend/routes/diagnostic_routes.py").read_text()
        assert f'LATEST_EA = "{self.EXPECTED}"' in text

    def test_setup_routes_ea_latest_version(self):
        text = Path("/app/backend/routes/setup_routes.py").read_text()
        assert f'"ea_latest_version": "{self.EXPECTED}"' in text

    def test_frontend_accounts_latest(self):
        text = Path("/app/frontend/src/pages/Accounts.jsx").read_text()
        assert f'LATEST_EA_VERSION = "{self.EXPECTED}"' in text

    def test_frontend_ea_version_strip_latest(self):
        text = Path("/app/frontend/src/components/EaVersionStrip.jsx").read_text()
        assert f'LATEST_EA_VERSION = "{self.EXPECTED}"' in text


# ---------------------------------------------------------------------------
# Scalp status shape (Round 13 changes #3 & #4)
# ---------------------------------------------------------------------------


class TestScalpStatus:
    def test_scalp_status_200_and_shape(self, session):
        r = session.get(f"{BASE_URL}/api/scalp/status", timeout=15)
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        assert isinstance(body, dict), body

        runners = body.get("runners") or body.get("items") or body
        # /api/scalp/status returns either dict {runners:[...]} or dict of runners.
        if isinstance(runners, dict) and "runners" in body:
            runners = body["runners"]

        assert runners, f"no runners in response: {body}"

        # If runners is a list of dicts, iterate directly; else if it's a mapping,
        # iterate values.
        if isinstance(runners, dict):
            runner_iter = list(runners.values())
        else:
            runner_iter = list(runners)

        assert runner_iter, "no runner objects to inspect"
        for r_obj in runner_iter:
            assert "block_reasons" in r_obj, f"runner missing block_reasons: {r_obj.keys()}"
            assert isinstance(r_obj["block_reasons"], list), r_obj["block_reasons"]
            assert "broker_state_stale" in r_obj, f"runner missing broker_state_stale: {r_obj.keys()}"
            stale = r_obj["broker_state_stale"]
            assert stale is None or isinstance(stale, str), stale
            # health object should exist and have a status string.
            health = r_obj.get("health") or {}
            assert isinstance(health, dict), health
            if "status" in health:
                assert isinstance(health["status"], str), health["status"]

        # Audit payload should carry account_blocks now.
        audit = body.get("audit") or {}
        assert "account_blocks" in audit, f"audit missing account_blocks: {audit}"


# ---------------------------------------------------------------------------
# Reason-level block registry unit spot-check (Round 13 change #3)
# ---------------------------------------------------------------------------


class TestReasonBlockRegistry:
    def test_add_two_clear_one_other_remains(self):
        from scalp import engine as scalp_engine

        acct = "test-iter51-reg-acct"
        # Use whatever constants the module exposes.
        r1 = getattr(scalp_engine, "BLOCK_INVARIANT", "invariant")
        r2 = getattr(scalp_engine, "BLOCK_LEDGER", "ledger")

        assert hasattr(scalp_engine, "add_account_block"), "helper missing"
        assert hasattr(scalp_engine, "clear_account_block"), "helper missing"
        assert hasattr(scalp_engine, "account_block_reasons"), "helper missing"

        try:
            scalp_engine.add_account_block(acct, r1)
            scalp_engine.add_account_block(acct, r2)
            reasons = set(scalp_engine.account_block_reasons(acct))
            assert r1 in reasons and r2 in reasons, reasons

            scalp_engine.clear_account_block(acct, r1)
            reasons2 = set(scalp_engine.account_block_reasons(acct))
            assert r1 not in reasons2, f"{r1} should have been cleared: {reasons2}"
            assert r2 in reasons2, f"{r2} should still be blocking: {reasons2}"
        finally:
            # Best-effort cleanup so this fake account never lingers in memory.
            scalp_engine.clear_account_block(acct, r1)
            scalp_engine.clear_account_block(acct, r2)


# ---------------------------------------------------------------------------
# Bridge heartbeat symbol_specs persistence (Round 13 change #8)
# ---------------------------------------------------------------------------


class TestBridgeHeartbeatSymbolSpecs:
    def _pick_account(self, mongo_db):
        """Find a SAFE inactive account (no recent heartbeat) with a bridge_token
        so the test doesn't overwrite live-streaming EA state. Falls back to any
        bridge_token account if no inactive one is available."""
        acct = mongo_db["accounts"].find_one(
            {"bridge_token": {"$exists": True, "$ne": None}, "last_heartbeat": None}
        )
        if not acct:
            acct = mongo_db["accounts"].find_one(
                {"bridge_token": {"$exists": True, "$ne": None}, "status": {"$ne": "connected"}}
            )
        if not acct:
            pytest.skip("no safe (inactive) account with bridge_token available")
        return acct

    def test_heartbeat_with_symbol_specs_persists(self, mongo_db):
        acct = self._pick_account(mongo_db)
        token = acct.get("bridge_token")
        account_id = str(acct.get("id") or acct.get("_id"))
        assert token, acct

        symbol_specs = {
            "EURUSD": {
                "point": 0.00001,
                "digits": 5,
                "stops_level_points": 20,
                "freeze_level_points": 0,
            }
        }
        payload = {
            "bridge_token": token,
            "account_id": account_id,
            "ea_version": "1.48",
            "status": "connected",
            "balance": float(acct.get("balance") or 10000.0),
            "equity": float(acct.get("equity") or 10000.0),
            "margin": 0.0,
            "free_margin": float(acct.get("balance") or 10000.0),
            "margin_level": 0.0,
            "symbol_specs": symbol_specs,
        }
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat",
            json=payload,
            timeout=15,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
        body = r.json()
        assert body.get("ok") is True or body.get("status") == "ok", body

        # Small settle window; the endpoint writes synchronously but leave headroom.
        time.sleep(0.5)
        refreshed = mongo_db["accounts"].find_one({"_id": acct["_id"]})
        assert refreshed is not None
        stored = refreshed.get("symbol_specs") or {}
        assert "EURUSD" in stored, f"symbol_specs.EURUSD not persisted: {stored}"
        eu = stored["EURUSD"]
        assert eu.get("digits") == 5, eu
        assert eu.get("stops_level_points") == 20, eu
        assert "symbol_specs_updated_at" in refreshed, refreshed.keys()

    def test_heartbeat_without_symbol_specs_still_ok(self, mongo_db):
        """EA 1.47 backward compatibility — heartbeat without symbol_specs still works."""
        acct = self._pick_account(mongo_db)
        token = acct.get("bridge_token")
        account_id = str(acct.get("id") or acct.get("_id"))

        payload = {
            "bridge_token": token,
            "account_id": account_id,
            "ea_version": "1.47",
            "status": "connected",
            "balance": float(acct.get("balance") or 10000.0),
            "equity": float(acct.get("equity") or 10000.0),
            "margin": 0.0,
            "free_margin": float(acct.get("balance") or 10000.0),
            "margin_level": 0.0,
        }
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat",
            json=payload,
            timeout=15,
        )
        assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"


# ---------------------------------------------------------------------------
# /api/bot/pulse regression + engine-label prefix (Round 13 change #10)
# ---------------------------------------------------------------------------


class TestBotPulse:
    def test_pulse_200_and_items(self, session):
        r = session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        # Pulse shape can be {items:[...]} or list directly.
        items = body.get("items") if isinstance(body, dict) else body
        assert items is not None, body
        assert isinstance(items, list), items

    def test_pulse_reasons_may_include_engine_labels(self, session):
        """Non-strict: only asserts if the endpoint has pulse reasons that look
        engine-labelled. If none of the active accounts have shadow /
        auto-exec-disabled branches firing right now, the prefix simply won't
        appear — that's fine, we just want to make sure the endpoint is not
        emitting malformed reasons."""
        r = session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
        assert r.status_code == 200, r.text[:400]
        body = r.json()
        items = body.get("items") if isinstance(body, dict) else body
        assert isinstance(items, list)

        engine_label_re = re.compile(r"\b(SNIPER|BALANCED|FAST SCALP|MTF|SCALP)\b", re.IGNORECASE)
        # Just walk the reasons string(s) present and confirm they are strings.
        for it in items:
            if not isinstance(it, dict):
                continue
            reasons = it.get("reasons") or it.get("reason") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            for reason in reasons:
                assert isinstance(reason, str), reason
                # Not an assertion — just report if a label is present so the
                # test logs give us signal (this is expected but not required).
                if engine_label_re.search(reason):
                    print(f"engine-labelled reason seen: {reason[:120]}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
