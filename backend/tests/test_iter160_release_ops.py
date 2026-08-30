"""iter-160 — canary rollout, config sync, trade lifecycle audit, fleet
health, benchmark harness."""
import os
import sys
from datetime import datetime, timezone

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


# ─── canary / staged rollout ─────────────────────────────────────────
def test_manifest_is_cohort_aware_and_signed():
    import json
    import release_signing
    r = requests.get(f"{API}/infra/artifacts/manifest?agent_id=not-a-canary",
                     timeout=TIMEOUT)
    assert r.status_code == 200
    m = r.json()
    assert m["channel"] == "stable"
    body = json.dumps({k: m[k] for k in ("artifacts", "update_policy")},
                      sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    assert release_signing.verify_hex(body, m["signature"]["value"])
    for a in m["artifacts"]:
        if a.get("sha256"):
            assert a["url"] == f"/api/artifacts/{a['sha256']}"


def test_release_store_snapshot_and_artifact_route():
    from release_channels import sync_channels, STORE_DIR
    st = _run(sync_channels(_db()))
    stable = st.get("stable") or {}
    assert stable, "stable channel empty after sync"
    for name, sha in stable.items():
        assert (STORE_DIR / sha).exists(), f"{name} missing from store"
        r = requests.get(f"{API}/artifacts/{sha}", timeout=TIMEOUT)
        assert r.status_code == 200, f"store artifact {name} not served"


def test_canary_channel_and_promote_rollback_cycle():
    from release_channels import (set_canary_agents, channel_for_agent,
                                  promote, rollback, set_pinned, STATE_ID)
    db = _db()

    async def go():
        st0 = await db.platform_state.find_one({"_id": STATE_ID})
        try:
            await set_canary_agents(db, ["canary-1"], actor="pytest")
            # synthetic candidate differing from stable
            fake_cand = {"stoic-ea": "f" * 64}
            await db.platform_state.update_one(
                {"_id": STATE_ID},
                {"$set": {"candidate": fake_cand,
                          "candidate_since":
                              datetime.now(timezone.utc).isoformat(),
                          "pinned": False}})
            ch, shas = await channel_for_agent(db, "canary-1")
            # sync_channels may clear an unknown candidate if it matches
            # neither disk nor stable — re-set then read raw state instead
            st = await db.platform_state.find_one({"_id": STATE_ID})
            assert st["canary_agents"] == ["canary-1"]
            # promote the synthetic candidate
            await db.platform_state.update_one(
                {"_id": STATE_ID}, {"$set": {"candidate": fake_cand}})
            prev_stable = st.get("stable")
            promoted = await promote(db, actor="pytest")
            assert promoted["stable"] == fake_cand
            assert promoted["previous_stable"] == prev_stable
            # rollback restores previous stable and PINS
            # (fake sha not in store → must fail safe)
            rolled = None
            try:
                rolled = await rollback(db, actor="pytest")
            except ValueError:
                rolled = None
            if rolled is None:
                # previous stable artifacts exist in store → direct rollback
                # of the REAL stable set must succeed
                await db.platform_state.update_one(
                    {"_id": STATE_ID},
                    {"$set": {"previous_stable": prev_stable}})
                rolled = await rollback(db, actor="pytest")
            assert rolled["stable"] == prev_stable
            assert rolled["pinned"] is True
            await set_pinned(db, False, actor="pytest")
        finally:
            if st0:
                st0.pop("_id", None)
                await db.platform_state.replace_one({"_id": STATE_ID}, st0,
                                                    upsert=True)
            await db.release_history.delete_many(
                {"detail.by": "pytest"})
    _run(go())


def test_release_endpoints_admin_only():
    for method, path in (("get", "/ops/releases"),
                         ("post", "/ops/releases/promote"),
                         ("post", "/ops/releases/rollback")):
        r = getattr(requests, method)(f"{API}{path}", timeout=TIMEOUT)
        assert r.status_code in (401, 403), path
    s = _admin()
    r = s.get(f"{API}/ops/releases", timeout=TIMEOUT)
    assert r.status_code == 200
    assert "stable" in r.json()["state"]


# ─── config sync ─────────────────────────────────────────────────────
def test_agent_config_sync_via_heartbeat():
    from vps_agent import agent_heartbeat
    db = _db()
    token = f"agt_tok_iter160-{os.urandom(6).hex()}"
    aid = f"iter160-agent-{os.urandom(4).hex()}"

    async def go():
        await db.vps_agents.insert_one(
            {"agent_id": aid, "agent_token": token, "revoked": False})
        out = await agent_heartbeat(db, token, {"cpu_percent": 5})
        assert "desired_config" not in out  # none set yet
        return out
    _run(go())
    s = _admin()
    r = s.post(f"{API}/ops/agents/{aid}/config",
               json={"telemetry_interval_sec": 30, "mt5_supervise": True,
                     "evil_key": "x"}, timeout=TIMEOUT)
    assert r.status_code == 200
    assert "evil_key" not in r.json()["desired_config"]

    async def hb2():
        out = await agent_heartbeat(db, token, {"cpu_percent": 5})
        await db.vps_agents.delete_one({"agent_id": aid})
        return out
    out = _run(hb2())
    assert out["desired_config"]["telemetry_interval_sec"] == 30
    # unknown agent 404
    r = s.post(f"{API}/ops/agents/no-such-agent/config",
               json={"mt5_supervise": False}, timeout=TIMEOUT)
    assert r.status_code == 404


# ─── trade lifecycle audit ───────────────────────────────────────────
def test_trade_timeline_assembly_and_authz():
    db = _db()

    async def latest_trade():
        return await db.trades.find_one({}, sort=[("_id", -1)])
    trade = _run(latest_trade())
    assert trade is not None, "no trades in db to audit"
    tid = str(trade["_id"])
    s = _admin()
    r = s.get(f"{API}/trades/{tid}/timeline", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    tl = r.json()
    names = [x["stage"] for x in tl["stages"]]
    for expected in ("signal", "validation", "risk", "execution",
                     "confirmation", "monitoring", "close"):
        assert expected in names, f"missing stage {expected}"
    # non-owner non-admin blocked
    from helpers import register_and_login
    s2 = register_and_login(f"iter160-{os.urandom(4).hex()}@example.com")
    if tl["user_id"]:
        r = s2.get(f"{API}/trades/{tid}/timeline", timeout=TIMEOUT)
        assert r.status_code == 403
    r = s.get(f"{API}/trades/definitely-not-a-trade/timeline",
              timeout=TIMEOUT)
    assert r.status_code == 404


# ─── fleet health ────────────────────────────────────────────────────
def test_ops_console_exposes_fleet():
    s = _admin()
    r = s.get(f"{API}/admin/ops-console", timeout=TIMEOUT)
    assert r.status_code == 200
    ha = r.json()["host_agents"]
    assert "fleet" in ha and isinstance(ha["fleet"], list)
    if ha["fleet"]:
        row = ha["fleet"][0]
        for k in ("agent_id", "heartbeat_age_sec", "cpu_percent",
                  "mt5_connected", "pending_reboot"):
            assert k in row


# ─── benchmark harness ───────────────────────────────────────────────
def test_benchmark_script_smoke():
    import subprocess
    root = os.path.dirname(_BACKEND_DIR)
    p = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "benchmark.py"),
         "--base", "http://localhost:8001", "--users", "4", "--seconds", "5"],
        capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert '"rps"' in p.stdout and "/api/health" in p.stdout


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
