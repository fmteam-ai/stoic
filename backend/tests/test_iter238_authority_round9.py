from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-238 — audit round 9 P0-01 / P0-02 / P0-03 acceptance tests.

ONE canonical decision dominates every surface; every new-order path is
denied server-side under any non-READY state; blockers clear only through two
fresh reconciliations + a stability window; the inventory projection keeps
expected/configured/enabled/connected/fresh/tradable apart and enforces the
1:1 enabled-account ↔ enabled-bot relation; UNKNOWN executions and stale
position truth dominate to CLOSE_ONLY; broker-event replay is idempotent.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


class EngineStub:
    def __init__(self):
        self.calls = []

    async def execute(self, *a, **kw):
        self.calls.append(kw)
        return {"ok": True, "order_id": "stub"}

    async def execute_authorized(self, *a, **kw):
        self.calls.append(kw)
        return {"ok": True, "order_id": "stub"}

    def __getattr__(self, name):
        async def _rec(*a, **kw):
            self.calls.append({"method": name})
            return {"ok": True}
        return _rec


@pytest.fixture
def live_account():
    db = _db()
    uid = f"iter238_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    n = str(700000 + int(uuid.uuid4().hex[:4], 16) % 90000)
    acc = {"_id": ObjectId(), "user_id": uid, "label": "R9-LIVE", "status": "connected", "mode": "live",
           "trading_enabled": True, "last_heartbeat": now, "open_positions": 0, "broker": "R9", "server": "R9-Live",
           "account_number": n, "account_login": n, "bridge_token": f"r9_{uuid.uuid4().hex}",
           "verified_identity": {"account_number": n, "broker_server": "R9-Live"},
           "balance": 10000, "equity": 10000, "base_currency": "USD", "created_at": now, "reconciliation_seq": 10}
    _run(db.accounts.insert_one(acc))
    _run(db.bot_configs.insert_one({"user_id": uid, "account_id": str(acc["_id"]), "active": True,
                                   "enabled": True, "strategy": "r9", "created_at": now}))
    yield acc
    aid = str(acc["_id"])
    _run(db.accounts.delete_many({"user_id": uid}))
    _run(db.bot_configs.delete_many({"user_id": uid}))
    _run(db.trades.delete_many({"account_id": aid}))
    _run(db.authority_stability.delete_many({"account_id": aid}))
    _run(db.execution_intents.delete_many({"account_id": aid}))
    _run(db.safety_blocks.delete_many({"account_id": aid}))


def _fresh(db, acc):
    return _run(db.accounts.find_one({"_id": acc["_id"]}))


def _decide(acc):
    from canonical_decision import decide_account
    return _run(decide_account(_db(), acc))


def _all_surfaces(acc):
    """decision object · enforce_new_trade · engine gate must agree."""
    from trading_authority import enforce_new_trade, gate_or_block
    db = _db()
    dec = _decide(acc)
    gate = _run(enforce_new_trade(db, acc))
    paper = _run(gate_or_block(db, acc, "paper_engine"))
    return dec, gate, paper


BLOCKERS = {
    "EXECUTION_UNKNOWN": lambda acc: {"_intent": {"status": "UNKNOWN"}},
    "POSITION_TRUTH_STALE": lambda acc: {"$set": {"last_heartbeat": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()}},
    "IDENTITY_MISMATCH": lambda acc: {"$set": {"broker_account_mismatch": True}},
    "CERTIFICATION_FAILED": lambda acc: {"$set": {"certification_status": "FAILED"}},
    "BOT_HEALTH_CRITICAL": lambda acc: {"$set": {"trading_blocked": True, "block_retcode_label": "TRADE_DISABLED"}},
    "PERFORMANCE_UNRECONCILED": lambda acc: {"_trade": {"status": "closed", "pnl_unknown": True,
                                                        "closed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}},
}
EXPECTED_STATE = {"EXECUTION_UNKNOWN": "CLOSE_ONLY", "POSITION_TRUTH_STALE": "CLOSE_ONLY", "IDENTITY_MISMATCH": "BLOCKED",
                  "CERTIFICATION_FAILED": "BLOCKED", "BOT_HEALTH_CRITICAL": "CLOSE_ONLY", "PERFORMANCE_UNRECONCILED": "CLOSE_ONLY"}


def _inject(db, acc, code):
    spec = BLOCKERS[code](acc)
    aid = str(acc["_id"])
    if "_intent" in spec:
        _run(db.execution_intents.insert_one({"intent_id": f"int_{uuid.uuid4().hex[:10]}", "account_id": aid,
                                              "user_id": acc["user_id"], "status": "unknown", "symbol": "EURUSD",
                                              "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                                              "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                                              "transitions": [{"to": "unknown", "at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}]}))
    elif "_trade" in spec:
        _run(db.trades.insert_one({"account_id": aid, "user_id": acc["user_id"], "symbol": "EURUSD", **spec["_trade"]}))
    else:
        _run(db.accounts.update_one({"_id": acc["_id"]}, spec))


class TestCanonicalDecisionDominates:
    def test_clean_live_account_is_ready_on_every_surface(self, live_account):
        dec, gate, paper = _all_surfaces(_fresh(_db(), live_account))
        assert dec["state"] == "READY" and dec["new_exposure_allowed"], dec["blockers"]
        assert gate["ok"] is True and paper is None

    @pytest.mark.parametrize("code", sorted(BLOCKERS))
    def test_each_blocker_alone_dominates_every_surface(self, live_account, code):
        db = _db()
        _inject(db, live_account, code)
        acc = _fresh(db, live_account)
        dec, gate, paper = _all_surfaces(acc)
        assert code in dec["reason_codes"], (code, dec["reason_codes"])
        assert dec["state"] == EXPECTED_STATE[code], (code, dec["state"], dec["blockers"])
        assert dec["new_exposure_allowed"] is False
        assert gate["ok"] is False and gate["state"] == dec["state"] and code in gate["reason_codes"]
        assert paper and paper["blocked"] == "trading_authority" and paper["state"] == dec["state"]
        assert code in paper["reason_codes"]

    def test_simultaneous_blockers_follow_severity_dominance(self, live_account, monkeypatch):
        from canonical_decision import dominant, STATES, from_snapshot
        assert STATES == ["READY", "DEGRADED", "CLOSE_ONLY", "BLOCKED", "EMERGENCY"]
        assert dominant("READY", "CLOSE_ONLY", "DEGRADED") == "CLOSE_ONLY"
        assert dominant("BLOCKED", "CLOSE_ONLY") == "BLOCKED"
        assert dominant("EMERGENCY", "BLOCKED") == "EMERGENCY"
        db = _db()
        _inject(db, live_account, "EXECUTION_UNKNOWN")
        _inject(db, live_account, "IDENTITY_MISMATCH")
        dec = _decide(_fresh(db, live_account))
        assert dec["state"] == "BLOCKED" and dec["dominant_code"] == "IDENTITY_MISMATCH"
        assert "EXECUTION_UNKNOWN" in dec["reason_codes"]
        snap = {"domains": {"a": {"level": "EMERGENCY", "reason": "x"}, "b": {"level": "LOCKED", "reason": "y"},
                            "c": {"level": "REDUCED", "reason": "z"}}, "level": "EMERGENCY", "hard_truth_fresh": True}
        d = from_snapshot(snap)
        assert d["state"] == "EMERGENCY" and [b["state"] for b in d["blockers"]] == ["EMERGENCY", "BLOCKED", "DEGRADED"]

    @pytest.mark.parametrize("code", ["EXECUTION_UNKNOWN", "IDENTITY_MISMATCH", "BOT_HEALTH_CRITICAL"])
    def test_every_new_order_path_denies_without_broker_submission(self, live_account, code):
        from execution_authority import submit_intent
        from execution import PaperEngine
        db = _db()
        _inject(db, live_account, code)
        acc = _fresh(db, live_account)
        engine = EngineStub()
        sig = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01, "entry_price": 1.1, "stop_loss": 1.09,
               "take_profit": 1.12, "confidence": 80, "strategy": "r9"}
        res = _run(submit_intent(user_id=acc["user_id"], account=acc, signal=dict(sig), engine=engine))
        assert res.get("blocked"), res
        assert engine.calls == []
        acc_paper = {**acc, "mode": "paper"} if code != "EXECUTION_UNKNOWN" else acc
        res2 = _run(PaperEngine().execute(user_id=acc["user_id"], account=acc_paper, signal=dict(sig)))
        assert res2.get("blocked") == "trading_authority" and res2["path"] == "paper_engine"
        assert _run(db.trades.count_documents({"account_id": str(acc["_id"]), "status": {"$in": ["pending", "open"]}})) == 0

    def test_binance_engine_is_gated(self):
        src = open(os.path.join(_BACKEND_DIR, "crypto_bridge", "binance_engine.py")).read()
        assert 'gate_or_block(db, account, "binance_engine")' in src

    def test_recovery_requires_two_reconciliations_and_window(self, live_account, monkeypatch):
        db = _db()
        monkeypatch.setenv("AUTHORITY_STABILITY_WINDOW_SECONDS", "120")
        _inject(db, live_account, "IDENTITY_MISMATCH")
        assert _decide(_fresh(db, live_account))["state"] == "BLOCKED"
        # clear the blocker → still not READY: recovery window
        _run(db.accounts.update_one({"_id": live_account["_id"]}, {"$unset": {"broker_account_mismatch": ""}}))
        dec = _decide(_fresh(db, live_account))
        assert dec["state"] == "CLOSE_ONLY" and "RECOVERY_WINDOW" in dec["reason_codes"]
        # two fresh reconciliations but window not elapsed → still recovering
        _run(db.accounts.update_one({"_id": live_account["_id"]}, {"$inc": {"reconciliation_seq": 2}}))
        dec = _decide(_fresh(db, live_account))
        assert "RECOVERY_WINDOW" in dec["reason_codes"]
        # window elapsed → READY
        _run(db.authority_stability.update_one({"account_id": str(live_account["_id"])},
                                               {"$set": {"restricted_at": (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()}}))
        dec = _decide(_fresh(db, live_account))
        assert dec["state"] == "READY", dec["blockers"]
        # only one reconciliation → not enough
        _inject(db, live_account, "IDENTITY_MISMATCH")
        _decide(_fresh(db, live_account))
        _run(db.accounts.update_one({"_id": live_account["_id"]}, {"$unset": {"broker_account_mismatch": ""}, "$inc": {"reconciliation_seq": 1}}))
        _run(db.authority_stability.update_one({"account_id": str(live_account["_id"])},
                                               {"$set": {"restricted_at": (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()}}))
        assert "RECOVERY_WINDOW" in _decide(_fresh(db, live_account))["reason_codes"]

    def test_live_decision_endpoints_agree(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        dec = s.get(f"{API}/authority/decision", timeout=TIMEOUT).json()
        auth = s.get(f"{API}/authority", timeout=TIMEOUT).json()
        assert dec["state"] in ("READY", "DEGRADED", "CLOSE_ONLY", "BLOCKED", "EMERGENCY")
        assert auth["decision"]["state"] == dec["state"]
        lvl_for = {"READY": "FULL", "DEGRADED": "REDUCED", "CLOSE_ONLY": "CLOSE_ONLY", "BLOCKED": "LOCKED", "EMERGENCY": "EMERGENCY"}
        from trading_authority import level_severity
        assert level_severity(auth["enforced_level"]) >= level_severity(lvl_for[dec["state"]])
        assert auth["enforced_level"] == auth["level"]
        inv = s.get(f"{API}/authority/inventory", timeout=TIMEOUT).json()
        for k in ("configured", "enabled", "live_enabled", "bots_enabled", "connected", "fresh", "tradable"):
            assert k in inv["counts"]
        assert requests.get(f"{API}/authority/decision", timeout=TIMEOUT).status_code in (401, 403)


class TestInventoryProjection:
    @pytest.fixture
    def fleet(self):
        db = _db()
        uid = f"iter238inv_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for i, (mode, enabled) in enumerate([("live", True), ("live", True), ("live", True),
                                             ("live", False), ("demo", False), ("demo", False)]):
            n = str(810000 + i)
            rows.append({"_id": ObjectId(), "user_id": uid, "label": f"INV-{i}", "mode": mode, "trading_enabled": enabled,
                         "status": "connected", "last_heartbeat": now, "open_positions": 0, "account_number": n,
                         "account_login": n, "bridge_token": f"inv_{uuid.uuid4().hex}",
                         "verified_identity": {"account_number": n, "broker_server": "X"}, "created_at": now})
        _run(db.accounts.insert_many(rows))
        for r in rows:
            _run(db.bot_configs.insert_one({"user_id": uid, "account_id": str(r["_id"]), "active": r["trading_enabled"], "created_at": now}))
        saved = _run(db.platform_state.find_one({"_id": "inventory_expectation"}))
        _run(db.platform_state.delete_one({"_id": "inventory_expectation"}))
        yield uid, rows
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.bot_configs.delete_many({"user_id": uid}))
        _run(db.inventory_config_events.delete_many({"actor": "iter238@stoic.test"}))
        _run(db.platform_state.delete_one({"_id": "inventory_expectation"}))
        if saved:
            _run(db.platform_state.replace_one({"_id": "inventory_expectation"}, saved, upsert=True))

    def test_six_three_three_matches_and_counts_stay_separate(self, fleet):
        from inventory_projection import projection, set_expectation, approve_current
        uid, rows = fleet
        db = _db()
        _run(set_expectation(db, {"accounts": 6, "enabled": 3, "bots": 3, "scope_user_id": uid,
                                  "account_ids": [str(r["_id"]) for r in rows[:3]]}, "iter238@stoic.test"))
        p = _run(projection(db, uid))
        c = p["counts"]
        assert (c["configured"], c["live_configured"], c["enabled"], c["live_enabled"], c["bots_enabled"]) == (6, 4, 3, 3, 3)
        assert c["connected"] == 6 and c["fresh"] == 6 and c["tradable"] == 3
        assert p["violations"] == [] and p["blocking"] is False
        p = _run(approve_current(db, "iter238@stoic.test", "initial 6/3/3 approval for drill", uid))
        assert p["pending_approval"]["inventory_hash"] == p["inventory_hash"] and p["approved_hash"] is None
        from inventory_projection import confirm_current
        p = _run(confirm_current(db, "iter238-second@stoic.test"))          # round 12 P2-05: second admin
        assert p["approved_hash"] == p["inventory_hash"] and not p["unapproved_change"]

    def test_fourth_enabled_or_lost_account_blocks_new_entries(self, fleet):
        from inventory_projection import projection, set_expectation, approve_current
        from trading_authority import inventory_domain
        uid, rows = fleet
        db = _db()
        _run(set_expectation(db, {"accounts": 6, "enabled": 3, "bots": 3, "scope_user_id": uid,
                                  "account_ids": [str(r["_id"]) for r in rows[:3]]}, "iter238@stoic.test"))
        _run(approve_current(db, "iter238@stoic.test", "initial 6/3/3 approval for drill", uid))
        from inventory_projection import confirm_current
        _run(confirm_current(db, "iter238-second@stoic.test"))
        assert _run(inventory_domain(db, None))["level"] == "FULL"
        # enable a fourth (account only → also breaks 1:1)
        _run(db.accounts.update_one({"_id": rows[3]["_id"]}, {"$set": {"trading_enabled": True}}))
        p = _run(projection(db, uid))
        assert p["blocking"] and any("!= expected 3" in v for v in p["violations"])
        assert any("relation broken" in v for v in p["violations"]) and p["unapproved_change"]
        assert _run(inventory_domain(db, None))["level"] == "CLOSE_ONLY"
        _run(db.accounts.update_one({"_id": rows[3]["_id"]}, {"$set": {"trading_enabled": False}}))
        # lose one of the three
        _run(db.accounts.update_one({"_id": rows[0]["_id"]}, {"$set": {"trading_enabled": False}}))
        _run(db.bot_configs.update_one({"account_id": str(rows[0]["_id"])}, {"$set": {"active": False}}))
        p = _run(projection(db, uid))
        assert p["blocking"] and p["counts"]["live_enabled"] == 2

    def test_demo_account_cannot_satisfy_live_requirement(self, fleet):
        from inventory_projection import projection, set_expectation
        uid, rows = fleet
        db = _db()
        _run(set_expectation(db, {"accounts": 6, "enabled": 3, "bots": 3, "scope_user_id": uid}, "iter238@stoic.test"))
        _run(db.accounts.update_one({"_id": rows[2]["_id"]}, {"$set": {"trading_enabled": False}}))
        _run(db.bot_configs.update_one({"account_id": str(rows[2]["_id"])}, {"$set": {"active": False}}))
        _run(db.accounts.update_one({"_id": rows[4]["_id"]}, {"$set": {"trading_enabled": True}}))
        _run(db.bot_configs.update_one({"account_id": str(rows[4]["_id"])}, {"$set": {"active": True}}))
        p = _run(projection(db, uid))
        assert p["counts"]["enabled"] == 3 and p["counts"]["live_enabled"] == 2 and p["counts"]["bots_enabled"] == 3
        assert any("demo/paper never satisfy" in v for v in p["violations"])

    def test_alias_change_keeps_immutable_id_reconciliation(self, fleet):
        from inventory_projection import projection, inventory_hash
        uid, rows = fleet
        db = _db()
        before = _run(projection(db, uid))
        _run(db.accounts.update_one({"_id": rows[0]["_id"]}, {"$set": {"label": "RENAMED-ALIAS"}}))
        after = _run(projection(db, uid))
        assert after["inventory_hash"] == before["inventory_hash"] == inventory_hash(after["accounts"])
        assert {r["account_id"] for r in after["accounts"]} == {r["account_id"] for r in before["accounts"]}
        assert all(r["broker_identity"]["verified"] for r in after["accounts"])


class TestExecutionTruthAndReplay:
    def test_unknown_execution_dominates_and_resolution_leaves_one_record(self, live_account):
        db = _db()
        aid = str(live_account["_id"])
        _inject(db, live_account, "EXECUTION_UNKNOWN")
        dec = _decide(_fresh(db, live_account))
        assert dec["state"] == "CLOSE_ONLY" and "EXECUTION_UNKNOWN" in dec["reason_codes"]
        intent = _run(db.execution_intents.find_one({"account_id": aid, "status": "unknown"}))
        # resolve from broker history (immutable ticket) — exactly one final record
        _run(db.execution_intents.update_one({"_id": intent["_id"]}, {"$set": {"status": "filled", "broker_ticket": 991001,
                                                                             "resolved_from": "broker_history"}}))
        assert _run(db.execution_intents.count_documents({"account_id": aid})) == 1
        dec = _decide(_fresh(db, live_account))
        assert "EXECUTION_UNKNOWN" not in dec["reason_codes"]

    def test_broker_event_replay_is_idempotent(self, live_account):
        from pymongo.errors import DuplicateKeyError
        db = _db()
        aid = str(live_account["_id"])
        ev = {"account_id": aid, "deal_id": 55123, "event_type": "DEAL_ADD", "ticket": 991001, "profit": 1.5}
        for col in (db.broker_deals, db.scalp_financial_events):
            _run(col.delete_many({"account_id": aid}))
            _run(col.insert_one(dict(ev)))
            with pytest.raises(DuplicateKeyError):
                _run(col.insert_one(dict(ev)))
            assert _run(col.count_documents({"account_id": aid, "deal_id": 55123})) == 1
            _run(col.delete_many({"account_id": aid}))

    def test_aged_position_snapshot_blocks_and_states_age(self, live_account):
        db = _db()
        _inject(db, live_account, "POSITION_TRUTH_STALE")
        dec = _decide(_fresh(db, live_account))
        assert dec["state"] == "CLOSE_ONLY" and "POSITION_TRUTH_STALE" in dec["reason_codes"]
        reasons = " ".join(b["reason"] for b in dec["blockers"])
        assert "STALE" in reasons and ("heartbeat" in reasons.lower() or "truth" in reasons.lower())
