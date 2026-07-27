"""iter-167 — 4th-audit fix: automatic rollback trusts ONLY operator-designated
release-trusted agents/tenants. Untrusted tenant telemetry (health score OR
corroboration) can never move fleet release state."""
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
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
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _open_watch(db, marker, sha, now):
    return db.platform_state.replace_one(
        {"_id": "release_state"},
        {"_id": "release_state",
         "stable": {marker: "bad-sha"}, "previous_stable": {marker: sha},
         "deploy_watch": {"started_at": now.isoformat(),
                          "baseline_score": 100.0,
                          "artifacts": {marker: sha}, "bake_hours": 4}},
        upsert=True)


async def _cleanup(db, marker, now, sha, store_dir):
    await db.platform_state.delete_one({"_id": "release_state"})
    await db.ops_alerts.delete_many({"dedup_key": {"$regex": marker}})
    await db.ops_alerts.delete_many(
        {"kind": {"$in": ["deployment_auto_rollback",
                          "deployment_review_needed"]},
         "created_at": {"$gte": now}})
    await db.release_history.delete_many({"at": {"$gte": now.isoformat()}})
    await db.vps_agents.delete_many({"agent_id": {"$regex": marker}})
    (store_dir / sha).unlink(missing_ok=True)


def test_untrusted_tenant_corroboration_does_not_rollback():
    """Two UNTRUSTED tenants reporting failure for the live digest must NOT
    trigger an auto-rollback (4th-audit SEC-002)."""
    from deployment_health import watch_deployment
    from release_channels import STORE_DIR
    db = _db()
    now = datetime.now(timezone.utc)
    marker = f"iter167a-{os.urandom(3).hex()}"
    blob = marker.encode()
    sha = hashlib.sha256(blob).hexdigest()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / sha).write_bytes(blob)

    async def scenario():
        orig = await db.platform_state.find_one({"_id": "release_state"})
        try:
            await _open_watch(db, marker, sha, now)
            # NO release_trusted agents exist for these tenants
            await db.ops_alerts.insert_many([
                {"kind": "deployment_failed", "severity": "critical",
                 "dedup_key": f"{marker}-{t}",
                 "meta": {"user_id": f"{marker}-tenant{t}", "sha256": sha},
                 "acked_at": None, "created_at": now} for t in range(3)])
            res = await watch_deployment(db)
            st = await db.platform_state.find_one({"_id": "release_state"})
            return res, st
        finally:
            if orig:
                await db.platform_state.replace_one(
                    {"_id": "release_state"}, orig, upsert=True)
            await _cleanup(db, marker, now, sha, STORE_DIR)
    res, st = _run(scenario())
    assert res["status"] != "auto_rollback", res
    assert st and "deploy_watch" in st, "watch cleared — a rollback happened"


def test_untrusted_health_degradation_warns_not_rollback():
    """Untrusted agents with bad health must NOT auto-rollback; a
    deployment_review_needed warning is raised for manual review instead
    (4th-audit SEC-001)."""
    from deployment_health import watch_deployment
    from release_channels import STORE_DIR
    db = _db()
    now = datetime.now(timezone.utc)
    marker = f"iter167b-{os.urandom(3).hex()}"
    blob = marker.encode()
    sha = hashlib.sha256(blob).hexdigest()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / sha).write_bytes(blob)

    async def scenario():
        orig = await db.platform_state.find_one({"_id": "release_state"})
        try:
            await _open_watch(db, marker, sha, now)
            # 5 UNTRUSTED agents, stale + mt5 disconnected → bad full-fleet score
            await db.vps_agents.insert_many([
                {"agent_id": f"{marker}-a{i}", "user_id": f"{marker}-tenant",
                 "revoked": False,
                 "last_heartbeat": now - timedelta(hours=3),
                 "last_metrics": {"mt5_connected": False}}
                for i in range(5)])
            res = await watch_deployment(db)
            st = await db.platform_state.find_one({"_id": "release_state"})
            review = await db.ops_alerts.count_documents(
                {"kind": "deployment_review_needed",
                 "dedup_key": f"deploy_review_{now.isoformat()}"})
            return res, st, review
        finally:
            if orig:
                await db.platform_state.replace_one(
                    {"_id": "release_state"}, orig, upsert=True)
            await _cleanup(db, marker, now, sha, STORE_DIR)
    res, st, review = _run(scenario())
    assert res["status"] != "auto_rollback", res
    assert st and "deploy_watch" in st, "watch cleared — a rollback happened"
    assert review == 1, "expected a manual-review warning alert"


def test_trusted_tenant_corroboration_does_rollback():
    """Positive control: TRUSTED tenants reporting failure DO roll back."""
    from deployment_health import watch_deployment
    from release_channels import STORE_DIR
    db = _db()
    now = datetime.now(timezone.utc)
    marker = f"iter167c-{os.urandom(3).hex()}"
    blob = marker.encode()
    sha = hashlib.sha256(blob).hexdigest()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / sha).write_bytes(blob)

    async def scenario():
        orig = await db.platform_state.find_one({"_id": "release_state"})
        try:
            await _open_watch(db, marker, sha, now)
            await db.vps_agents.insert_many([
                {"agent_id": f"{marker}-t{t}", "user_id": f"{marker}-tenant{t}",
                 "release_trusted": True, "revoked": False}
                for t in range(2)])
            await db.ops_alerts.insert_many([
                {"kind": "deployment_failed", "severity": "critical",
                 "dedup_key": f"{marker}-{t}",
                 "meta": {"user_id": f"{marker}-tenant{t}", "sha256": sha},
                 "acked_at": None, "created_at": now} for t in range(2)])
            res = await watch_deployment(db)
            st = await db.platform_state.find_one({"_id": "release_state"})
            return res, st
        finally:
            if orig:
                await db.platform_state.replace_one(
                    {"_id": "release_state"}, orig, upsert=True)
            await _cleanup(db, marker, now, sha, STORE_DIR)
    res, st = _run(scenario())
    assert res["status"] == "auto_rollback", res
    assert st["stable"] == {marker: sha}, "previous stable not restored"
    assert st["pinned"] is True


def test_release_trust_endpoint():
    s = _admin()
    r = s.post(f"{API}/ops/agents/no-such-agent/release-trust",
               json={"trusted": True}, timeout=TIMEOUT)
    assert r.status_code == 404, r.text
    r = requests.post(f"{API}/ops/agents/x/release-trust",
                      json={"trusted": True},
                      headers={"X-Step-Up-Bypass": ""}, timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    # status endpoint exposes release-trust config
    r = s.get(f"{API}/ops/deployment-health", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    assert "release_trust" in body and "trusted_health" in body
    assert "auto_rollback_enabled" in body["release_trust"]
