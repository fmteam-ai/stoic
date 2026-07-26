"""iter-157 — Runtime validation harness + host-agent v2.1 backend support."""
import os
import sys

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
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


# ─── the harness itself: every scenario must PASS on a healthy system ──
def test_all_runtime_scenarios_pass():
    from runtime_validation import run_runtime_validation, SCENARIOS
    db = _db()
    run = _run(run_runtime_validation(db, actor="pytest"))
    failed = [r for r in run["results"] if r["status"] != "pass"]
    assert not failed, f"runtime validation failures: {failed}"
    assert run["passed"] == len(SCENARIOS)
    assert run["run_id"].startswith("rtv-")


def test_run_recorded_in_validation_runs():
    db = _db()

    async def go():
        return await db.validation_runs.find_one(
            {"mode": "runtime"}, sort=[("at", -1)])
    doc = _run(go())
    assert doc and doc["results"] and doc["started_by"]


def test_runtime_endpoints_admin_only_and_functional():
    # unauthenticated → 401/403
    r = requests.post(f"{API}/ops/validation/runtime/run", json={},
                      timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    r = requests.get(f"{API}/ops/validation/runtime/runs", timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    # admin: run a single fast scenario over HTTP
    s = _admin()
    r = s.post(f"{API}/ops/validation/runtime/run",
               json={"scenarios": ["heartbeat_timeout", "expired_token"]},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed"] == 0 and body["passed"] == 2
    r = s.get(f"{API}/ops/validation/runtime/runs", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["runs"][0]["run_id"] == body["run_id"]
    assert "lease_expiration" in r.json()["scenarios"]


def test_unknown_scenarios_ignored():
    from runtime_validation import run_runtime_validation
    run = _run(run_runtime_validation(_db(), scenarios=["nope", "expired_token"]))
    assert [r["scenario"] for r in run["results"]] == ["expired_token"]


# ─── host agent v2.1: token renewal ──────────────────────────────────
def test_agent_token_renewal_rotates_and_kills_old():
    from vps_agent import rotate_agent_token, agent_by_token
    db = _db()
    old_token = f"agt_tok_test-{os.urandom(8).hex()}"

    async def go():
        await db.vps_agents.insert_one(
            {"agent_id": f"rv-agent-{os.urandom(4).hex()}",
             "agent_token": old_token, "revoked": False})
        out = await rotate_agent_token(db, old_token)
        assert out["agent_token"].startswith("agt_tok_")
        assert out["agent_token"] != old_token
        import pytest as _pt
        with _pt.raises(ValueError):
            await agent_by_token(db, old_token)  # old token dead
        fresh = await agent_by_token(db, out["agent_token"])
        assert fresh and fresh["agent_id"] == out["agent_id"]
        await db.vps_agents.delete_one({"agent_id": out["agent_id"]})
    _run(go())


def test_renew_token_endpoint_rejects_bad_token():
    r = requests.post(f"{API}/infra/agent/renew-token",
                      json={"agent_token": "agt_tok_bogus"}, timeout=TIMEOUT)
    assert r.status_code == 401


# ─── host agent spec structural checks ───────────────────────────────
def test_host_agent_script_v21_capabilities():
    path = os.path.join(_BACKEND_DIR, "..", "docs", "host-agent",
                        "stoic-host-agent.ps1")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for marker in ("Ensure-Mt5", "Invoke-BackendCommands", "Renew-TokenIfDue",
                   "release_public_key_b64", "restart_mt5",
                   "collect_diagnostics", "$target.bak",  # rollback
                   "renew-token", "pending_reboot", "2.1.0"):
        assert marker in src, f"host agent spec missing {marker}"
    # never install from a mutable URL — updates flow through the manifest
    assert "artifacts/manifest" in src


def test_signed_msi_doc_present():
    path = os.path.join(_BACKEND_DIR, "..", "docs", "host-agent",
                        "SIGNED_MSI.md")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for marker in ("signtool", "wix", "SHA256", "Authenticode",
                   "/api/artifacts/{sha256}"):
        assert marker in src, f"MSI doc missing {marker}"
