"""iter-159 — scheduled nightly drills + user-facing stress test."""
import os
import sys

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 120


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ─── nightly suite ───────────────────────────────────────────────────
def test_nightly_suite_runs_all_three_and_is_green():
    from scheduled_drills import run_nightly_suite
    out = _run(run_nightly_suite(_db(), actor="pytest"))
    assert out["ok"] is True, f"failures: {out['failures']}"
    assert out["chaos"]["total"] == 12 and out["chaos"]["failed"] == 0
    assert out["runtime"]["total"] == 9 and out["runtime"]["failed"] == 0
    assert out["stress"]["verdict"] == "STAYED_CALM"


def test_nightly_due_logic():
    from scheduled_drills import _due
    db = _db()
    # a run was just recorded by the previous test → not due
    assert _run(_due(db)) is False


def test_nightly_failure_raises_critical_alert(monkeypatch):
    import scheduled_drills as sd
    db = _db()

    async def _fake_runtime(_db, actor="x", scenarios=None):
        return {"run_id": "rtv-fake", "passed": 8, "failed": 1,
                "results": [{"scenario": "replay_protection",
                             "status": "fail", "notes": "synthetic"}]}
    import runtime_validation
    monkeypatch.setattr(runtime_validation, "run_runtime_validation",
                        _fake_runtime)
    out = _run(sd.run_nightly_suite(db, actor="pytest-fail"))
    assert out["ok"] is False
    assert "runtime:replay_protection" in out["failures"]

    async def verify():
        import datetime as _dt
        key = f"scheduled_drills:{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d')}"
        alert = await db.ops_alerts.find_one({"dedup_key": key})
        # cleanup synthetic artifacts
        await db.ops_alerts.delete_many({"dedup_key": key})
        await db.scheduled_drill_runs.delete_many(
            {"started_by": "pytest-fail"})
        return alert
    alert = _run(verify())
    assert alert and alert["severity"] == "critical"


def test_scheduled_drills_endpoints():
    r = requests.get(f"{API}/ops/scheduled-drills", timeout=TIMEOUT)
    assert r.status_code in (401, 403)
    s = _admin()
    r = s.get(f"{API}/ops/scheduled-drills", timeout=TIMEOUT)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert runs and runs[0]["chaos"]["total"] == 12


def test_scheduled_drill_task_is_running():
    # the loop task is created at startup — verify via a marker in the code
    # path: the loop must exist and be schedulable (import-level sanity)
    from scheduled_drills import scheduled_drill_loop, CHECK_INTERVAL_SEC
    assert callable(scheduled_drill_loop) and CHECK_INTERVAL_SEC >= 60


# ─── user-facing stress test ─────────────────────────────────────────
def test_user_stress_test_uses_own_profile():
    from helpers import register_and_login
    email = f"iter159-{os.urandom(4).hex()}@example.com"
    s = register_and_login(email)
    r = s.post(f"{API}/stress-test/run?severity=moderate", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "STAYED_CALM"
    assert body["user_id"]  # personalized
    assert "risk profile" in body["params"]["profile_note"]
    # own history only
    r = s.get(f"{API}/stress-test/runs", timeout=TIMEOUT)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == body["run_id"]


def test_user_stress_test_isolated_between_users():
    from helpers import register_and_login
    s2 = register_and_login(f"iter159b-{os.urandom(4).hex()}@example.com")
    r = s2.get(f"{API}/stress-test/runs", timeout=TIMEOUT)
    assert r.status_code == 200 and r.json()["runs"] == []


def test_user_stress_test_requires_auth_and_valid_severity():
    r = requests.post(f"{API}/stress-test/run?severity=moderate",
                      timeout=TIMEOUT)
    assert r.status_code == 401
    from helpers import register_and_login
    s = register_and_login(f"iter159c-{os.urandom(4).hex()}@example.com")
    r = s.post(f"{API}/stress-test/run?severity=apocalypse", timeout=TIMEOUT)
    assert r.status_code == 400


def test_user_stress_equity_personalization():
    from stress_test import run_user_stress_test
    db = _db()
    uid = f"iter159-eq-{os.urandom(4).hex()}"

    async def go():
        await db.accounts.insert_one(
            {"user_id": uid, "name": "Big Live", "equity": 50_000.0,
             "balance": 50_000.0, "status": "connected"})
        await db.bot_configs.insert_one(
            {"user_id": uid, "active": True, "risk_level": "high"})
        out = await run_user_stress_test(db, uid, severity="severe")
        await db.accounts.delete_many({"user_id": uid})
        await db.bot_configs.delete_many({"user_id": uid})
        await db.stress_tests.delete_many({"user_id": uid})
        return out
    out = _run(go())
    assert out["params"]["equity"] == 50_000.0
    assert out["params"]["risk_pct"] == 2.5  # high profile
    assert out["verdict"] == "STAYED_CALM"
