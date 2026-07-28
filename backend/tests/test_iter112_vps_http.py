"""iter-112 — HTTP contract tests for VPS infrastructure endpoints.
Auth via httpOnly cookies + X-CSRF-Token. Agent/pairing-claim endpoints
are token-based (no session/CSRF).
"""
import os
import uuid
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                          "https://stoic-trading-bot.preview.emergentagent.com"
                          ).rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "missing csrf_token cookie"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def created_ids():
    return {"deployments": [], "pairing_codes": []}


# ── providers ───────────────────────────────────────────────────
def test_providers_list(sess):
    r = sess.get(f"{BASE_URL}/api/infra/providers", timeout=15)
    assert r.status_code == 200
    provs = {p["name"]: p for p in r.json()["providers"]}
    assert set(provs) == {"forexvps", "cns", "beeks", "vultr", "simulated"}
    # partner-gated → available False
    for name in ("forexvps", "cns", "beeks"):
        assert provs[name].get("method") == "partner"
        assert provs[name].get("available") is False, provs[name]
    # available providers
    assert provs["vultr"].get("available") is True
    assert provs["simulated"].get("available") is True


def test_providers_connect_beeks_gated(sess):
    r = sess.post(f"{BASE_URL}/api/infra/providers/beeks/connect",
                  json={"api_key": "x"}, timeout=15)
    assert r.status_code == 409, r.text


def test_providers_connect_vultr_masked(sess):
    api_key = "vlt_test_" + uuid.uuid4().hex[:16]
    r = sess.post(f"{BASE_URL}/api/infra/providers/vultr/connect",
                  json={"api_key": api_key}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "vultr"
    assert body["masked"] and api_key not in body["masked"]


# ── brokers / recommend ─────────────────────────────────────────
def test_brokers_catalog(sess):
    r = sess.get(f"{BASE_URL}/api/infra/brokers/catalog", timeout=15)
    assert r.status_code == 200
    names = [b["broker"] for b in r.json()["brokers"]]
    assert "IC Markets" in names and "RoboForex" in names


def test_recommend_ic_markets(sess):
    r = sess.post(f"{BASE_URL}/api/infra/recommend",
                  json={"broker": "IC Markets", "mt5_instances": 3},
                  timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    lat = body["latency"]
    assert lat["recommended_region"] == "london"
    # region_estimates sorted asc
    ests = [e["estimate_ms"] for e in lat["region_estimates"]]
    assert ests == sorted(ests)
    assert "disclaimer" in lat and lat.get("at")
    cap = body["capacity"]
    assert cap["plan"] == "4vcpu-8gb"


def test_recommend_research_workload_note(sess):
    r = sess.post(f"{BASE_URL}/api/infra/recommend",
                  json={"broker": "IC Markets", "mt5_instances": 2,
                        "research_workload": True}, timeout=30)
    assert r.status_code == 200
    notes = " ".join(r.json()["capacity"]["notes"]).lower()
    assert "separate" in notes and "worker" in notes


# ── deployments ─────────────────────────────────────────────────
def test_deployment_idempotency_and_shadow(sess, created_ids):
    key = f"idem-http-{uuid.uuid4().hex[:8]}"
    payload = {"provider": "simulated", "region": "london",
               "plan": "2vcpu-4gb", "mt5_instances": 1,
               "issue_bootstrap": True}
    h = {"Idempotency-Key": key}
    r1 = sess.post(f"{BASE_URL}/api/infra/deployments",
                   json=payload, headers=h, timeout=15)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["mode"] == "shadow"
    assert d1["state"] == "REQUESTED"
    assert d1.get("bootstrap", {}).get("token", "").startswith("bst_")
    created_ids["deployments"].append(d1["deployment_id"])

    r2 = sess.post(f"{BASE_URL}/api/infra/deployments",
                   json=payload, headers=h, timeout=15)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["deployment_id"] == d1["deployment_id"]


def test_deployment_advances_state(sess, created_ids):
    payload = {"provider": "simulated", "region": "london",
               "plan": "2vcpu-4gb", "mt5_instances": 1}
    r = sess.post(f"{BASE_URL}/api/infra/deployments",
                  json=payload, timeout=15)
    assert r.status_code == 200
    dep_id = r.json()["deployment_id"]
    created_ids["deployments"].append(dep_id)
    # advance takes ~28s
    final = None
    for _ in range(20):
        time.sleep(2)
        g = sess.get(f"{BASE_URL}/api/infra/deployments/{dep_id}",
                     timeout=15)
        assert g.status_code == 200
        final = g.json()
        if final["state"] == "READY":
            break
    assert final and final["state"] == "READY", final
    states = [s["state"] for s in final["state_history"]]
    assert states[0] == "REQUESTED" and states[-1] == "READY"


# ── agent lifecycle ─────────────────────────────────────────────
def test_agent_full_lifecycle_http(sess, created_ids):
    # existing_vps path → bootstrap issued automatically
    r = sess.post(f"{BASE_URL}/api/infra/deployments",
                  json={"provider": "existing", "path": "existing_vps",
                        "mt5_instances": 1}, timeout=15)
    assert r.status_code == 200, r.text
    dep = r.json()
    created_ids["deployments"].append(dep["deployment_id"])
    token = dep["bootstrap"]["token"]
    assert token.startswith("bst_")

    # installer served for valid token
    r = requests.get(f"{BASE_URL}/api/infra/agent/bootstrap/installer",
                     params={"token": token}, timeout=15)
    assert r.status_code == 200
    assert "STOIC Agent bootstrap" in r.text

    # invalid token → 401
    r = requests.get(f"{BASE_URL}/api/infra/agent/bootstrap/installer",
                     params={"token": "bst_bogus"}, timeout=15)
    assert r.status_code == 401

    # register agent (no session/csrf)
    fp = f"fp-http-{uuid.uuid4().hex[:8]}"
    payload = {"bootstrap_token": token, "machine_fingerprint": fp,
               "agent_version": "1.0.0",
               "windows_version": "Server 2022", "cpu": "test",
               "ram_gb": 8, "disk_free_gb": 60, "public_ip": "203.0.113.9",
               "timezone": "UTC", "clock_offset_ms": 12}
    r = requests.post(f"{BASE_URL}/api/infra/agent/register",
                      json=payload, timeout=15)
    assert r.status_code == 200, r.text
    reg = r.json()
    assert reg["agent_id"].startswith("agt_")
    at = reg["agent_token"]

    # re-registering with same bootstrap → 401 single-use
    r = requests.post(f"{BASE_URL}/api/infra/agent/register",
                      json=payload, timeout=15)
    assert r.status_code == 401, r.text

    # heartbeat
    r = requests.post(f"{BASE_URL}/api/infra/agent/heartbeat",
                      json={"agent_token": at,
                            "metrics": {"cpu_percent": 10,
                                        "ram_percent": 40,
                                        "disk_free_gb": 60,
                                        "clock_offset_ms": 12,
                                        "mt5_processes": 1,
                                        "agent_version": "1.0.0"}},
                      timeout=15)
    assert r.status_code == 200 and r.json().get("ok") is True

    # hardening — only firewall true → complete False + missing[]
    r = requests.post(f"{BASE_URL}/api/infra/agent/hardening",
                      json={"agent_token": at,
                            "checklist": {"firewall_enabled": True}},
                      timeout=15)
    assert r.status_code == 200
    hard = r.json()
    assert hard["complete"] is False
    assert "rdp_restricted" in hard["missing"]

    # register MT5 instance → directory pattern
    r = requests.post(f"{BASE_URL}/api/infra/mt5/instances",
                      json={"agent_token": at, "account_ref": "99001",
                            "broker": "TestBroker", "ea_version": "1.54"},
                      timeout=15)
    assert r.status_code == 200
    assert "account-99001" in r.json()["directory"]

    # deployment now READY
    g = sess.get(f"{BASE_URL}/api/infra/deployments/{dep['deployment_id']}",
                 timeout=15)
    assert g.status_code == 200
    assert g.json()["state"] == "READY"


def test_agent_endpoints_reject_bad_token():
    r = requests.post(f"{BASE_URL}/api/infra/agent/heartbeat",
                      json={"agent_token": "agt_tok_bogus", "metrics": {}},
                      timeout=15)
    assert r.status_code == 401


# ── pairing (hardened iter-114: synthetic account — claim ROTATES the
# bridge token, so never pair a real admin account in tests) ─────
def test_pairing_flow(sess, created_ids):
    import uuid as _uuid
    from pymongo import MongoClient
    import os as _os
    from dotenv import load_dotenv as _ld
    _ld(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), ".env"))
    mdb = MongoClient(_os.environ["MONGO_URL"])[_os.environ["DB_NAME"]]
    uid = "iter112http-pair"
    res = mdb.accounts.insert_one({
        "user_id": "admin-user-id-placeholder", "mode": "demo",
        "label": "http-pair-test",
        "bridge_token": f"tok-{_uuid.uuid4().hex}"})
    account_id = str(res.inserted_id)
    # bind the synthetic account to the logged-in admin user id
    me = sess.get(f"{BASE_URL}/api/auth/me", timeout=15).json()
    admin_id = me.get("id") or me.get("user", {}).get("id")
    mdb.accounts.update_one({"_id": res.inserted_id},
                            {"$set": {"user_id": admin_id}})
    try:
        r = sess.post(f"{BASE_URL}/api/infra/pairing",
                      json={"account_id": account_id}, timeout=15)
        assert r.status_code == 200, r.text
        code = r.json()["code"]
        assert code.startswith("PAIR-")
        # digest-only storage — plaintext code is not in the DB
        assert mdb.ea_pairing_codes.find_one({"code": code}) is None

        # claim without terminal binding → 400
        r = requests.post(f"{BASE_URL}/api/infra/pairing/claim",
                          json={"code": code}, timeout=15)
        assert r.status_code == 400

        # claim WITHOUT auth, bound to exactly one terminal
        r = requests.post(
            f"{BASE_URL}/api/infra/pairing/claim",
            json={"code": code,
                  "terminal": {"terminal_path": "C:\\STOIC\\MT5\\a\\",
                               "host_fingerprint": "host-http"}},
            timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("bridge_token", "").startswith("tok_")  # rotated
        assert body.get("account_id") == account_id
        assert body.get("connected") is False

        # re-claim → 401 already claimed
        r = requests.post(
            f"{BASE_URL}/api/infra/pairing/claim",
            json={"code": code,
                  "terminal": {"terminal_path": "x",
                               "host_fingerprint": "y"}}, timeout=15)
        assert r.status_code == 401
    finally:
        mdb.accounts.delete_one({"_id": res.inserted_id})
        mdb.ea_pairing_codes.delete_many({"account_id": account_id})
        mdb.ea_deployments.delete_many({"account_id": account_id})
        mdb.installations.delete_many({"account_id": account_id})
        mdb.execution_leases.delete_many({"account_id": account_id})


# ── overview + certification ────────────────────────────────────
def test_overview_shape(sess):
    r = sess.get(f"{BASE_URL}/api/infra/overview", timeout=15)
    assert r.status_code == 200
    body = r.json()
    for k in ("agents", "mt5_instances", "backups"):
        assert k in body and isinstance(body[k], list)


def test_certification_shape(sess):
    r = sess.get(f"{BASE_URL}/api/infra/certification", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["initial_mode"] == "shadow"
    assert body["total"] == 8
    assert set(body["checks"].keys()) == {
        "market_data_receiving", "signals_generating", "risk_decisions",
        "ea_heartbeat", "broker_reconciliation", "clock_synchronization",
        "no_duplicate_commands", "latency_within_threshold"}


# ── auth boundaries ─────────────────────────────────────────────
def test_unauth_endpoints_reject():
    # session-protected endpoints
    for path in ("/api/infra/providers", "/api/infra/overview",
                 "/api/infra/certification",
                 "/api/infra/brokers/catalog"):
        r = requests.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code in (401, 403), f"{path}: {r.status_code}"
    # deployment listing
    r = requests.get(f"{BASE_URL}/api/infra/deployments", timeout=15)
    assert r.status_code in (401, 403)


# ── cleanup ─────────────────────────────────────────────────────
def test_zz_cleanup(sess, created_ids):
    """Best-effort: delete deployments created by this run. Pairing codes
    are TTL-bound and used=true; agents attached are cleaned via provider
    delete when path was existing_vps we can only mark deployment deleted."""
    for dep_id in set(created_ids["deployments"]):
        # simulated deployments have a server → hit actions/delete;
        # existing_vps ones have no server so we skip and leave a note.
        r = sess.post(
            f"{BASE_URL}/api/infra/deployments/{dep_id}/actions",
            json={"action": "delete"}, timeout=15)
        # 200 OK or 404 (no server for existing_vps) both acceptable
        assert r.status_code in (200, 404), f"{dep_id}: {r.status_code}"
    # Direct DB cleanup of leftovers created by this test module
    import os as _os
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv(_os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), ".env"))
    cli = MongoClient(_os.environ["MONGO_URL"])
    db = cli[_os.environ["DB_NAME"]]
    if created_ids["deployments"]:
        # remove agents / mt5 / bootstrap tokens tied to these
        db.vps_agents.delete_many(
            {"deployment_id": {"$in": created_ids["deployments"]}})
        db.mt5_instances.delete_many(
            {"deployment_id": {"$in": created_ids["deployments"]}})
        db.vps_bootstrap_tokens.delete_many(
            {"deployment_id": {"$in": created_ids["deployments"]}})
        db.vps_deployments.delete_many(
            {"deployment_id": {"$in": created_ids["deployments"]}})
    if created_ids["pairing_codes"]:
        db.ea_pairing_codes.delete_many(
            {"code": {"$in": created_ids["pairing_codes"]}})
    # remove the vultr cred we upserted
    db.vps_provider_creds.delete_many(
        {"provider": "vultr", "api_key_masked": {"$exists": True}})
    cli.close()
