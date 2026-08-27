"""iter-183 — runtime watchdog & crash forensics."""
import os
import sys
import time

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def test_rss_reading_works():
    from runtime_watchdog import rss_mb
    assert rss_mb() > 10  # this pytest process certainly uses >10MB


def test_blockage_capture_on_stale_heartbeat():
    import runtime_watchdog as rw
    rw._heartbeat = time.monotonic() - (rw.BLOCK_THRESHOLD_SEC + 3)
    try:
        b = rw._check_blockage()
        assert b is not None
        assert b["blocked_for_s"] >= rw.BLOCK_THRESHOLD_SEC
        assert "stack" in b and len(b["stack"]) > 20
        assert rw._last_blockage is b
    finally:
        rw._heartbeat = time.monotonic()
        rw._last_blockage = None
    assert rw._check_blockage() is None  # fresh heartbeat → no blockage


def test_previous_run_archived_to_crash_log():
    import runtime_watchdog as rw
    db = _db()
    marker = f"iter183-{int(time.time())}"
    _run(db.runtime_health.update_one(
        {"_id": "current"},
        {"$set": {"pid": 424242, "boot_at": "2026-01-01T00:00:00+00:00",
                  "last_seen": marker, "rss_mb": 987.0, "max_rss_mb": 999.0,
                  "last_blockage": {"blocked_for_s": 9.9, "stack": "fake"}}},
        upsert=True))
    entry = _run(rw._record_previous_run(db))
    try:
        assert entry and entry["last_seen"] == marker
        assert entry["max_rss_mb"] == 999.0
        assert entry["last_blockage"]["blocked_for_s"] == 9.9
        doc = _run(db.runtime_crash_log.find_one({"last_seen": marker}))
        assert doc is not None
    finally:
        _run(db.runtime_crash_log.delete_many({"last_seen": marker}))


def test_runtime_stats_endpoint():
    r = requests.get(f"{API}/ops/runtime-stats", timeout=TIMEOUT)
    assert r.status_code == 403
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    r = s.get(f"{API}/ops/runtime-stats", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"pid", "uptime_s", "rss_mb", "max_rss_mb", "loop_lag_s",
            "restarts", "samples"} <= set(body)
    assert body["rss_mb"] > 50           # live server process
    assert body["loop_lag_s"] < 30       # watchdog heartbeating

    # metrics token path (used by scrapers / remote diagnosis)
    tok = os.environ["METRICS_TOKEN"]
    r = requests.get(f"{API}/ops/runtime-stats",
                     headers={"Authorization": f"Bearer {tok}"},
                     timeout=TIMEOUT)
    assert r.status_code == 200


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
