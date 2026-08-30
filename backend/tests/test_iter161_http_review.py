"""iter-161 HTTP review — hits the live preview backend as admin.
Covers: P&L reconciliation, Ed25519 attestation, ops alerts scope, cc_status.
"""
import os
import re

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback to reading frontend/.env (relative — never a hardcoded path)
    _env = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "frontend", ".env")
    if os.path.exists(_env):
        for line in open(_env):
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
if not BASE_URL:
    pytest.skip("REACT_APP_BACKEND_URL not configured — live-stack HTTP "
                "suite runs in preview only", allow_module_level=True)

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


# ── P1-2 : /api/trades/stats reconciliation ───────────────────────────────
def test_trade_stats_reconciliation_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/trades/stats", timeout=15)
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    assert "reconciliation" in data, data
    rec = data["reconciliation"]
    for k in ("total_pnl", "sum_components", "components", "delta",
              "tolerance", "closed_trades", "status"):
        assert k in rec, f"missing key {k} in {rec}"
    assert rec["tolerance"] == 0.01
    assert rec["status"] in ("RECONCILED", "UNRECONCILED")
    assert isinstance(rec["components"], dict)
    assert isinstance(rec["closed_trades"], int)
    # arithmetic tie-out
    assert abs(rec["total_pnl"] - rec["sum_components"]) <= rec["tolerance"] + 1e-9 \
        or rec["status"] == "UNRECONCILED"
    # sum of components equals sum_components
    if rec["components"]:
        s = round(sum(rec["components"].values()), 2)
        assert abs(s - rec["sum_components"]) <= rec["tolerance"] + 1e-9


# ── P1-4 : Ed25519 attestation via public endpoints ───────────────────────
def test_performance_verified_ed25519_and_public_verify(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/performance/verified", timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    att = body.get("attestation") or body
    # normalise: sometimes shape is {attestation:{...}} vs top-level
    if "algo" not in att and "attestation" in body:
        att = body["attestation"]
    assert att.get("algo", "").startswith("Ed25519"), att
    assert att.get("key_id") == "perf-ed25519-v1"
    sig = att.get("signature", "")
    assert re.fullmatch(r"[0-9a-fA-F]{128}", sig), f"bad sig: {sig[:20]}..."
    assert att.get("public_key_b64"), "public_key_b64 missing"
    payload_hash = att.get("payload_hash")
    assert payload_hash

    # POST to public verify with same payload_hash + signature
    v = requests.post(f"{BASE_URL}/api/public/performance/verify",
                      json={"payload_hash": payload_hash, "signature": sig},
                      timeout=15)
    assert v.status_code == 200, v.text[:200]
    vjson = v.json()
    assert vjson.get("valid") is True, vjson
    assert vjson.get("public_key_b64")
    assert (vjson.get("algo") or "").startswith("Ed25519")

    # tampered signature → valid False
    bad = "f" + sig[1:]
    v2 = requests.post(f"{BASE_URL}/api/public/performance/verify",
                       json={"payload_hash": payload_hash, "signature": bad},
                       timeout=15)
    assert v2.status_code == 200
    assert v2.json().get("valid") is False


# ── P1-6/7 : /api/ops/alerts scope filter ────────────────────────────────
@pytest.mark.parametrize("scope", ["real", "synthetic", "all"])
def test_ops_alerts_scope(admin_session, scope):
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts?scope={scope}",
                          timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body.get("scope") == scope, body
    assert "unacked" in body
    assert "synthetic_unacked" in body
    assert isinstance(body.get("alerts", body.get("items", [])), list)


def test_ops_alerts_default_is_real(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("scope") == "real"


# ── P1-7 : /api/command-center/status excludes synthetic ─────────────────
def test_command_center_status_excludes_synthetic(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/command-center/status", timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    recent = body.get("recent_alerts") or \
        body.get("sections", {}).get("recent_alerts") or []
    for a in recent:
        assert a.get("synthetic") is not True, a
    workers = body.get("sections", {}).get("workers") or body.get("workers") or {}
    assert "open_critical_alerts" in workers, workers


# ── Regression : iter-160 P0 basics still intact ──────────────────────────
def test_bot_health_score_hard_caps(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "hard_caps" in body, body
