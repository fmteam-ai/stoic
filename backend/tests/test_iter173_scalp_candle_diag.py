"""iter-173 — scalp regime warm-up warning now self-diagnoses the candle
feed (uses candle_feed_health instead of a generic 'wait for warm-up')."""
import os
import sys
from datetime import datetime, timedelta, timezone

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


_CONFTEST = None


def _conftest_mod():
    """Root tests/conftest.py (shared loop), resolved safely even in
    mixed-directory runs where the bare `conftest` module name collides
    with tests/integration/conftest.py."""
    global _CONFTEST
    if _CONFTEST is not None:
        return _CONFTEST
    import importlib.util
    root_path = os.path.join(_BACKEND_DIR, "tests", "conftest.py")
    for mod in list(sys.modules.values()):
        if (getattr(mod, "__file__", None) == root_path
                and hasattr(mod, "run_async")):
            _CONFTEST = mod
            return mod
    spec = importlib.util.spec_from_file_location(
        "_root_tests_conftest", root_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CONFTEST = mod
    return mod


def _run(coro):
    return _conftest_mod().run_async(coro)


def _db():
    import asyncio
    import database
    mod = _conftest_mod()
    asyncio.set_event_loop(mod._shared_loop())
    database._client = None   # rebind to the shared loop (mixed-run safety)
    database._db = None
    from database import get_db
    return get_db()


def _uid():
    return f"iter173-{os.urandom(4).hex()}"


def _diag(db, uid, have=0):
    from scalp.permissions import _candle_feed_diagnosis
    return _run(_candle_feed_diagnosis(db, uid, "EURUSD", have))


def test_no_payloads_ever_received():
    db = _db()
    uid = _uid()
    msg = _diag(db, uid)
    assert "no candle payloads have reached STOIC" in msg
    assert "M15 chart" in msg  # actionable: forces history download


def test_payloads_arriving_but_short_history():
    db = _db()
    uid = _uid()
    now = datetime.now(timezone.utc)
    _run(db.candle_feed_health.insert_one({
        "user_id": uid, "symbol": "EURUSD", "timeframe": "M15",
        "last_received_at": now.isoformat(), "valid_bars": 8,
        "last_error": None}))
    try:
        msg = _diag(db, uid, have=8)
        assert "candle payloads are arriving" in msg and "8/12" in msg
    finally:
        _run(db.candle_feed_health.delete_many({"user_id": uid}))


def test_feed_stopped():
    db = _db()
    uid = _uid()
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    _run(db.candle_feed_health.insert_one({
        "user_id": uid, "symbol": "EURUSD", "timeframe": "M15",
        "last_received_at": old.isoformat(), "valid_bars": 96,
        "last_error": None}))
    try:
        msg = _diag(db, uid)
        assert "then stopped" in msg and "45 min" in msg
    finally:
        _run(db.candle_feed_health.delete_many({"user_id": uid}))


def test_rejected_payloads_surfaced():
    db = _db()
    uid = _uid()
    _run(db.candle_feed_health.insert_one({
        "user_id": uid, "symbol": "EURUSD", "timeframe": "M15",
        "last_received_at": datetime.now(timezone.utc).isoformat(),
        "valid_bars": 0, "last_error": "all bars invalid"}))
    try:
        msg = _diag(db, uid)
        assert "rejected" in msg and "all bars invalid" in msg
    finally:
        _run(db.candle_feed_health.delete_many({"user_id": uid}))


def test_compute_uses_diagnosis():
    from scalp.instruments import approved
    from scalp.permissions import _compute
    db = _db()
    uid = _uid()
    cfg = approved("EURUSD")
    perms = _run(_compute(db, uid, "EURUSD", cfg))
    assert perms["regime"] == "UNKNOWN"
    assert "no candle payloads have reached STOIC" in perms["regime_reason"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
