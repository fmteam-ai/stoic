from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-53 · EOD flatten live regression — verifies the runtime wiring
(server startup task, bot_runner import, scalp engine import) is intact
and doesn't perturb live-endpoint behavior. NO writes to live trades.
Current UTC is outside every account's flatten window, so sweep-loop is
verified only via the presence of the task registration in server.py
(compile-time check) — DO NOT invoke sweep_eod_flatten on live db here.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import re
import subprocess

import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()
pass  # ADMIN_EMAIL comes from live_target
ADMIN_PASS = ADMIN_PASSWORD


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text[:200]}"
    return s


# --- Sanity: health + pulse (bot_runner import path exercised) --------
def test_health_ok(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/health", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok" or "status" in r.json()


def test_bot_pulse_returns_items(admin_session):
    """bot_runner imports eod_flatten at the top of its veto path — if the
    import broke, /api/bot/pulse would 500. It returns items structure."""
    r = admin_session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


# --- Scalp status: engine imports eod_flatten module cleanly ----------
def test_scalp_status_current_time_outside_window(admin_session):
    """Current UTC ~15:xx — outside every account's flatten window (23:15
    broker time). Enabled EURUSD runner must have block_reasons==[] and
    health OK. This proves scalp/engine.py imports eod_flatten cleanly and
    that _eod_flatten_block does NOT veto outside the window."""
    r = admin_session.get(f"{BASE_URL}/api/scalp/status", timeout=15)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    runners = body.get("runners") or body.get("data", {}).get("runners") or []
    if not runners:
        pytest.skip("no runners returned by /api/scalp/status")
    enabled = [x for x in runners
               if x.get("enabled") and (x.get("symbol") or "").upper()
               .startswith("EURUSD")]
    if not enabled:
        pytest.skip("no enabled EURUSD runner in status payload")
    for x in enabled:
        assert x.get("block_reasons") == [], (
            f"runner has unexpected block_reasons "
            f"outside flatten window: {x.get('block_reasons')} "
            f"symbol={x.get('symbol')} account={x.get('account_id')}")
        # eod_flatten reason string must NOT appear
        reasons_blob = str(x.get("block_reasons") or "")
        assert "EOD flatten" not in reasons_blob


# --- Startup wiring compile-time proofs -------------------------------
def test_server_registers_eod_flatten_task():
    # iter-62: the loop body moved to background_loops.py (worker separation)
    src = open(_os.path.join(_BACKEND_DIR, "server.py")).read()
    assert "_eod_flatten_task = asyncio.create_task(_eod_flatten_loop())" \
        in src
    loops = open(_os.path.join(_BACKEND_DIR, "background_loops.py")).read()
    assert "async def _eod_flatten_loop" in loops
    assert "from eod_flatten import sweep_eod_flatten" in loops


def test_bot_runner_has_combined_veto():
    src = open(_os.path.join(_BACKEND_DIR, "bot_runner.py")).read()
    assert "from eod_flatten import eod_flatten_block" in src
    # both quiet and flatten blocks chained
    assert re.search(r"eod_quiet_block\([^)]*\)\s*\n\s*or\s+eod_flatten_block",
                     src), "bot_runner should combine eod_quiet_block OR eod_flatten_block"


def test_scalp_engine_imports_and_chains_flatten_block():
    src = open(_os.path.join(_BACKEND_DIR, "scalp/engine.py")).read()
    assert "from eod_flatten import eod_flatten_block as _eod_flatten_block" \
        in src
    assert "_eod_flatten_block(self.account)" in src


# --- Env toggle & window bounds are what spec requires ----------------
def test_flatten_window_bounds_match_ea_quiet():
    """FLATTEN_END_MIN must equal QUIET_START_MIN so we finish before the
    EA v1.41 quiet window begins."""
    from eod_flatten import FLATTEN_END_MIN, FLATTEN_START_MIN
    from eod_quiet import QUIET_START_MIN
    assert FLATTEN_END_MIN == QUIET_START_MIN
    assert FLATTEN_START_MIN == QUIET_START_MIN - 25  # default 25min lead


def test_env_disable_kills_block_and_sweep(monkeypatch):
    """EOD_FLATTEN_ENABLED=false disables the veto AND the sweep."""
    import asyncio
    from datetime import datetime, timezone
    from eod_flatten import eod_flatten_block, sweep_eod_flatten
    monkeypatch.setenv("EOD_FLATTEN_ENABLED", "false")
    now = datetime(2026, 1, 1, 23, 20, tzinfo=timezone.utc)
    assert eod_flatten_block({"broker_utc_offset_sec": 0}, now) is None
    # sweep short-circuits without ever touching db
    out = asyncio.run(sweep_eod_flatten(object(), now=now))
    assert out == {"enabled": False, "queued": 0, "accounts": 0}


def test_no_eod_flatten_tracebacks_in_recent_logs():
    """Recent backend logs must contain zero eod_flatten tracebacks."""
    out = subprocess.check_output(
        ["tail", "-n", "2000", "/var/log/supervisor/backend.err.log"],
        text=True)
    # Count tracebacks that reference eod_flatten anywhere in a 20-line window
    lines = out.splitlines()
    hits = 0
    for i, ln in enumerate(lines):
        if "Traceback" in ln:
            window = "\n".join(lines[i:i + 20])
            if "eod_flatten" in window:
                hits += 1
    assert hits == 0, f"found {hits} eod_flatten tracebacks in recent logs"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
