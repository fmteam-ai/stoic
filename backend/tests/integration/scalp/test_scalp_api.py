"""Scalp fast-path INTEGRATION tests — require the running backend.

Environment resolution happens lazily inside fixtures, never at collection.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))


def _api_base() -> str:
    url = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
    if not url:
        env = BACKEND.parent / "frontend" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("REACT_APP_BACKEND_URL"):
                    url = line.split("=", 1)[1].strip().strip('"').rstrip("/")
    if not url:
        pytest.skip("no backend URL configured")
    return f"{url}/api"


def _impulse_pullback_path(pip=0.0001):
    base = 1.08000
    path = [base] * 140
    path += [base + i * 0.3 * pip for i in range(1, 11)]
    top = path[-1]
    path += [top - i * 0.25 * pip for i in range(1, 5)]
    low = path[-1]
    inc = 0.0
    for step in (0.05, 0.1, 0.15, 0.2):
        inc += step * pip
        path.append(low + inc)
    return path


class TestScalpApi:
    @staticmethod
    @pytest.fixture(scope="class")
    def ctx():
        api = _api_base()
        s = requests.Session()
        email = f"TEST_scalp_{uuid.uuid4().hex[:6]}@example.com"
        s.post(f"{api}/auth/register", json={"terms_agreed": True, "email": email,
                                             "password": "Vq7#Xn4bT8wKm2Ye"}, timeout=15)
        from helpers import mark_email_verified, make_elite
        mark_email_verified(email)
        make_elite(email)  # starter caps at 1 account; e2e needs a 2nd
        s.post(f"{api}/auth/login", json={"email": email, "password": "Vq7#Xn4bT8wKm2Ye"},
               timeout=30)
        acc = s.post(f"{api}/accounts", json={
            "label": "TEST_ScalpAcc", "broker": "Exness", "server": "T",
            "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
            "base_currency": "USD"}, timeout=10).json()
        # SEC — bridge_token is no longer bulk-returned; fetch on demand
        acc["bridge_token"] = s.get(
            f"{api}/accounts/{acc['id']}/bridge-token",
            timeout=10).json()["bridge_token"]
        return {"api": api, "s": s, "acc": acc}

    def test_config_rejects_unapproved_symbol(self, ctx):
        r = ctx["s"].post(f"{ctx['api']}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "XAUUSD", "enabled": True},
            timeout=10)
        assert r.status_code == 422

    def test_demo_live_requires_confirm(self, ctx):
        r = ctx["s"].post(f"{ctx['api']}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "demo_live", "confirm_live": False}, timeout=10)
        assert r.status_code == 422

    def test_enable_shadow_with_commission(self, ctx):
        r = ctx["s"].post(f"{ctx['api']}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "shadow",
            "commission_usd_per_lot_side": 3.5}, timeout=10)
        assert r.status_code == 200
        st = r.json()["status"]
        assert st["enabled"] is True and st["risk_restored"] is True
        assert "|" in st["model_key"]           # broker-keyed model

    def test_bridge_ticks_ingest(self, ctx):
        now = int(time.time() * 1000)
        ticks = [{"tm": now - 1000 + i * 100, "b": 1.08000 + i * 0.00001,
                  "a": 1.08006 + i * 0.00001} for i in range(10)]
        r = requests.post(f"{ctx['api']}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"], "symbol": "EURUSD",
            "sent_at_ms": now, "ticks": ticks}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["ticks"] == 10
        assert body["risk_restored"] is True

    def test_bridge_ticks_unapproved_ignored(self, ctx):
        r = requests.post(f"{ctx['api']}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"], "symbol": "XAUUSD",
            "sent_at_ms": 1, "ticks": [{"tm": 1, "b": 2000.0, "a": 2000.5}]},
            timeout=10)
        assert r.status_code == 200 and r.json()["status"] == "ignored"

    def test_metrics_and_retrain(self, ctx):
        r = ctx["s"].get(f"{ctx['api']}/scalp/metrics?symbol=EURUSD", timeout=10)
        assert r.status_code == 200
        r = ctx["s"].post(
            f"{ctx['api']}/scalp/retrain?symbol=EURUSD&account_id={ctx['acc']['id']}",
            timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert "model_key" in body and body["model_key"].startswith("Exness|")

    def test_e2e_decision_recorded_via_bridge(self, ctx):
        """Impulse-pullback ticks → decision doc with fail-closed permission
        verdict, correct dataset tag and hot-path timing fields."""
        acc = ctx["s"].post(f"{ctx['api']}/accounts", json={
            "label": "TEST_ScalpE2E", "broker": "Exness", "server": "T",
            "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
            "base_currency": "USD"}, timeout=10).json()
        acc["bridge_token"] = ctx["s"].get(
            f"{ctx['api']}/accounts/{acc['id']}/bridge-token",
            timeout=10).json()["bridge_token"]
        r = ctx["s"].post(f"{ctx['api']}/scalp/config", json={
            "account_id": acc["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "shadow"}, timeout=10)
        assert r.status_code == 200
        now = int(time.time() * 1000)
        path = _impulse_pullback_path()
        t0 = now - (len(path) - 1) * 500
        ticks = [{"tm": t0 + i * 500, "b": round(m - 0.00003, 5),
                  "a": round(m + 0.00003, 5)} for i, m in enumerate(path)]
        r = requests.post(f"{ctx['api']}/bridge/ticks", json={
            "bridge_token": acc["bridge_token"], "symbol": "EURUSD",
            "sent_at_ms": now, "ticks": ticks}, timeout=15)
        assert r.status_code == 200 and r.json()["enabled"] is True
        time.sleep(1.5)
        docs = ctx["s"].get(f"{ctx['api']}/scalp/decisions?symbol=EURUSD",
                            timeout=10).json()["decisions"]
        docs = [d for d in docs if d["account_id"] == acc["id"]]
        assert docs, "expected a scalp decision from the impulse-pullback stream"
        d = docs[0]
        assert d["direction"] == "BUY"
        assert d["verdict"] == "rejected"          # permissions fail closed
        assert d["gates"]["permission"]["ok"] is False
        assert d["dataset"] == "candidate"
        assert d["decision_id"]
        assert d["signal_ts_ms"] <= d["ts_ms"]     # real initiating-tick timestamp
        assert d["broker"] == "Exness"
