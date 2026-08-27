"""Iter 25h — Force Sync must NEVER wipe trades whose tickets are still
on the EA's open_tickets list, and the heartbeat must auto-revive any
closed-without-exit trade whose ticket the broker still reports as open.

Regression scenario:
  1. Bot opens 4 XAUUSD positions (real, on a v1.22+ EA reporting open_tickets)
  2. Slippage veto + reconciler aggressively close 2 of them in DB
  3. User clicks Force Sync wanting STOIC to align with broker
  4. The EA's NEXT heartbeat reports all 4 tickets STILL OPEN on broker
  5. STOIC must:
       a) NOT pass [] to reconcile_account — must use the real ticket list
       b) Auto-revive every closed trade whose ticket is still in open_tickets
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
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


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
def me(admin_session):
    return admin_session.get(f"{API}/auth/me", timeout=10).json()


@pytest.fixture
def fresh_account(mongo_db, me):
    acc = {
        "user_id": me["id"], "label": "TEST_iter25h",
        "broker": "RoboForex", "account_type": "demo",
        "account_number": "TESTH",
        "bridge_token": "iter25h-" + str(ObjectId()),
        "status": "connected", "balance": 1000, "equity": 1000,
        "mode": "live", "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = mongo_db.accounts.insert_one(acc)
    acc["_id"] = res.inserted_id
    yield acc
    mongo_db.accounts.delete_one({"_id": acc["_id"]})
    mongo_db.trades.delete_many({"account_id": str(acc["_id"])})


class TestForceSyncRespectsOpenTickets:
    def test_force_sync_does_not_wipe_trades_when_ea_reports_them_open(
        self, admin_session, mongo_db, me, fresh_account
    ):
        """Plant 4 open trades, set the EA's open_tickets to include them all,
        run Force Sync. None should be closed — the EA says they're all live.
        """
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        tickets = [700001001, 700001002, 700001003, 700001004]
        tids = []
        for tk in tickets:
            r = mongo_db.trades.insert_one({
                "user_id": me["id"], "account_id": str(fresh_account["_id"]),
                "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
                "entry_price": 4050.0, "stop_loss": 4070.0, "take_profit": 4020.0,
                "status": "open", "mt5_ticket": tk, "opened_at": now,
            })
            tids.append(r.inserted_id)
        mongo_db.accounts.update_one(
            {"_id": fresh_account["_id"]},
            {"$set": {"open_tickets": tickets, "open_positions": 4,
                      "last_heartbeat": now}},
        )

        # Trigger Force Sync via the public API the UI uses.
        r = admin_session.post(f"{API}/trades/reconcile?force=true", timeout=15)
        assert r.status_code == 200, r.text

        # All 4 must still be open
        for tid in tids:
            doc = mongo_db.trades.find_one({"_id": tid})
            assert doc["status"] == "open", (
                f"Force Sync wrongly closed ticket {doc['mt5_ticket']} "
                f"reason={doc.get('close_reason')}"
            )

    def test_force_sync_still_closes_genuinely_orphaned_trades(
        self, admin_session, mongo_db, me, fresh_account
    ):
        """Plant 2 trades. EA only reports 1 ticket open. Force Sync must
        close the orphan but leave the live one alone."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        live = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "open", "mt5_ticket": 700002001, "opened_at": now,
        }).inserted_id
        orphan = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.1,
            "entry_price": 4060.0, "stop_loss": 4080.0,
            "status": "open", "mt5_ticket": 700002002, "opened_at": now,
        }).inserted_id
        mongo_db.accounts.update_one(
            {"_id": fresh_account["_id"]},
            {"$set": {"open_tickets": [700002001], "open_positions": 1,
                       "last_heartbeat": now}},
        )

        r = admin_session.post(f"{API}/trades/reconcile?force=true", timeout=15)
        assert r.status_code == 200, r.text

        assert mongo_db.trades.find_one({"_id": live})["status"] == "open"
        assert mongo_db.trades.find_one({"_id": orphan})["status"] == "closed"


class TestHeartbeatTicketRevive:
    def test_heartbeat_revives_closed_trade_whose_ticket_is_still_open(
        self, mongo_db, fresh_account, me
    ):
        """A trade was prematurely closed (close_reason=slippage_veto). The
        EA's next heartbeat lists its ticket as STILL OPEN. STOIC must auto-
        revive without waiting for the user to click anything."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0, "take_profit": 4020.0,
            "status": "closed", "exit_price": None, "pnl": 0.0,
            "close_reason": "slippage_veto",
            "pending_modification": {"type": "FULL_CLOSE"},
            "mt5_ticket": 700003001, "opened_at": now, "closed_at": now,
        }).inserted_id

        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000, "open_positions": 1,
            "open_tickets": [700003001],
        }, timeout=10)
        assert r.status_code == 200, r.text

        doc = mongo_db.trades.find_one({"_id": tid})
        assert doc["status"] == "open", \
            f"heartbeat must auto-revive; got status={doc['status']}"
        assert doc.get("revived_via_open_tickets") is True
        assert doc.get("pending_modification") is None
        assert "close_reason" not in doc or not doc.get("close_reason")

    def test_heartbeat_does_not_revive_trade_with_exit_price(
        self, mongo_db, fresh_account, me
    ):
        """If exit_price is already set, the trade was *legitimately* closed
        by EA report — don't auto-revive even if the ticket appears in
        open_tickets again (covers EA replay scenarios)."""
        now = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()  # past iter-90 45s grace window
        tid = mongo_db.trades.insert_one({
            "user_id": me["id"], "account_id": str(fresh_account["_id"]),
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.2,
            "entry_price": 4050.0, "stop_loss": 4070.0,
            "status": "closed", "exit_price": 4045.0, "pnl": 100.0,
            "close_reason": "manual", "mt5_ticket": 700004001,
            "opened_at": now, "closed_at": now,
        }).inserted_id

        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": fresh_account["bridge_token"],
            "balance": 1000, "equity": 1000, "open_positions": 1,
            "open_tickets": [700004001],
        }, timeout=10)
        assert r.status_code == 200

        doc = mongo_db.trades.find_one({"_id": tid})
        assert doc["status"] == "closed", "must not revive a legitimately-closed trade"
        assert doc["exit_price"] == 4045.0


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
