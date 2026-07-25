"""iter-114 HTTP contract: pairing digest-only, atomic claim + terminal
binding + token rotation, execution-owner lease, deployment ladder,
progress endpoint, install_ea guard, EX5 manifest, no irm|iex."""
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from pymongo import MongoClient  # noqa: E402
from bson import ObjectId  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") \
    if os.environ.get("REACT_APP_BACKEND_URL") \
    else open(os.path.join(_REPO, "frontend", ".env")).read().split(
        "REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip().strip('"')
API = f"{BASE}/api"
ADMIN = {"email": "admin@trading.bot", "password": "admin123"}
TAG = f"TEST_iter114_{uuid.uuid4().hex[:6]}"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json=ADMIN, timeout=15)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token") or r.json().get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin_id(sess):
    r = sess.get(f"{API}/auth/me", timeout=10)
    assert r.status_code == 200
    return r.json()["id"]


@pytest.fixture(scope="module")
def synth_account(mongo, admin_id):
    """Create a SYNTHETIC account bound to admin — NEVER touch real."""
    res = mongo.accounts.insert_one({
        "user_id": admin_id, "mode": "demo",
        "label": f"{TAG}_synth",
        "server": "TEST-Server",
        "broker_account_id_reported": "9990000",
        "bridge_token": f"tok_original_{uuid.uuid4().hex}"})
    aid = str(res.inserted_id)
    yield aid
    mongo.accounts.delete_one({"_id": ObjectId(aid)})
    mongo.ea_pairing_codes.delete_many({"account_id": aid})
    mongo.ea_deployments.delete_many({"account_id": aid})
    mongo.installations.delete_many({"account_id": aid})
    mongo.execution_leases.delete_many({"account_id": aid})


# ─── pairing creation: digest-only ──────────────────────────────
def test_01_pairing_create_digest_only(sess, mongo, synth_account):
    r = sess.post(f"{API}/infra/pairing",
                  json={"account_id": synth_account}, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert re.match(r"^PAIR-[A-Z0-9]{4}-[A-Z0-9]{4}$", j["code"])
    assert j["ea_deployment_id"].startswith("eadep_")
    assert "digest" in j.get("note", "").lower()

    doc = mongo.ea_pairing_codes.find_one({"account_id": synth_account})
    assert doc, "pairing code doc missing"
    assert "code" not in doc, "plaintext code stored!"
    assert len(doc["code_digest"]) == 64
    pytest._iter114_code = j["code"]
    pytest._iter114_orig_bridge = mongo.accounts.find_one(
        {"_id": ObjectId(synth_account)})["bridge_token"]


# ─── claim without terminal → 400 ───────────────────────────────
def test_02_claim_without_terminal(sess):
    r = sess.post(f"{API}/infra/pairing/claim",
                  json={"code": pytest._iter114_code}, timeout=15)
    assert r.status_code == 400
    assert "terminal" in r.text.lower()


# ─── claim with terminal → 200 rotate token, connected=false ────
def test_03_claim_with_terminal(sess, mongo, synth_account):
    r = sess.post(f"{API}/infra/pairing/claim",
                  json={"code": pytest._iter114_code,
                        "terminal": {"terminal_path": "C:\\STOIC\\MT5\\a\\",
                                     "host_fingerprint": "hostA"}},
                  timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["installation_id"].startswith("inst_")
    assert j["bridge_token"].startswith("tok_")
    assert j["bridge_token"] != pytest._iter114_orig_bridge, "token not rotated"
    assert j["connected"] is False
    assert j["lease_seconds"] == 20
    pytest._iter114_installation = j["installation_id"]


# ─── re-claim same code → 401 ───────────────────────────────────
def test_04_reclaim_rejected(sess):
    r = sess.post(f"{API}/infra/pairing/claim",
                  json={"code": pytest._iter114_code,
                        "terminal": {"terminal_path": "x",
                                     "host_fingerprint": "y"}},
                  timeout=15)
    assert r.status_code == 401


# ─── re-pair blocked while lease active ─────────────────────────
def test_05_repair_blocked_by_lease(sess, synth_account):
    r = sess.post(f"{API}/infra/pairing",
                  json={"account_id": synth_account}, timeout=15)
    assert r.status_code == 409, r.text
    assert "execution owner" in r.text.lower()


# ─── re-pair with revoke_existing → old installation revoked ────
def test_06_repair_with_revoke(sess, mongo, synth_account):
    r = sess.post(f"{API}/infra/pairing",
                  json={"account_id": synth_account,
                        "revoke_existing": True}, timeout=15)
    assert r.status_code == 200, r.text

    old = mongo.installations.find_one(
        {"installation_id": pytest._iter114_installation})
    assert old and old.get("revoked") is True


# ─── GET /infra/ea-deployments returns state machine ladder ─────
def test_07_ea_deployments_list(sess, synth_account):
    r = sess.get(f"{API}/infra/ea-deployments", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert len(j["states"]) == 10
    assert j["lease_seconds"] == 20
    # find our deployment
    mine = [d for d in j["deployments"]
            if d["account_id"] == synth_account]
    assert mine, "our deployment not listed"
    d = mine[0]
    assert d["connected"] is False
    # state may be TOKEN_ISSUED (fresh code from test_06) or later
    assert d["state"] in j["states"]


# ─── connect-existing: no irm|iex, no 'quick' key ───────────────
def test_08_connect_existing_no_quick_no_irm(sess):
    r = sess.post(f"{API}/infra/vps/connect-existing",
                  json={"provider_name": "own",
                        "label": f"{TAG}_vps",
                        "region": "test"}, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    ic = j["install_commands"]
    assert "quick" not in ic, f"'quick' key present: {ic}"
    body = str(ic)
    assert "irm" not in body.lower() and "| iex" not in body.lower()
    pytest._iter114_dep_id = j["deployment_id"]
    pytest._iter114_enroll = j["enrollment_code"]


# ─── artifacts manifest has ex5 entry ───────────────────────────
def test_09_manifest_has_ex5(sess):
    r = sess.get(f"{API}/infra/artifacts/manifest", timeout=15)
    assert r.status_code == 200
    j = r.json()
    arts = j.get("artifacts") or j.get("items") or []
    if not arts and isinstance(j, dict):
        # scan any list value
        for v in j.values():
            if isinstance(v, list):
                arts = v
                break
    ex5 = [a for a in arts if a.get("name") == "stoic-ea-ex5"
           or a.get("type") == "ex5"]
    assert ex5, f"no ex5 artifact in manifest: {j}"
    note = str(ex5[0].get("note") or "").lower()
    assert "recompil" in note or "never" in note


# ─── register agent for progress + install_ea tests ─────────────
@pytest.fixture(scope="module")
def agent(mongo, sess):
    # trigger connect-existing (done in test_08 but ensure isolated)
    r = sess.post(f"{API}/infra/vps/connect-existing",
                  json={"provider_name": "own",
                        "label": f"{TAG}_ag",
                        "region": "test"}, timeout=15)
    j = r.json()
    dep_id = j["deployment_id"]
    enroll = j["enrollment_code"]
    reg = requests.post(f"{API}/infra/agent/register",
                        json={"enrollment_code": enroll,
                              "machine_fingerprint": f"fp_{TAG}"},
                        timeout=15)
    assert reg.status_code == 200, reg.text
    data = reg.json()
    data["deployment_id"] = dep_id
    yield data
    mongo.vps_agents.delete_one({"agent_id": data["agent_id"]})
    mongo.vps_deployments.delete_one({"deployment_id": dep_id})
    mongo.vps_bootstrap_tokens.delete_many({"deployment_id": dep_id})
    mongo.agent_commands.delete_many({"agent_id": data["agent_id"]})


# ─── progress: agent may only report certain states ─────────────
def test_10_progress_agent_allowed(sess, agent, synth_account):
    # ARTIFACT_VERIFIED then EA_INSTALLED
    for state in ("ARTIFACT_VERIFIED", "EA_INSTALLED"):
        r = requests.post(f"{API}/infra/ea-deploy/progress",
                          json={"agent_token": agent["agent_token"],
                                "state": state,
                                "account_id": synth_account}, timeout=15)
        # NOTE: the deployment was created for admin's synth_account (fine).
        # But the agent belongs to admin too → same user_id → should work
        assert r.status_code == 200, f"{state}: {r.status_code} {r.text}"


def test_11_progress_token_claimed_forbidden(sess, agent, synth_account):
    r = requests.post(f"{API}/infra/ea-deploy/progress",
                      json={"agent_token": agent["agent_token"],
                            "state": "TOKEN_CLAIMED",
                            "account_id": synth_account}, timeout=15)
    assert r.status_code == 400


# ─── install_ea command guard ───────────────────────────────────
def test_12_install_ea_without_terminal_400(sess, agent):
    r = sess.post(
        f"{API}/infra/agents/{agent['agent_id']}/commands",
        json={"command": "install_ea", "params": {}}, timeout=15)
    assert r.status_code == 400
    assert "terminal" in r.text.lower()


def test_13_install_ea_with_terminal_queued(sess, agent):
    r = sess.post(
        f"{API}/infra/agents/{agent['agent_id']}/commands",
        json={"command": "install_ea",
              "params": {"terminal_path": "C:\\MT5\\x\\",
                         "account_ref": "9990000"}}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("status") in ("queued", "ok") or \
        r.json().get("command_id")
