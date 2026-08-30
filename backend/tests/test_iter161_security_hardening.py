"""iter-161 — deployment health auto-rollback, security runtime drills,
model lineage/replay validation, trace-id propagation."""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 60


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ─── deployment health scoring ───────────────────────────────────────
def test_fleet_health_score_structure():
    from deployment_health import score_fleet
    db = _db()
    uid = "iter161-score"

    async def scenario():
        now = datetime.now(timezone.utc)
        await db.vps_agents.insert_many([
            {"agent_id": f"{uid}-fresh", "user_id": uid,
             "last_heartbeat": now,
             "last_metrics": {"mt5_connected": True}},
            {"agent_id": f"{uid}-stale", "user_id": uid,
             "last_heartbeat": now - timedelta(hours=3),
             "last_metrics": {"mt5_connected": False}},
        ])
        try:
            return await score_fleet(db)
        finally:
            await db.vps_agents.delete_many({"user_id": uid})
    h = _run(scenario())
    assert 0 <= h["score"] <= 100
    assert h["fleet_size"] >= 2
    for k in ("heartbeat_fresh", "mt5_connected",
              "deployment_failed_2h", "command_failed_2h"):
        assert k in h["components"]


def test_auto_rollback_and_bake_complete():
    """deployment_failed alert during the bake window → automatic rollback
    (pinned); a clean expired bake clears the watch."""
    from deployment_health import watch_deployment
    from release_channels import STORE_DIR
    db = _db()
    now = datetime.now(timezone.utc)
    blob = b"iter161-artifact"
    sha = hashlib.sha256(blob).hexdigest()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / sha).write_bytes(blob)
    marker = f"iter161-{os.urandom(3).hex()}"

    async def scenario():
        original = await db.platform_state.find_one({"_id": "release_state"})
        try:
            await db.platform_state.replace_one(
                {"_id": "release_state"},
                {"_id": "release_state",
                 "stable": {marker: "bad-sha"},
                 "previous_stable": {marker: sha},
                 "deploy_watch": {"started_at": now.isoformat(),
                                  "baseline_score": 100.0,
                                  "artifacts": {marker: sha},
                                  "bake_hours": 4}},
                upsert=True)
            # tenantA/B must be RELEASE-TRUSTED for corroboration to count
            # (4th audit — only operator-trusted tenants move release state)
            await db.vps_agents.insert_many([
                {"agent_id": f"{marker}-tA", "user_id": f"{marker}-tenantA",
                 "release_trusted": True, "revoked": False},
                {"agent_id": f"{marker}-tB", "user_id": f"{marker}-tenantB",
                 "release_trusted": True, "revoked": False}])
            await db.ops_alerts.insert_many([
                {"kind": "deployment_failed", "severity": "critical",
                 "message": "iter161 synthetic a", "dedup_key": f"{marker}-a",
                 "meta": {"agent_id": f"{marker}-agentA",
                          "user_id": f"{marker}-tenantA", "sha256": sha},
                 "acked_at": None, "created_at": now},
                {"kind": "deployment_failed", "severity": "critical",
                 "message": "iter161 synthetic b", "dedup_key": f"{marker}-b",
                 "meta": {"agent_id": f"{marker}-agentB",
                          "user_id": f"{marker}-tenantB", "sha256": sha},
                 "acked_at": None, "created_at": now}])
            first = await watch_deployment(db)
            st = await db.platform_state.find_one({"_id": "release_state"})
            # clean bake: watch older than bake_hours, no new failures —
            # baseline pinned to the CURRENT live score so real fleet state
            # can't fake a degradation
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$in": [f"{marker}-a", f"{marker}-b"]}})
            from deployment_health import (_trusted_agent_filter,
                                            _trusted_user_ids, score_fleet)
            # make the trusted agents healthy so the clean bake completes
            await db.vps_agents.update_many(
                {"agent_id": {"$in": [f"{marker}-tA", f"{marker}-tB"]}},
                {"$set": {"last_heartbeat": now,
                          "last_metrics": {"mt5_connected": True}}})
            tu = await _trusted_user_ids(db)
            tlive = await score_fleet(db, _trusted_agent_filter(tu))
            await db.platform_state.update_one(
                {"_id": "release_state"},
                {"$set": {"deploy_watch": {
                    "started_at": (now - timedelta(hours=9)).isoformat(),
                    "baseline_score": tlive["score"], "bake_hours": 4}}})
            second = await watch_deployment(db)
            st2 = await db.platform_state.find_one({"_id": "release_state"})
            return first, st, second, st2
        finally:
            if original is not None:
                await db.platform_state.replace_one(
                    {"_id": "release_state"}, original, upsert=True)
            else:
                await db.platform_state.delete_one({"_id": "release_state"})
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$in": [f"{marker}-a", f"{marker}-b"]}})
            await db.ops_alerts.delete_many(
                {"kind": "deployment_auto_rollback",
                 "created_at": {"$gte": now}})
            await db.release_history.delete_many(
                {"event": {"$in": ["rollback", "auto_rollback",
                                   "bake_complete"]},
                 "at": {"$gte": now.isoformat()}})
            (STORE_DIR / sha).unlink(missing_ok=True)
            await db.vps_agents.delete_many(
                {"agent_id": {"$in": [f"{marker}-tA", f"{marker}-tB"]}})

    first, st, second, st2 = _run(scenario())
    assert first["status"] == "auto_rollback", first
    assert first["outcome"] == "rolled_back"
    assert st["stable"] == {marker: sha}, "previous stable not restored"
    assert st["pinned"] is True, "channel must be pinned after auto-rollback"
    assert "deploy_watch" not in st
    assert second["status"] == "bake_complete", second
    assert "deploy_watch" not in st2


def test_deployment_health_endpoint():
    s = _admin()
    r = s.get(f"{API}/ops/deployment-health", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "score" in body["health"]
    assert set(body["policy"]) == {"bake_hours", "min_score", "max_drop",
                                   "min_fail_tenants"}
    assert "release_trust" in body
    # unauthenticated blocked
    r = requests.get(f"{API}/ops/deployment-health", timeout=TIMEOUT)
    assert r.status_code == 403


# ─── security runtime drills ─────────────────────────────────────────
def test_security_drills_all_pass():
    from chaos_drills import (_drill_command_replay, _drill_invalid_signature,
                              _drill_token_expiry,
                              _drill_unauthorized_admin_access)
    db = _db()
    results = [
        _drill_invalid_signature(),
        _run(_drill_command_replay(db)),
        _run(_drill_token_expiry(db)),
        _drill_unauthorized_admin_access(),
    ]
    failed = [r for r in results if not r["passed"]]
    assert not failed, failed


# ─── model lineage + replay validation ───────────────────────────────
def test_model_lineage_stamp_and_replay():
    from model_lineage import model_version, replay_validate, stamp_lineage
    db = _db()
    signal = {"symbol": "EURUSD", "action": "BUY", "confidence": 71,
              "entry_price": 1.1, "stop_loss": 1.09, "rr_ratio": 2.0,
              "market_regime": "trend", "user_id": "iter161-lineage",
              "iter161": True}

    async def scenario():
        await stamp_lineage(db, signal)
        res = await db.signals.insert_one(dict(signal))
        try:
            out = await replay_validate(db, str(res.inserted_id))
            reg = await db.model_code_versions.find_one(
                {"_id": signal["model"]["version"]})
            missing = await replay_validate(db, "not-a-real-id")
            return out, reg, missing
        finally:
            await db.signals.delete_many({"user_id": "iter161-lineage"})
    out, reg, missing = _run(scenario())
    mv = model_version()
    assert signal["model"]["version"] == mv["version"]
    assert signal["model"]["version"].startswith("m-")
    assert "confidence" in signal["model"]["features"]
    assert len(signal["model"]["feature_set_hash"]) == 16
    assert reg is not None and reg.get("files"), "version not registered"
    assert out["verdict"] == "reproducible", out
    assert out["checks"]["model_version_match"] is True
    assert out["checks"]["feature_set_match"] is True
    assert missing is None


def test_model_version_and_replay_endpoints():
    s = _admin()
    r = s.get(f"{API}/ops/model-version", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["current"]["version"].startswith("m-")
    r = s.post(f"{API}/ops/signals/000000000000000000000000/replay-validate",
               timeout=TIMEOUT)
    assert r.status_code == 404


# ─── admin step-up on ops release/fleet controls (iter-163) ──────────
def test_ops_release_controls_require_step_up():
    """Admin sessions WITHOUT a fresh step-up token are refused on promote /
    rollback / agent config push; the CI bypass restores access."""
    s = _admin()
    s.headers["X-Step-Up-Bypass"] = ""  # disable conftest auto-bypass
    for path in ("/ops/releases/promote", "/ops/releases/rollback",
                 "/ops/agents/no-such-agent/config"):
        r = s.post(f"{API}{path}", json={"mt5_supervise": True},
                   timeout=TIMEOUT)
        assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"
        code = (r.json().get("detail") or {}).get("code")
        assert code in ("step_up_required", "mfa_enrollment_required"), r.text
    # bypass (CI) path still reaches the endpoint logic
    s2 = _admin()
    r = s2.post(f"{API}/ops/agents/no-such-agent/config",
                json={"mt5_supervise": True}, timeout=TIMEOUT)
    assert r.status_code == 404, r.text  # past the gate → agent lookup
    # unauthenticated remains blocked
    r = requests.post(f"{API}/ops/releases/promote",
                      headers={"X-Step-Up-Bypass": ""}, timeout=TIMEOUT)
    assert r.status_code in (401, 403)


def test_step_up_issue_accepts_new_ops_actions():
    from step_up import STEP_UP_ACTIONS
    for a in ("release_promote", "release_rollback", "agent_config_push"):
        assert a in STEP_UP_ACTIONS


# ─── trace propagation ───────────────────────────────────────────────
def test_trace_id_header_roundtrip():
    r = requests.get(f"{API}/health",
                     headers={"X-Trace-ID": "iter161-trace-abc"},
                     timeout=TIMEOUT)
    assert r.headers.get("X-Trace-ID") == "iter161-trace-abc"
    r = requests.get(f"{API}/health", timeout=TIMEOUT)
    assert r.headers.get("X-Trace-ID") == r.headers.get("X-Request-ID")


def test_trace_id_header_sanitized():
    """SEC-003 — control chars / non [A-Za-z0-9_-] stripped from
    attacker-controlled ids before logging/echoing (log forging)."""
    r = requests.get(f"{API}/health",
                     headers={"X-Trace-ID": "evil\tid rn{injected}!",
                              "X-Request-ID": "bad id\t{x}"},
                     timeout=TIMEOUT)
    assert r.headers.get("X-Trace-ID") == "evilidrninjected"
    assert r.headers.get("X-Request-ID") == "badidx"


def test_command_queue_stamps_trace_id():
    from correlation import new_correlation_id
    from vps_pathb import queue_command
    db = _db()
    uid = f"iter161-trace-{os.urandom(3).hex()}"
    agent_id = f"agent-{uid}"

    async def scenario():
        await db.vps_agents.insert_one(
            {"agent_id": agent_id, "user_id": uid,
             "agent_token": os.urandom(8).hex(), "command_seq": 0,
             "last_acked_seq": 0})
        try:
            new_correlation_id(prefix="iter161-")
            cmd = await queue_command(db, uid, agent_id, "run_diagnostics",
                                      None, "iter161-test")
            return await db.agent_commands.find_one(
                {"command_id": cmd["command_id"]})
        finally:
            await db.vps_agents.delete_many({"user_id": uid})
            await db.agent_commands.delete_many({"user_id": uid})
    doc = _run(scenario())
    assert str(doc.get("trace_id", "")).startswith("iter161-")


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
