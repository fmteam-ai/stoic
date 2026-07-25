"""iter-100 HTTP smoke: Batch A — Digital Twin, Strategy Genetics, Calibration."""
import os
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") + "/api"
ADMIN = ("admin@trading.bot", "admin123")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


# ------------------------------------------------------- Twin
def test_twin_summary_requires_auth():
    r = requests.get(f"{BASE}/twin/summary", timeout=15)
    assert r.status_code in (401, 403), r.text


def test_twin_summary_shape(admin_session):
    r = admin_session.get(f"{BASE}/twin/summary?days=30", timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("days", "accounts", "totals", "top_divergences"):
        assert k in body, f"missing {k}: {body.keys()}"
    assert body["days"] == 30
    assert isinstance(body["accounts"], list)
    for k in ("live_pnl", "alt_r", "intercepts_replayed"):
        assert k in body["totals"]
    for a in body["accounts"]:
        for k in ("account_id", "label", "live", "twin", "verdict"):
            assert k in a
        for k in ("trades", "wins", "losses", "pnl", "win_rate"):
            assert k in a["live"]
        for k in ("replayed", "would_win", "would_lose", "timeout",
                  "alt_r", "alt_pnl_est", "twin_pnl_est"):
            assert k in a["twin"]
        assert a["verdict"] in {"GATES PROTECTING", "GATES COSTING EDGE",
                                "NEUTRAL", "NO INTERCEPTS"}


def test_twin_summary_days_clamped(admin_session):
    r = admin_session.get(f"{BASE}/twin/summary?days=999", timeout=60)
    assert r.status_code == 200
    # `days` in response echoes the clamped value (min(max(999,1),90) = 90)
    assert r.json()["days"] == 90


# ------------------------------------------------------- Genetics
def test_genetics_requires_auth():
    r = requests.get(f"{BASE}/genetics/lineage", timeout=15)
    assert r.status_code in (401, 403), r.text


def test_genetics_lineage_shape(admin_session):
    r = admin_session.get(f"{BASE}/genetics/lineage", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("policies", "versions", "events"):
        assert k in body, f"missing {k}: {body.keys()}"
    assert "risk_policy" in body["policies"]
    assert "execution_policy" in body["policies"]
    assert "feature_schema" in body["policies"]
    assert isinstance(body["versions"], list) and len(body["versions"]) >= 1
    for v in body["versions"]:
        for k in ("engine", "current_version"):
            assert k in v
        assert "performance" in v
    assert isinstance(body["events"], list)
    valid_kinds = {"governance", "auto_guard", "research", "tuning"}
    for e in body["events"]:
        assert e["kind"] in valid_kinds


# ------------------------------------------------------- Calibration
def test_calibration_requires_auth():
    r = requests.get(f"{BASE}/analytics/calibration", timeout=15)
    assert r.status_code in (401, 403), r.text


def test_calibration_shape_and_math(admin_session):
    r = admin_session.get(f"{BASE}/analytics/calibration?days=90", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 90
    assert "summary" in body and "engines" in body
    # If admin has closed trades, summary should be present with math check
    if body["summary"]:
        s = body["summary"]
        for k in ("predicted", "actual", "calibration_error", "n"):
            assert k in s
        # Recompute summary math from engines and compare (tolerate rounding)
        tot_n = 0
        stated_sum = realized_sum = err_sum = 0.0
        for ent in body["engines"].values():
            for b in ent["buckets"]:
                n = b["n"]
                tot_n += n
                stated_sum += b["stated"] * n
                realized_sum += b["realized"] * n
                err_sum += abs(b["gap"]) * n
        assert tot_n == s["n"]
        if tot_n:
            assert abs(round(stated_sum / tot_n, 1) - s["predicted"]) <= 0.2
            assert abs(round(realized_sum / tot_n, 1) - s["actual"]) <= 0.2
            assert abs(round(err_sum / tot_n, 1) - s["calibration_error"]) <= 0.2
