"""Smoke test for GET /api/accounts/{id}/test-connection."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _BACKEND_DIR)


def _arun(coro):
    return asyncio.run(coro)


VALID_OID = "656b1a2c3d4e5f6071829304"  # any 24-char hex


def _make_acc(mode="live", hb_age_seconds=None, balance=10000.0, spreads=None):
    """Build a fake Mongo account doc with a heartbeat at `hb_age_seconds` ago."""
    last_hb = None
    if hb_age_seconds is not None:
        last_hb = (datetime.now(timezone.utc) - timedelta(seconds=hb_age_seconds)).isoformat()
    return {
        "_id": VALID_OID, "user_id": "u1", "mode": mode,
        "balance": balance, "equity": balance,
        "last_heartbeat": last_hb,
        "current_spreads": spreads or {},
        "spreads_updated_at": last_hb,
    }


def _call(account_doc):
    """Invoke the test_connection route handler with a faked Mongo."""
    from routes.account_routes import test_connection

    db = MagicMock()
    db.accounts.find_one = AsyncMock(return_value=account_doc)
    with patch("routes.account_routes.get_db", return_value=db):
        return _arun(test_connection(VALID_OID, user={"id": "u1"}))


def test_connection_paper_always_ok():
    """Paper accounts are virtual — always reachable."""
    acc = _make_acc(mode="paper", hb_age_seconds=None)
    out = _call(acc)
    assert out["connected"] is True
    assert out["diagnostic"]["severity"] == "ok"
    assert "Paper" in out["diagnostic"]["message"]


def test_connection_live_fresh_heartbeat():
    """Heartbeat < 60s → ok / connected."""
    acc = _make_acc(hb_age_seconds=15, spreads={"XAUUSD": 1.8, "BTCUSD": 22.5})
    out = _call(acc)
    assert out["connected"] is True
    assert out["fresh"] is True
    assert out["diagnostic"]["severity"] == "ok"
    assert out["age_seconds"] < 60
    assert out["current_spreads"] == {"XAUUSD": 1.8, "BTCUSD": 22.5}


def test_connection_live_stale_heartbeat_warn():
    """Heartbeat 5min old → warn (stale)."""
    acc = _make_acc(hb_age_seconds=300)
    out = _call(acc)
    assert out["connected"] is False
    assert out["fresh"] is False
    assert out["diagnostic"]["severity"] == "warn"


def test_connection_live_dead_heartbeat_error():
    """Heartbeat > 10min → error (disconnected)."""
    acc = _make_acc(hb_age_seconds=3600)
    out = _call(acc)
    assert out["diagnostic"]["severity"] == "error"
    assert "disconnected" in out["diagnostic"]["message"].lower()


def test_connection_never_connected():
    """No heartbeat ever → error with 'never connected' messaging."""
    acc = _make_acc(hb_age_seconds=None)
    out = _call(acc)
    assert out["connected"] is False
    assert out["age_seconds"] is None
    assert out["diagnostic"]["severity"] == "error"
    assert "never connected" in out["diagnostic"]["message"].lower()


def test_connection_unknown_account_404():
    """Returns 404 when the account isn't owned by the user."""
    from fastapi import HTTPException
    from routes.account_routes import test_connection

    db = MagicMock()
    db.accounts.find_one = AsyncMock(return_value=None)
    with patch("routes.account_routes.get_db", return_value=db):
        try:
            _arun(test_connection(VALID_OID, user={"id": "u1"}))
            assert False, "expected HTTPException"
        except HTTPException as e:
            assert e.status_code == 404
