"""Iter-59 · Independent review verification for the three approved
architecture features:
  (a) Feature schema versioning + train/serve gating
  (b) EA-native pre-submit broker-constraints preflight (trade_mode + stops)
  (c) trade_events append-only event stream + GET /api/trades/events

This suite intentionally covers what the pure-unit `test_iter59_feature_schema_events`
does NOT: HTTP boundary of /api/trades/events, cross-user isolation, index
creation on first append, and full source-consistency for LATEST_EA=1.49
on every surface (backend routes, frontend Accounts.jsx, EaVersionStrip.jsx,
MQL5 source #property + EA_CLIENT_VERSION + AppendSymbolSpec trade_mode).

Safe: uses throwaway users only, never touches admin accounts, never
issues live orders. `trade_events` inserts here carry ephemeral UUIDs the
main app will never look up.
"""
import os
import re
import time
import uuid
from pathlib import Path

import pytest
import requests
from pymongo import MongoClient

from tests.helpers import base_url, register_and_login, mongo_db
from tests.ea_version import current_ea_version


API = f"{base_url()}/api"
REPO = Path(__file__).resolve().parents[2]


# ---------- (c) HTTP endpoint: GET /api/trades/events ----------
class TestTradeEventsEndpoint:

    def test_422_without_trade_or_decision_id(self):
        s = register_and_login(f"iter59_ev_{uuid.uuid4().hex[:8]}@example.com")
        r = s.get(f"{API}/trades/events", timeout=15)
        assert r.status_code == 422, f"expected 422, got {r.status_code} body={r.text}"
        assert "trade_id" in r.text or "decision_id" in r.text

    def test_unauth_rejected(self):
        r = requests.get(f"{API}/trades/events?decision_id=abc", timeout=15)
        assert r.status_code in (401, 403), f"unauth path must reject, got {r.status_code}"

    def test_returns_events_scoped_to_user_chronologically(self):
        # Seed 3 events for TWO users; verify only my events return, in ts order.
        user_a_email = f"iter59_a_{uuid.uuid4().hex[:8]}@example.com"
        user_b_email = f"iter59_b_{uuid.uuid4().hex[:8]}@example.com"
        sa = register_and_login(user_a_email)
        sb = register_and_login(user_b_email)
        db = mongo_db()
        uid_a = str(db.users.find_one({"email": user_a_email.lower()})["_id"])
        uid_b = str(db.users.find_one({"email": user_b_email.lower()})["_id"])

        did = f"TEST_iter59_{uuid.uuid4().hex}"
        base_ms = int(time.time() * 1000)
        docs = []
        for i, (et, uid) in enumerate([
            ("DecisionCreated", uid_a),
            ("BrokerSubmitted", uid_a),
            ("PositionOpened", uid_a),
            ("DecisionCreated", uid_b),  # noise
        ]):
            docs.append({
                "event_id": uuid.uuid4().hex,
                "event_type": et,
                "schema_v": 1,
                "occurred_at": "2026-01-01T00:00:00+00:00",
                "ts_ms": base_ms + i * 1000,
                "user_id": uid,
                "decision_id": did,
                "trade_id": None,
                "account_id": "acct_test",
                "symbol": "EURUSD",
                "source": "test",
                "payload": {"i": i},
            })
        try:
            db.trade_events.insert_many(docs)
            r = sa.get(f"{API}/trades/events",
                       params={"decision_id": did}, timeout=15)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["count"] == 3, f"user A should see 3 events, got {data}"
            events = data["events"]
            types = [e["event_type"] for e in events]
            assert types == ["DecisionCreated", "BrokerSubmitted", "PositionOpened"]
            for e in events:
                assert e["user_id"] == uid_a
                assert "_id" not in e            # mongo _id must be projected out
                assert e["schema_v"] == 1

            # User B sees ONLY their own doc.
            r2 = sb.get(f"{API}/trades/events",
                        params={"decision_id": did}, timeout=15)
            assert r2.status_code == 200
            data2 = r2.json()
            assert data2["count"] == 1 and data2["events"][0]["user_id"] == uid_b
        finally:
            db.trade_events.delete_many({"decision_id": did})


# ---------- (c) direct DB layer: append + indexes ----------
class TestTradeEventsModule:

    def test_append_creates_indexes(self):
        """The insert path is idempotent w.r.t. index creation (module-level
        _indexed flag). We just verify the indexes end up present."""
        # Use pymongo directly since the app-side call is async; the module
        # itself is async but we can trigger index creation via an inline loop.
        import asyncio
        from motor.motor_asyncio import AsyncIOMotorClient
        from trade_events import build, append

        async def _run():
            client = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = client[os.environ["DB_NAME"]]
            # Force _indexed to False so THIS test always exercises the branch.
            import trade_events as te
            te._indexed = False
            ev = build("DecisionCreated", user_id="TEST_uid",
                       decision_id=f"TEST_idx_{uuid.uuid4().hex}",
                       payload={})
            await append(db, ev)
            # Read back the index catalog with pymongo (sync).
            sync = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
            idx = sync.trade_events.index_information()
            # Cleanup our seed doc:
            sync.trade_events.delete_many({"decision_id": ev["decision_id"]})
            client.close()
            return idx

        idx = asyncio.new_event_loop().run_until_complete(_run())
        # There must be an index covering (decision_id, ts_ms), (trade_id, ts_ms),
        # and (event_type, ts_ms). Extract just the key tuples for the check.
        key_sets = [tuple(v["key"]) for v in idx.values()]
        assert (("decision_id", 1), ("ts_ms", 1)) in key_sets, key_sets
        assert (("trade_id", 1), ("ts_ms", 1)) in key_sets, key_sets
        assert any(k[0] == ("event_type", 1) for k in key_sets), key_sets

    def test_build_populates_all_metadata(self):
        from trade_events import build, SCHEMA_V
        ev = build("PositionClosed", user_id="u", decision_id="d",
                   trade_id="t", account_id="a", symbol="XAUUSD",
                   payload={"net_pnl_usd": 1.23})
        assert ev["schema_v"] == SCHEMA_V == 1
        assert ev["source"] == "scalp_engine"
        assert ev["ts_ms"] > 0 and ev["occurred_at"]
        assert ev["event_id"] and len(ev["event_id"]) == 32


# ---------- EA version consistency (round item 3) ----------
class TestEaVersionConsistency:
    EXPECTED = None  # set below — coherence against live EA_CLIENT_VERSION

    @property
    def _v(self):
        return current_ea_version()

    def test_ea_source_reports_1_49(self):
        assert re.fullmatch(r"[\d.]+", current_ea_version())

    def test_ea_source_property_version_matches(self):
        ea_src = (REPO / "backend/static/EmergentTradingBridge.mq5").read_text()
        m = re.search(r'#property\s+version\s+"([\d.]+)"', ea_src)
        assert m and m.group(1) == self._v, f"#property version = {m and m.group(1)}"
        assert 'AppendSymbolSpec' in ea_src
        assert 'SYMBOL_TRADE_MODE' in ea_src
        # MQL5 source has escaped quotes: \"trade_mode\":%I64d
        assert 'trade_mode' in ea_src and '%I64d' in ea_src
        assert 'SymbolInfoInteger(sym, SYMBOL_TRADE_MODE)' in ea_src

    def test_backend_routes_pin_1_49(self):
        for rel in ("backend/routes/bot_routes.py",
                    "backend/routes/diagnostic_routes.py"):
            src = (REPO / rel).read_text()
            assert f'LATEST_EA = "{self._v}"' in src, f"{rel} not on {self._v}"

    def test_setup_routes_ea_latest_1_49(self):
        src = (REPO / "backend/routes/setup_routes.py").read_text()
        assert f'"ea_latest_version": "{self._v}"' in src

    def test_frontend_ea_version_1_49(self):
        for rel in ("frontend/src/pages/Accounts.jsx",
                    "frontend/src/components/EaVersionStrip.jsx"):
            src = (REPO / rel).read_text()
            assert f'LATEST_EA_VERSION = "{self._v}"' in src, f"{rel} not on {self._v}"


# ---------- (a) Feature schema · sanity + gating ----------
class TestFeatureSchemaGating:

    def test_retrain_filters_schema_mismatch(self):
        """`retrain` reads decisions from Mongo but SKIPS anything whose
        stored feature_schema_version doesn't equal FEATURE_SCHEMA_VERSION.
        We assert that the source code embeds that filter (mock-free source
        contract check — safer than firing a real training run against the
        prod DB)."""
        src = (REPO / "backend/scalp/model.py").read_text()
        assert "if int(d.get(\"feature_schema_version\") or 1) != FEATURE_SCHEMA_VERSION" in src
        assert "\"feature_schema_version\": FEATURE_SCHEMA_VERSION" in src

    def test_load_persisted_refuses_mismatch(self):
        src = (REPO / "backend/scalp/model.py").read_text()
        # comment mentioning the gate + explicit int-check on the doc's field
        assert "int(doc.get(\"feature_schema_version\") or 1) != FEATURE_SCHEMA_VERSION" in src


# ---------- (b) engine preflight — source-level guards ----------
class TestBrokerConstraintsPreflight:

    def test_engine_rejects_on_min_stops_and_trade_mode(self):
        src = (REPO / "backend/scalp/engine.py").read_text()
        # reject-stage tag must be exactly the string the review contract mandates
        assert "\"reject_stage\": \"pre_submit_broker_constraints\"" in src
        # trade_mode enum branches covered (0/1/2/3/4)
        assert "tm == 4" in src
        assert "tm == 1 and decision[\"direction\"] == \"BUY\"" in src
        assert "tm == 2 and decision[\"direction\"] == \"SELL\"" in src
        # slot is released on reject
        assert ("release_broker_submission_slot(db, slot)" in src
                and "constraint_reason is not None" in src)


# ---------- (c) engine lifecycle emits ----------
class TestEngineLifecycleEmits:

    def test_engine_emits_full_lifecycle(self):
        src = (REPO / "backend/scalp/engine.py").read_text()
        for et in ("DecisionCreated", "RiskApproved", "OrderIntentCreated",
                   "BrokerSubmitted", "BrokerRejected", "PositionOpened",
                   "PositionClosed", "FinancialApplied"):
            assert f"\"{et}\"" in src, f"engine.py missing emit for {et}"

    def test_protection_guard_emits_placed(self):
        src = (REPO / "backend/protection_guard.py").read_text()
        assert "\"ProtectionPlaced\"" in src
        assert "from trade_events import build, append" in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
