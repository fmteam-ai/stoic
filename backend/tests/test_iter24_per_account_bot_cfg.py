from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter 24 — per-account bot configs + max_lot_size cap.

User-facing contracts under test:
  - GET /api/bot/config            -> default profile (account_id=null)
  - GET /api/bot/config?account_id=X -> per-account override (auto-created)
  - GET /api/bot/configs           -> list every config the user owns
  - PUT /api/bot/config?account_id=X with max_lot_size persists & echoes back
  - DELETE /api/bot/config?account_id=X removes the override
  - POST /api/bot/start|stop?account_id=X is independent per scope
  - Unknown account_id returns 404 — protects against cross-tenant pokes
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pathlib
import pytest
import requests


def _read_env_backend_url() -> str:
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    return ""


_FRONT_ENV = pathlib.Path(_os.path.join(_REPO_DIR, "frontend", ".env"))
def _read_frontend_backend_url() -> str:
    if not _FRONT_ENV.exists():
        return ""
    for line in _FRONT_ENV.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    return ""


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _read_frontend_backend_url()
            or _read_env_backend_url()
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def primary_account(admin_session):
    r = admin_session.get(f"{API}/accounts", timeout=15)
    assert r.status_code == 200
    accs = r.json()
    assert accs, "Admin needs at least one MT5 account for this test"
    return accs[0]


def _delete_override(s, account_id):
    s.delete(f"{API}/bot/config", params={"account_id": account_id}, timeout=10)


class TestPerAccountBotConfig:
    def test_default_config_has_null_account_id(self, admin_session):
        r = admin_session.get(f"{API}/bot/config", timeout=10)
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["account_id"] is None
        assert "max_lot_size" in cfg
        assert cfg["max_lot_size"] >= 0

    def test_get_per_account_config_auto_creates(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        _delete_override(admin_session, acc_id)  # clean slate
        r = admin_session.get(f"{API}/bot/config",
                              params={"account_id": acc_id}, timeout=10)
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["account_id"] == acc_id
        # New override defaults are sane
        assert cfg["active"] is False
        assert cfg["max_lot_size"] == 0.0

    def test_put_max_lot_size_persists(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        r = admin_session.put(f"{API}/bot/config",
                              params={"account_id": acc_id},
                              json={"max_lot_size": 0.05, "risk_level": "low"},
                              timeout=10)
        assert r.status_code == 200, r.text
        cfg = r.json()
        assert cfg["account_id"] == acc_id
        assert cfg["max_lot_size"] == 0.05
        assert cfg["risk_level"] == "low"

    def test_max_lot_size_negative_rejected(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        r = admin_session.put(f"{API}/bot/config",
                              params={"account_id": acc_id},
                              json={"max_lot_size": -0.5}, timeout=10)
        # Audit C1: invalid values are rejected at the boundary, not clamped.
        assert r.status_code == 422

    def test_default_config_independent_from_account(self, admin_session, primary_account):
        # Override sets risk=low; default remains untouched.
        acc_id = primary_account["id"]
        admin_session.put(f"{API}/bot/config",
                          params={"account_id": acc_id},
                          json={"risk_level": "low", "max_lot_size": 0.05}, timeout=10)
        # default risk is set elsewhere — read & assert account_id is null
        default_cfg = admin_session.get(f"{API}/bot/config", timeout=10).json()
        assert default_cfg["account_id"] is None

    def test_list_configs_returns_both(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        # ensure both exist
        admin_session.get(f"{API}/bot/config", timeout=10)
        admin_session.get(f"{API}/bot/config",
                          params={"account_id": acc_id}, timeout=10)
        r = admin_session.get(f"{API}/bot/configs", timeout=10)
        assert r.status_code == 200
        cfgs = r.json()
        account_ids = {c["account_id"] for c in cfgs}
        assert None in account_ids        # default profile
        assert acc_id in account_ids      # per-account override

    def test_start_stop_is_scoped_per_account(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        # Stop default first to isolate
        admin_session.post(f"{API}/bot/stop", timeout=10)
        # Start ONLY the per-account bot
        r = admin_session.post(f"{API}/bot/start",
                               params={"account_id": acc_id}, timeout=10)
        if r.status_code == 409 and \
                (r.json().get("detail") or {}).get("code") == "activation_not_ready":
            # Environmental: the REAL admin account in preview may have a
            # stale heartbeat / pre-fencing EA. The gate itself is covered
            # by tests/test_iter145_ea_fencing.py.
            pytest.skip(f"live activation env not ready: {r.json()['detail']['problems']}")
        assert r.status_code == 200
        assert r.json()["active"] is True
        # default must still be stopped
        default_cfg = admin_session.get(f"{API}/bot/config", timeout=10).json()
        assert default_cfg["active"] is False
        # per-account must be running
        acc_cfg = admin_session.get(f"{API}/bot/config",
                                    params={"account_id": acc_id}, timeout=10).json()
        assert acc_cfg["active"] is True
        # cleanup
        admin_session.post(f"{API}/bot/stop", params={"account_id": acc_id}, timeout=10)

    def test_unknown_account_id_returns_404(self, admin_session):
        r = admin_session.get(f"{API}/bot/config",
                              params={"account_id": "000000000000000000000000"},
                              timeout=10)
        assert r.status_code == 404

    def test_delete_override_reverts_to_default(self, admin_session, primary_account):
        acc_id = primary_account["id"]
        # create the override
        admin_session.put(f"{API}/bot/config",
                          params={"account_id": acc_id},
                          json={"max_lot_size": 0.05}, timeout=10)
        r = admin_session.delete(f"{API}/bot/config",
                                 params={"account_id": acc_id}, timeout=10)
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        # Listing it back: now only default remains for that scope
        cfgs = admin_session.get(f"{API}/bot/configs", timeout=10).json()
        account_ids = {c["account_id"] for c in cfgs}
        assert acc_id not in account_ids


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
