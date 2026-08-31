"""HTTP e2e — iter-172 one-click trusted terminal.

Flow: create account → EA heartbeat (no installation_id, unverified) →
owner clicks trust-terminal → next plain heartbeat resolves the trusted
installation and verifies the FULL identity chain (ea_identity
authoritative + verified_identity stamped + lease renewed).
"""
import uuid

import pytest
import requests

from live_target import require_live_base_url

pytestmark = pytest.mark.http

BASE_URL = require_live_base_url()
TIMEOUT = 20


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def account(session):
    login = str(90000000 + int(uuid.uuid4().hex[:6], 16) % 9000000)
    r = session.post(f"{BASE_URL}/api/accounts", json={
        "label": f"trusttest-{uuid.uuid4().hex[:6]}",
        "broker": "TrustTestBroker", "server": "TrustTest-Demo",
        "account_number": login, "account_type": "demo",
        "mode": "live"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    acc = r.json()
    acc_id = acc.get("id") or acc.get("account_id")
    tok = session.post(f"{BASE_URL}/api/accounts/{acc_id}/rotate-token",
                       timeout=TIMEOUT)
    assert tok.status_code == 200, tok.text
    yield {"id": acc_id, "login": login,
           "bridge_token": tok.json()["bridge_token"]}
    session.delete(f"{BASE_URL}/api/accounts/{acc_id}?force=true",
                   timeout=TIMEOUT)


def _heartbeat(account):
    r = requests.post(f"{BASE_URL}/api/bridge/heartbeat", json={
        "bridge_token": account["bridge_token"],
        "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
        "account_login": int(account["login"]),
        "broker_server": "TrustTest-Demo",
        "ea_version": "1.56", "terminal_build": 4400,
        "positions": []}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text


def _find_account(session, acc_id):
    r = session.get(f"{BASE_URL}/api/accounts", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return next(a for a in r.json() if a["id"] == acc_id)


def test_trust_requires_fresh_heartbeat(session, account):
    r = session.post(
        f"{BASE_URL}/api/accounts/{account['id']}/trust-terminal",
        timeout=TIMEOUT)
    assert r.status_code == 409, r.text
    assert "heartbeat" in str(r.json().get("detail", "")).lower()


def test_trust_then_plain_heartbeat_verifies_chain(session, account):
    _heartbeat(account)
    a = _find_account(session, account["id"])
    assert not a.get("verified_identity")

    r = session.post(
        f"{BASE_URL}/api/accounts/{account['id']}/trust-terminal",
        timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    inst_id = body["installation_id"]
    assert inst_id.startswith("inst_")

    # stamped immediately for instant authority
    a = _find_account(session, account["id"])
    assert (a.get("verified_identity") or {}).get(
        "installation_id") == inst_id

    # a subsequent PLAIN heartbeat (no installation_id) resolves the
    # trusted installation and verifies the full chain
    _heartbeat(account)
    a = _find_account(session, account["id"])
    ident = a.get("ea_identity") or {}
    assert ident.get("installation_id") == inst_id
    assert ident.get("authoritative") is True
    assert ident.get("reason") is None
    assert not a.get("broker_account_mismatch")

    # idempotent — second click reports already verified
    r = session.post(
        f"{BASE_URL}/api/accounts/{account['id']}/trust-terminal",
        timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json().get("already_verified") is True


def test_installations_list_and_revoke(session, account):
    """iter-173 — trusted terminals list + one-tap revoke."""
    r = session.get(
        f"{BASE_URL}/api/accounts/{account['id']}/installations",
        timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    insts = body["installations"]
    assert len(insts) == 1
    inst = insts[0]
    assert inst["method"] == "user_trust"
    assert inst["is_current"] is True
    assert inst["fingerprint"]["account_login"] == account["login"]

    r = session.post(
        f"{BASE_URL}/api/accounts/{account['id']}/installations/"
        f"{inst['installation_id']}/revoke", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # identity stripped + list empty + double revoke → 404
    a = _find_account(session, account["id"])
    assert not a.get("verified_identity")
    assert (a.get("ea_identity") or {}).get("authoritative") is False
    r = session.get(
        f"{BASE_URL}/api/accounts/{account['id']}/installations",
        timeout=TIMEOUT)
    assert r.json()["installations"] == []
    r = session.post(
        f"{BASE_URL}/api/accounts/{account['id']}/installations/"
        f"{inst['installation_id']}/revoke", timeout=TIMEOUT)
    assert r.status_code == 404

    # a plain heartbeat after revoke stays UNVERIFIED (trust is dead)
    _heartbeat(account)
    a = _find_account(session, account["id"])
    assert (a.get("ea_identity") or {}).get("authoritative") is False
