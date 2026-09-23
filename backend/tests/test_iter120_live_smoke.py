from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Live external URL smoke tests for v53 batch A+B+C+D — iter120."""
import os
import time
import uuid
import requests
import pytest

from live_target import require_live_base_url
BASE = require_live_base_url()
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
STEP_UP = os.environ.get("STEP_UP_BYPASS_TOKEN") or open(_ENV_PATH).read().split("STEP_UP_BYPASS_TOKEN=")[1].split()[0]
RL_BYPASS = os.environ.get("RATE_LIMIT_BYPASS_TOKEN") or open(_ENV_PATH).read().split("RATE_LIMIT_BYPASS_TOKEN=")[1].split()[0]


def _login(email, password):
    s = requests.Session()
    s.headers.update({"X-Step-Up-Bypass": STEP_UP, "X-RateLimit-Bypass": RL_BYPASS})
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin_a():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def admin_b():
    return _login("admin@stoicaibot.com", ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def program_id(admin_a):
    r = admin_a.get(f"{BASE}/api/pamm/programs?limit=50", timeout=30)
    assert r.status_code == 200, r.text
    progs = r.json().get("programs") or r.json()
    assert progs, "no programs listed"
    # prefer Alpha Managed Fund
    for p in progs:
        if p.get("name") == "Alpha Managed Fund":
            return p["program_id"]
    return progs[0]["program_id"]


def test_program_listed(program_id):
    assert program_id.startswith("pgm_")


def test_invalid_curve_rejected(admin_a, program_id):
    # invalid curve should be 400
    r = admin_a.put(
        f"{BASE}/api/pamm/programs/{program_id}/risk-limits",
        json={"daily_loss_pct": {"curve": "parabolic"}},
        timeout=30,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"


def test_valid_curve_change_persists(admin_a, program_id):
    # curve-only change should NOT trigger dual-auth per problem statement
    r = admin_a.put(
        f"{BASE}/api/pamm/programs/{program_id}/risk-limits",
        json={"daily_loss_pct": {"curve": "logistic"}},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body.get("pending_approval"), f"curve-only should not need dual-auth: {body}"
    g = admin_a.get(f"{BASE}/api/pamm/programs/{program_id}", timeout=30)
    assert g.status_code == 200
    prog = g.json().get("program", g.json())
    limits = prog.get("risk_limits") or {}
    dlp = limits.get("daily_loss_pct") or {}
    assert dlp.get("curve") == "logistic", f"curve did not persist: {limits}"
    admin_a.put(
        f"{BASE}/api/pamm/programs/{program_id}/risk-limits",
        json={"daily_loss_pct": {"curve": "linear"}},
        timeout=30,
    )


def test_verdict_trade_check(admin_a, program_id):
    r = admin_a.post(
        f"{BASE}/api/pamm/programs/{program_id}/trade-verdict",
        json={"requested_risk_pct": 0.30},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("verdict") in ("APPROVE", "REDUCE", "REJECT"), body
    assert "approved_risk_pct" in body


def test_dual_auth_leverage_cap_increase(admin_a, admin_b, program_id):
    payload = {"kind": "leverage_cap_increase", "payload": {"new_leverage_cap": 50.0}, "reason": "iter120 smoke"}
    r = admin_a.post(f"{BASE}/api/pamm/programs/{program_id}/change-requests", json=payload, timeout=30)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    cid = body.get("change_id") or body.get("id")
    assert cid, body
    # unknown kind
    bad = admin_a.post(
        f"{BASE}/api/pamm/programs/{program_id}/change-requests",
        json={"kind": "not_a_real_kind", "payload": {}, "reason": "x"},
        timeout=30,
    )
    assert bad.status_code == 400, bad.text
    # approve by second admin
    ap = admin_b.post(
        f"{BASE}/api/pamm/change-requests/{cid}/approve",
        json={},
        timeout=30,
    )
    assert ap.status_code == 200, ap.text
    # verify governance recorded
    g = admin_a.get(f"{BASE}/api/pamm/programs/{program_id}", timeout=30)
    gbody = g.json()
    prog = gbody.get("program", gbody)
    gov = (prog.get("governance") or {}).get("leverage_cap_increase")
    assert gov, f"governance.leverage_cap_increase not recorded: {prog.get('governance')}"


def test_flatten_failed_banner_via_mongo_toggle(admin_a, program_id):
    """Verify the API response surfaces flatten_failed when Mongo has it set (banner data source)."""
    import subprocess
    # set flatten_failed
    js = (
        'db.pamm_programs.updateOne('
        '{program_id:"' + program_id + '"},'
        '{$set:{flatten_failed:{attempts:2, remaining:3, first_at:new Date(), at:new Date()}}}'
        ')'
    )
    subprocess.run(["mongosh", "ai_trading_bot", "--quiet", "--eval", js], check=True, capture_output=True)
    try:
        r = admin_a.get(f"{BASE}/api/pamm/programs/{program_id}", timeout=30)
        assert r.status_code == 200
        rbody = r.json()
        prog = rbody.get("program", rbody)
        assert prog.get("flatten_failed"), f"flatten_failed not exposed on GET program: {list(prog.keys())}"
    finally:
        subprocess.run(
            ["mongosh", "ai_trading_bot", "--quiet", "--eval",
             'db.pamm_programs.updateOne({program_id:"' + program_id + '"},{$unset:{flatten_failed:1}})'],
            check=True, capture_output=True,
        )
        # confirm cleanup
        r2 = admin_a.get(f"{BASE}/api/pamm/programs/{program_id}", timeout=30)
        p2 = r2.json().get("program", r2.json())
        assert not p2.get("flatten_failed"), "flatten_failed not cleaned up"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
