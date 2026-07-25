"""iter-113 — Path B HTTP contract tests.

Covers the review-request flows:
  - connect-existing → enrollment code + 3-step recommended install
  - bootstrap installer via enrollment_code (valid + bogus)
  - agent/register with enrollment_code (single-use)
  - pathb-status ladder: WAITING → CONNECTED → INSPECTING → READY
  - discovery ingest + list + decisions (manage-consent, clone,
    single-writer 409, unmanage)
  - command queue: queue → poll (deliver-once) → ack; compensation on
    ack ok=false for install_mt5; unknown command → 400
  - health policies via /agent/heartbeat + /agents/{id}/health
  - artifact manifest (public), broker-profiles (auth),
    broker-installers admin approval, failure-matrix
Cleans up all created DB rows so the admin UI stays tidy.
"""
import os
import sys
import uuid

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ─── session fixture ───────────────────────────────────────────
@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "missing csrf_token cookie"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


CTX = {"label": f"TEST_iter113_{uuid.uuid4().hex[:8]}"}


# ─── connect-existing + enrollment ─────────────────────────────
def test_connect_existing(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/vps/connect-existing",
        json={"provider_name": "ForexVPS", "label": CTX["label"],
              "region": "London", "mt5_installed": True},
        timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "WAITING_FOR_AGENT"
    code = j["enrollment_code"]
    assert len(code) == 7 and code[3] == "-", code
    assert code[:3].isalpha() and code[4:].isdigit()
    rec = j["install_commands"]["recommended"]
    assert isinstance(rec, list) and len(rec) == 3
    assert "-EnrollmentCode" in rec[-1]
    CTX["deployment_id"] = j["deployment_id"]
    CTX["code"] = code


# ─── bootstrap installer (script) ──────────────────────────────
def test_bootstrap_installer_bogus_code_401():
    r = requests.get(
        f"{BASE_URL}/api/infra/agent/bootstrap/installer",
        params={"enrollment_code": "ZZZ-000"}, timeout=15)
    assert r.status_code == 401, r.status_code


def test_bootstrap_installer_valid_code():
    r = requests.get(
        f"{BASE_URL}/api/infra/agent/bootstrap/installer",
        params={"enrollment_code": CTX["code"]}, timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.text
    # PowerShell shape: register + heartbeat/command poll loop + discovery
    assert "Invoke-RestMethod" in body or "Invoke-WebRequest" in body \
        or "iwr" in body.lower() or "$token" in body.lower()
    lower = body.lower()
    assert "discover" in lower or "mt5" in lower
    assert "heartbeat" in lower or "commands" in lower


# ─── ladder: WAITING → CONNECTED (register) ────────────────────
def test_pathb_status_waiting(sess):
    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/pathb-status", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "WAITING_FOR_AGENT"
    assert j.get("diagnostics")


def test_register_agent_via_enrollment_code():
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/register",
        json={"enrollment_code": CTX["code"],
              "machine_fingerprint": "fp-http-b",
              "agent_version": "1.0.0"},
        timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["agent_id"] and j["agent_token"]
    CTX["agent_id"] = j["agent_id"]
    CTX["agent_token"] = j["agent_token"]


def test_enrollment_code_single_use():
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/register",
        json={"enrollment_code": CTX["code"],
              "machine_fingerprint": "fp-http-b2",
              "agent_version": "1.0.0"},
        timeout=15)
    assert r.status_code == 401, r.status_code


def test_pathb_status_agent_connected(sess):
    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/pathb-status", timeout=15)
    assert r.status_code == 200
    assert r.json()["status"] == "AGENT_CONNECTED"


def test_pathb_status_inspecting_after_heartbeat(sess):
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/heartbeat",
        json={"agent_token": CTX["agent_token"],
              "metrics": {"cpu_percent": 4}}, timeout=15)
    assert r.status_code == 200, r.text
    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/pathb-status", timeout=15)
    assert r.json()["status"] == "INSPECTING_SERVER"


# ─── discovery ─────────────────────────────────────────────────
def test_discovery_ingest_and_ready(sess):
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/discovery",
        json={"agent_token": CTX["agent_token"], "terminals": [
            {"path": "C:\\Program Files\\IC Markets MT5",
             "broker_hint": "IC Markets", "account_login": "990113",
             "running": True, "ea_installed": False,
             "source": "filesystem"},
            {"path": "C:\\Program Files\\Pepperstone MT5",
             "broker_hint": "Pepperstone", "running": False,
             "source": "registry"}]},
        timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["stored"] == 2

    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/pathb-status", timeout=15)
    assert r.json()["status"] == "READY_FOR_SETUP"

    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/discovery", timeout=15)
    assert r.status_code == 200
    terms = r.json()["terminals"]
    assert len(terms) == 2
    ic = next(t for t in terms if t["broker_hint"] == "IC Markets")
    pep = next(t for t in terms if t["broker_hint"] == "Pepperstone")
    CTX["disc_ic"] = ic["discovery_id"]
    CTX["disc_pep"] = pep["discovery_id"]


def test_manage_without_consent_403(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/discovery/{CTX['disc_ic']}/decision",
        json={"action": "manage", "consent": False}, timeout=15)
    assert r.status_code == 403, r.text


def test_clone_creates_isolated_instance(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/discovery/{CTX['disc_ic']}/decision",
        json={"action": "clone"}, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["decision"] == "clone"
    assert j["directory"] == "C:\\STOIC\\MT5\\account-990113\\"


def test_single_writer_guard_409(sess):
    # ingest a second discovery for same account_login
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/discovery",
        json={"agent_token": CTX["agent_token"], "terminals": [
            {"path": "D:\\Another\\MT5", "broker_hint": "IC Markets",
             "account_login": "990113", "running": False}]},
        timeout=15)
    assert r.status_code == 200
    r = sess.get(
        f"{BASE_URL}/api/infra/deployments/"
        f"{CTX['deployment_id']}/discovery", timeout=15)
    dup = next(t for t in r.json()["terminals"]
               if t["path"] == "D:\\Another\\MT5")
    r = sess.post(
        f"{BASE_URL}/api/infra/discovery/{dup['discovery_id']}/decision",
        json={"action": "manage", "consent": True}, timeout=15)
    assert r.status_code == 409, r.text


def test_unmanage_decision(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/discovery/{CTX['disc_pep']}/decision",
        json={"action": "unmanage"}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "unmanage"


# ─── command queue + compensation ──────────────────────────────
def test_queue_poll_ack_ok(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/agents/{CTX['agent_id']}/commands",
        json={"command": "restart_terminal", "params": {"acc": "990113"}},
        timeout=15)
    assert r.status_code == 200, r.text
    cmd_id = r.json()["command_id"]

    r = requests.post(
        f"{BASE_URL}/api/infra/agent/commands/poll",
        json={"agent_token": CTX["agent_token"]}, timeout=15)
    assert r.status_code == 200
    ids = [c["command_id"] for c in r.json()["commands"]]
    assert cmd_id in ids

    # second poll should NOT redeliver
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/commands/poll",
        json={"agent_token": CTX["agent_token"]}, timeout=15)
    assert cmd_id not in [c["command_id"] for c in r.json()["commands"]]

    r = requests.post(
        f"{BASE_URL}/api/infra/agent/commands/ack",
        json={"agent_token": CTX["agent_token"],
              "command_id": cmd_id, "ok": True, "detail": "restarted"},
        timeout=15)
    assert r.status_code == 200
    assert r.json().get("compensation") is None


def test_failed_install_triggers_compensation(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/agents/{CTX['agent_id']}/commands",
        json={"command": "install_mt5", "params": {}}, timeout=15)
    assert r.status_code == 200
    cmd_id = r.json()["command_id"]
    requests.post(f"{BASE_URL}/api/infra/agent/commands/poll",
                  json={"agent_token": CTX["agent_token"]}, timeout=15)
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/commands/ack",
        json={"agent_token": CTX["agent_token"],
              "command_id": cmd_id, "ok": False, "detail": "hash mismatch"},
        timeout=15)
    assert r.status_code == 200, r.text
    comp = r.json().get("compensation")
    assert comp and comp["command"] == "rollback_mt5"


def test_unknown_command_400(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/agents/{CTX['agent_id']}/commands",
        json={"command": "format_c", "params": {}}, timeout=15)
    assert r.status_code == 400, r.text


# ─── health policies ───────────────────────────────────────────
def test_disk_low_rotates_logs(sess):
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/heartbeat",
        json={"agent_token": CTX["agent_token"],
              "metrics": {"disk_free_gb": 2}}, timeout=15)
    assert r.status_code == 200
    assert "rotate_logs_queued" in r.json().get("policy_actions", [])


def test_time_drift_disables_order_entry(sess):
    r = requests.post(
        f"{BASE_URL}/api/infra/agent/heartbeat",
        json={"agent_token": CTX["agent_token"],
              "metrics": {"clock_offset_ms": 5000}}, timeout=15)
    assert r.status_code == 200
    assert "order_entry_disabled" in r.json().get("policy_actions", [])
    r = sess.get(
        f"{BASE_URL}/api/infra/agents/{CTX['agent_id']}/health",
        timeout=15)
    assert r.status_code == 200
    flags = r.json().get("policy_flags") or {}
    assert flags.get("order_entry_disabled") is True


# ─── artifacts + broker profiles + failure matrix ─────────────
def test_artifact_manifest_public():
    r = requests.get(f"{BASE_URL}/api/infra/artifacts/manifest", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    ea = next(a for a in j["artifacts"] if a["name"] == "stoic-ea")
    assert ea["version"] == "1.54"
    assert len(ea["sha256"]) == 64
    assert ea["rollback_version"] == "1.53"


def test_broker_profiles_seeded(sess):
    r = sess.get(f"{BASE_URL}/api/infra/broker-profiles", timeout=15)
    assert r.status_code == 200
    profs = r.json()["profiles"]
    assert len(profs) >= 3
    for k in ("broker", "server_names", "silent_arguments"):
        assert k in profs[0]


def test_failure_matrix_ten_rows(sess):
    r = sess.get(f"{BASE_URL}/api/infra/failure-matrix", timeout=15)
    assert r.status_code == 200
    rows = r.json()["matrix"]
    assert len(rows) == 10
    assert all(r_.get("response") for r_ in rows)


def test_custom_installer_approval_flow(sess):
    r = sess.post(
        f"{BASE_URL}/api/infra/broker-installers",
        json={"broker": "TEST_ObscureBrokerIter113",
              "sha256": "b" * 64,
              "installer_url": "https://broker.example/mt5.exe"},
        timeout=15)
    assert r.status_code == 200, r.text
    inst = r.json()
    assert inst["approved"] is False
    CTX["installer_id"] = inst["installer_id"]

    r = sess.post(
        f"{BASE_URL}/api/infra/broker-installers/"
        f"{inst['installer_id']}/approve", timeout=15)
    # admin session — should succeed
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True


# ─── unauth check ──────────────────────────────────────────────
def test_broker_profiles_unauth_blocked():
    r = requests.get(f"{BASE_URL}/api/infra/broker-profiles", timeout=15)
    assert r.status_code in (401, 403), r.status_code


# ─── cleanup ───────────────────────────────────────────────────
def test_zz_cleanup():
    """Purge everything we created so the admin UI stays clean."""
    import asyncio
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        dep = CTX.get("deployment_id")
        agent_id = CTX.get("agent_id")
        if dep:
            await db.vps_deployments.delete_many({"deployment_id": dep})
            await db.vps_bootstrap_tokens.delete_many(
                {"deployment_id": dep})
            await db.mt5_discovered.delete_many({"deployment_id": dep})
            await db.mt5_instances.delete_many({"deployment_id": dep})
        if agent_id:
            await db.vps_agents.delete_many({"agent_id": agent_id})
            await db.agent_commands.delete_many({"agent_id": agent_id})
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$regex": agent_id}})
        if CTX.get("installer_id"):
            await db.broker_installers.delete_many(
                {"installer_id": CTX["installer_id"]})
        # Belt-and-braces: purge by label prefix too
        await db.vps_deployments.delete_many(
            {"label": {"$regex": "^TEST_iter113_"}})
        await db.broker_installers.delete_many(
            {"broker": {"$regex": "^TEST_ObscureBrokerIter113"}})
        client.close()

    asyncio.get_event_loop().run_until_complete(go())
