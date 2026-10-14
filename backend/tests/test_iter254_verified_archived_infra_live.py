"""Iteration 254 — live preview checks (GET only, non-destructive):
Verified Performance headline/archived split, Infra shadow clock detail + server backups, presets cap-fit regression.
Creds come from backend/.env (TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD) — never hardcoded."""
import os as _os_p
_BACKEND_ROOT = _os_p.path.dirname(_os_p.path.dirname(_os_p.path.abspath(__file__)))
_ENV_BE = _os_p.path.join(_BACKEND_ROOT, ".env")
_ENV_FE = _os_p.path.join(_os_p.path.dirname(_BACKEND_ROOT), "frontend", ".env")
import os
import pytest
import requests
from dotenv import dotenv_values

pytestmark = pytest.mark.http

_FE = dotenv_values(_ENV_FE)
_BE = dotenv_values(_ENV_BE)
BASE = (os.environ.get("REACT_APP_BACKEND_URL") or _FE["REACT_APP_BACKEND_URL"]).rstrip("/")
EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or _BE.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD") or _BE.get("TEST_ADMIN_PASSWORD")
ACCOUNT_ID = "6a435003545b76de9539d197"


@pytest.fixture(scope="module")
def s():
    if not PASSWORD:
        pytest.skip("TEST_ADMIN_PASSWORD missing")
    sess = requests.Session()
    r = sess.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code}")
    return sess


@pytest.fixture(scope="module")
def verified(s):
    r = s.get(f"{BASE}/api/performance/verified", timeout=60)
    assert r.status_code == 200, r.text[:300]
    return r.json()


# --- (A) Verified performance: headline vs archived ---
def test_verified_shape(verified):
    for k in ("accounts", "archived", "integrity", "overall"):
        assert k in verified
    for k in ("closed_trades", "broker_verified_trades", "verified_pct", "accounts_included",
              "archived_accounts_excluded", "archived_deals_excluded"):
        assert k in verified["integrity"], k


def test_headline_equals_current_accounts(verified):
    assert verified["overall"]["deals"] == sum(a["deals"] for a in verified["accounts"])
    labels = [a["label"] for a in verified["accounts"]]
    assert labels == ["Start Trader"], labels
    for a in verified["accounts"]:
        assert a["environment"] and a["environment"] != "UNKNOWN", a


def test_archived_rows(verified):
    arch = verified["archived"]
    assert len(arch) == 7, len(arch)
    keys = {"label", "environment", "deals", "closed_positions", "net_pnl", "tombstone", "account_id_suffix", "deleted_at"}
    for row in arch:
        assert keys <= set(row), set(row)
        assert isinstance(row["tombstone"], bool)
        assert row["label"].startswith("DELETED ACCOUNT"), row["label"]
        assert row["environment"] == "UNKNOWN"
    total = sum(r["deals"] for r in arch)
    assert total == 2104, total
    assert verified["integrity"]["archived_deals_excluded"] == total
    assert verified["integrity"]["archived_accounts_excluded"] == 7


def test_integrity_same_deal_set(verified):
    i, o = verified["integrity"], verified["overall"]
    assert i["broker_verified_trades"] == o["closed_positions"]
    assert i["accounts_included"] >= len(verified["accounts"])
    assert i["closed_trades"] >= i["broker_verified_trades"]


# --- (B) Infra: shadow clock + backups ---
def test_cert_clock(s):
    r = s.get(f"{BASE}/api/infra/certification", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d["checks"]["clock_synchronization"] is True
    assert d["details"]["clock_synchronization"] == {"source": "ea_broker_time", "offset_sec": 10800,
                                                     "account": "Start Trader"}


def test_infra_overview_backups(s):
    r = s.get(f"{BASE}/api/infra/overview", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["backups"], list)
    sb = d["server_backups"]
    assert sb["mounted"] is False and sb["files"] == [] and sb["newest_age_h"] is None
    assert sb["dir"]


def test_infra_unauth():
    assert requests.get(f"{BASE}/api/infra/overview", timeout=30).status_code == 401
    assert requests.get(f"{BASE}/api/performance/verified", timeout=30).status_code == 401


# --- regression: cap-aware presets ---
def test_presets_cap_fit(s):
    r = s.get(f"{BASE}/api/bot/presets", params={"account_id": ACCOUNT_ID}, timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d.get("policy_risk_cap_pct") == 1.0, d.get("policy_risk_cap_pct")
    presets = d.get("presets") or []
    assert presets
    for p in presets:
        assert "cap_fit" in p, p.get("id") or p.get("key")
