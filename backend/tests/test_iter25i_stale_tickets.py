from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter 25i — Detect stale `open_tickets` from buggy EAs.

Some EA builds (pre-v1.26) cache the tickets list and never purge entries
after MT5 closes the position. The heartbeat then arrives with
`open_positions=0` but `open_tickets=[4 dead ones]`. STOIC's auto-revive
would loop-revive the dead trades on every heartbeat — exactly the bug
the user hit on June 24 with tickets 696559793 / 696559939 / 696561199 /
696561738 (all TP-hit, MT5 history confirmed closed, EA still emitting
them as open).

`open_positions` comes from MT5's authoritative `PositionsTotal()`. When
the count disagrees with the tickets list (count smaller), STOIC must
treat the tickets list as stale and reconcile against the count.
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
from datetime import datetime, timedelta, timezone
from bson import ObjectId

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
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


def _strip(v: str) -> str:
    return v.strip().strip('"').strip("'")


@pytest.fixture(scope="module")
def mongo_db():
    import os as _os, pymongo
    env_path = pathlib.Path(_os.path.join(_BACKEND_DIR, ".env"))
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MONGO_URL="):
                _os.environ.setdefault("MONGO_URL", _strip(line.split("=", 1)[1]))
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", _strip(line.split("=", 1)[1]))
    client = pymongo.MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200
    return s


@pytest.fixture(scope="module")
def me(admin_session):
    return admin_session.get(f"{API}/auth/me", timeout=10).json()


@pytest.fixture
def fresh_account(mongo_db, me):
    acc = {
        "user_id": me["id"], "label": "TEST_iter25i",
        "broker": "RoboForex", "account_type": "demo",
        "account_number": "TESTI",
        "bridge_token": "iter25i-" + str(ObjectId()),
        "status": "connected", "balance": 1000, "equity": 1000,
        "mode": "live", "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = mongo_db.accounts.insert_one(acc)
    acc["_id"] = res.inserted_id
    yield acc
    mongo_db.accounts.delete_one({"_id": acc["_id"]})
    mongo_db.trades.delete_many({"account_id": str(acc["_id"])})


class TestStaleTicketsDetection:
    def test_heartbeat_treats_tickets_as_empty_when_positions_disagree(
        self, mongo_db, fresh_account, me
    ):
        """positions=0 but tickets=[4 ghost ones] → STOIC must close all 4
        and NOT loop-revive them."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        tickets = [700009001, 700009002, 700009003, 700009004]
        tids = []
        for tk in tickets:
            r = mongo_db.trades.insert_one({
                "user_id": me["id"], "account_id": str(fresh_account["_id"]),
                "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
                "entry_price": 4050.0, "stop_loss": 4070.0,
                "status": "open", "mt5_ticket": tk, "opened_at": now,
            })
            tids.append(r.inserted_id)

        # Buggy heartbeat: count=0 but tickets present
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000,
            "open_positions": 0,
            "open_tickets": tickets,
        }, timeout=10)
        assert r.status_code == 200, r.text

        # All 4 must now be closed
        for tid in tids:
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc["status"] == "closed", (
                f"stale-tickets guard failed: ticket {doc['mt5_ticket']} still open"
            )

    def test_heartbeat_does_not_revive_already_closed_trades_on_stale_tickets(
        self, mongo_db, fresh_account, me
    ):
        """A closed-without-exit trade whose ticket is in a STALE list must
        NOT be revived — this was the loop bug."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "closed", "exit_price": None, "pnl": 0.0,
            "close_reason": "slippage_veto",
            "mt5_ticket": 700010001, "opened_at": now, "closed_at": now,
        }).inserted_id

        # EA sends stale heartbeat with positions=0 but this ticket included
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000,
            "open_positions": 0,
            "open_tickets": [700010001],
        }, timeout=10)
        assert r.status_code == 200

        doc = mongo_db.trades.find_one({"_id": tid})
        # Must still be closed (no revive loop)
        assert doc["status"] == "closed"
        assert doc.get("revived_via_open_tickets") is not True

    def test_consistent_heartbeat_still_revives_and_reconciles_normally(
        self, mongo_db, fresh_account, me
    ):
        """When positions and tickets agree, the normal revive + reconcile
        flow runs unaffected by the new guard."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        # 1 ghost (broker has it open, STOIC closed it)
        ghost = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "closed", "exit_price": None, "pnl": 0.0,
            "close_reason": "slippage_veto",
            "mt5_ticket": 700011001, "opened_at": now, "closed_at": now,
        }).inserted_id
        # 1 genuine orphan (STOIC open, broker has nothing)
        orphan = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "open", "mt5_ticket": 700011002, "opened_at": now,
        }).inserted_id

        # EA: positions=1, tickets=[700011001] — consistent
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000,
            "open_positions": 1,
            "open_tickets": [700011001],
        }, timeout=10)
        assert r.status_code == 200

        # Ghost revived (broker has it open) — orphan closed (broker doesn't)
        assert mongo_db.trades.find_one({"_id": ghost})["status"] == "open"
        assert mongo_db.trades.find_one({"_id": orphan})["status"] == "closed"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
