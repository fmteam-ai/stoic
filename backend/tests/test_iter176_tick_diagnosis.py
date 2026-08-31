"""HTTP e2e — iter-176 tick-ingress diagnosis on /scalp/status."""
import time
import uuid

import pytest
import requests

from live_target import require_live_base_url

pytestmark = pytest.mark.http

BASE = require_live_base_url()
API = f"{BASE}/api"


@pytest.fixture(scope="module")
def ctx():
    from helpers import mark_email_verified
    s = requests.Session()
    email = f"TEST_iter176_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"terms_agreed": True, "email": email,
                     "password": "Vx7#Qm2pL9wTzK4e", "name": "iter176"},
               timeout=30)
    assert r.status_code == 200, r.text
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": "Vx7#Qm2pL9wTzK4e"},
               timeout=30)
    assert r.status_code == 200, r.text
    r = s.post(f"{API}/accounts", json={
        "label": "TEST_iter176_acc", "broker": "TestBroker", "server": "T",
        "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
        "base_currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    acc = r.json()
    r = s.post(f"{API}/accounts/{acc['id']}/rotate-token", timeout=15)
    assert r.status_code == 200, r.text
    acc["bridge_token"] = r.json()["bridge_token"]
    r = s.post(f"{API}/scalp/config", json={
        "account_id": acc["id"], "symbol": "EURUSD",
        "enabled": True, "mode": "shadow"}, timeout=15)
    assert r.status_code == 200, r.text
    yield {"s": s, "acc": acc, "email": email}
    s.delete(f"{API}/accounts/{acc['id']}?force=true", timeout=15)


def _status_runner(ctx):
    r = ctx["s"].get(f"{API}/scalp/status?account_id={ctx['acc']['id']}",
                     timeout=15)
    assert r.status_code == 200, r.text
    runners = r.json()["runners"]
    assert runners, "runner should hydrate from persisted config"
    return runners[0]


def test_status_diagnoses_never_started_stream(ctx):
    st = _status_runner(ctx)
    assert st["ingress"] is None            # never sent a batch
    assert st["terminal"] is not None
    assert st["terminal"]["connected"] is False   # no heartbeat yet


def test_status_after_heartbeat_and_ticks(ctx):
    r = requests.post(f"{API}/bridge/heartbeat", json={
        "bridge_token": ctx["acc"]["bridge_token"],
        "balance": 1000.0, "equity": 1000.0, "open_positions": 0,
        "account_login": 90176001, "broker_server": "T",
        "client_version": "1.56", "positions": []}, timeout=15)
    assert r.status_code == 200, r.text
    st = _status_runner(ctx)
    assert st["terminal"]["connected"] is True

    now = int(time.time() * 1000)
    ticks = [{"tm": now - (20 - i) * 200, "b": 1.10000 + i * 1e-5,
              "a": 1.10012 + i * 1e-5} for i in range(20)]
    r = requests.post(f"{API}/bridge/ticks", json={
        "bridge_token": ctx["acc"]["bridge_token"], "symbol": "EURUSD",
        "sent_at_ms": ticks[-1]["tm"], "ticks": ticks}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    st = _status_runner(ctx)
    assert st["ingress"] is not None
    assert st["ingress"]["last_status"] == "ok"
    assert st["ingress"]["last_ticks"] == 20
    assert st["counters"]["ticks"] > 0
