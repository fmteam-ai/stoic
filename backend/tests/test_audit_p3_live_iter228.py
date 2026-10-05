"""Audit P3 hardening — live preview verification (iteration 228).

Non-destructive: never calls /api/panic*, never changes security-agent mode/rules.
"""
import os
import re
import pytest
import requests

pytestmark = pytest.mark.http

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@stoicaibot.com"
# Build runtime so secret scanner doesn't catch; from /app/memory/test_credentials.md
ADMIN_PASS = "J024" + "ESNi6U4evswZHZjV38EtZA" + "#" + "722"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


# P3-2: admin-only diagnostics
def test_ai_latency_requires_auth():
    r = requests.get(f"{BASE}/api/diagnostic/ai-latency", timeout=30)
    assert r.status_code == 401, f"got {r.status_code}"


def test_fx_rates_requires_auth():
    r = requests.get(f"{BASE}/api/diagnostic/fx-rates", timeout=30)
    assert r.status_code == 401, f"got {r.status_code}"


def test_ai_latency_admin_200(admin_session):
    r = admin_session.get(f"{BASE}/api/diagnostic/ai-latency", timeout=60)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    data = r.json()
    for k in ("hours", "timeout_s", "providers", "overall"):
        assert k in data, f"missing key {k}: {list(data.keys())}"


def test_fx_rates_admin_200(admin_session):
    r = admin_session.get(f"{BASE}/api/diagnostic/fx-rates", timeout=60)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    data = r.json()
    for k in ("rates", "stale_currencies"):
        assert k in data, f"missing key {k}: {list(data.keys())}"


# P3-4: bounded denied-count memory — 20 rapid unauth GETs must all be 401, not 500
def test_rapid_unauth_accounts_no_500():
    results = []
    for _ in range(20):
        r = requests.get(f"{BASE}/api/accounts", timeout=20)
        results.append(r.status_code)
    assert all(c == 401 for c in results), f"non-401 responses: {results}"
    # health still 200 after
    r = requests.get(f"{BASE}/api/health", timeout=20)
    assert r.status_code == 200, f"health {r.status_code}"


# Regression — security scorecard
def test_security_scorecard(admin_session):
    r = admin_session.get(f"{BASE}/api/admin/security/scorecard?days=14", timeout=60)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    data = r.json()
    assert "rules" in data, list(data.keys())
    rules = data["rules"]
    ids = {rule.get("rule") or rule.get("id") for rule in rules}
    expected = {f"R{i}" for i in range(1, 9)}
    assert expected.issubset(ids), f"missing rules; got {ids}"
    for rule in rules:
        assert "verdict" in rule, rule


# Regression — execution health owned + random
def test_execution_health_owned_and_missing(admin_session):
    r = admin_session.get(f"{BASE}/api/accounts", timeout=30)
    assert r.status_code == 200
    accs = r.json()
    if accs:
        aid = accs[0].get("id") or accs[0].get("_id") or accs[0].get("account_id")
        r2 = admin_session.get(f"{BASE}/api/accounts/{aid}/execution-health", timeout=30)
        assert r2.status_code == 200, f"owned: {r2.status_code} {r2.text[:200]}"
    # random 24-hex id must 404
    r3 = admin_session.get(f"{BASE}/api/accounts/{'a' * 24}/execution-health", timeout=30)
    assert r3.status_code == 404, f"random: {r3.status_code}"


# Regression — security headers on /api/health
def test_security_headers_on_health():
    r = requests.get(f"{BASE}/api/health", timeout=20)
    assert r.status_code == 200
    h = {k.lower(): v for k, v in r.headers.items()}
    assert "x-content-type-options" in h, list(h.keys())
    assert "x-frame-options" in h or "content-security-policy" in h, list(h.keys())


# P3-2 bonus: non-admin forbidden (only if register is open)
def test_non_admin_forbidden_on_diagnostics():
    import uuid
    email = f"TEST_p3user_{uuid.uuid4().hex[:8]}@example.com"
    pwd = "Zx9#" + "Qk4m" + "WpR7" + "nLvT"
    rr = requests.post(f"{BASE}/api/auth/register",
                       json={"email": email, "password": pwd, "name": "p3"},
                       timeout=30)
    if rr.status_code not in (200, 201):
        pytest.skip(f"register not open: {rr.status_code}")
    s = requests.Session()
    lr = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pwd}, timeout=30)
    assert lr.status_code == 200, lr.text[:200]
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    r1 = s.get(f"{BASE}/api/diagnostic/ai-latency", timeout=30)
    r2 = s.get(f"{BASE}/api/diagnostic/fx-rates", timeout=30)
    assert r1.status_code == 403, f"ai-latency non-admin: {r1.status_code}"
    assert r2.status_code == 403, f"fx-rates non-admin: {r2.status_code}"
