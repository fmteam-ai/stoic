"""Iter 24h — Heartbeat live-position snapshot backfill.

EA v1.25 sends `positions: [...]` in every heartbeat — full snapshot of
every currently-open MT5 position. The backend ingests this and auto-
creates STOIC trade records for any unknown ticket. Solves the
"I see 4 trades on MT5 but 0 in the bot" gap for pre-existing or
manually-opened positions.
"""
import os
import pathlib
import pytest
import requests

_FRONT_ENV = pathlib.Path("/app/frontend/.env")


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

TICKET_BASE = 99000000


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
    env_path = pathlib.Path("/app/backend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MONGO_URL="):
                _os.environ.setdefault("MONGO_URL", line.split("=", 1)[1].strip())
            if line.startswith("DB_NAME="):
                _os.environ.setdefault("DB_NAME", line.split("=", 1)[1].strip())
    client = pymongo.MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


@pytest.fixture(scope="module")
def live_account(admin_session, mongo_db):
    accs = admin_session.get(f"{API}/accounts", timeout=10).json()
    live = next((a for a in accs if a.get("mode") == "live"), None)
    if not live:
        pytest.skip("admin has no live account")
    from bson import ObjectId
    return mongo_db.accounts.find_one({"_id": ObjectId(live["id"])})


def _hb_with_positions(token: str, login: int, tickets: list[int], magic: int = 0):
    """Build a v1.25 heartbeat payload with synthetic positions."""
    return {
        "bridge_token": token,
        "balance": 10000.0, "equity": 10000.0, "open_positions": len(tickets),
        "account_login": login,
        "positions": [
            {"ticket": t, "symbol": "XAUUSD", "type": "BUY",
             "volume": 0.1, "price_open": 4050.0, "sl": 4030.0, "tp": 4080.0,
             "time_open": 1750800000 + i, "magic": magic, "profit": 0.0}
            for i, t in enumerate(tickets)
        ],
    }


def _cleanup(mongo_db, tickets):
    mongo_db.trades.delete_many({"mt5_ticket": {"$in": list(tickets)}})


class TestPositionSnapshotBackfill:
    def test_new_tickets_get_created(self, live_account, mongo_db):
        tickets = [TICKET_BASE + 1, TICKET_BASE + 2]
        configured = int(live_account["account_number"])
        try:
            r = requests.post(f"{API}/bridge/heartbeat",
                              json=_hb_with_positions(live_account["bridge_token"], configured, tickets, magic=0),
                              timeout=10)
            assert r.status_code == 200
            for t in tickets:
                doc = mongo_db.trades.find_one({"mt5_ticket": t})
                assert doc is not None
                assert doc["status"] == "open"
                assert doc["origin"] == "external"   # magic=0
                assert doc["backfilled_from_snapshot"] is True
        finally:
            _cleanup(mongo_db, tickets)

    def test_replay_is_idempotent(self, live_account, mongo_db):
        tickets = [TICKET_BASE + 3]
        configured = int(live_account["account_number"])
        try:
            payload = _hb_with_positions(live_account["bridge_token"], configured, tickets)
            requests.post(f"{API}/bridge/heartbeat", json=payload, timeout=10)
            requests.post(f"{API}/bridge/heartbeat", json=payload, timeout=10)
            requests.post(f"{API}/bridge/heartbeat", json=payload, timeout=10)
            assert mongo_db.trades.count_documents({"mt5_ticket": tickets[0]}) == 1
        finally:
            _cleanup(mongo_db, tickets)

    def test_skipped_on_terminal_mismatch(self, live_account, mongo_db):
        """When EA reports wrong login → don't ingest the positions
        (they belong to a different MT5 account)."""
        tickets = [TICKET_BASE + 4]
        bogus_login = int(live_account["account_number"]) + 999_999_999
        try:
            r = requests.post(f"{API}/bridge/heartbeat",
                              json=_hb_with_positions(live_account["bridge_token"], bogus_login, tickets),
                              timeout=10)
            assert r.status_code == 200
            assert mongo_db.trades.find_one({"mt5_ticket": tickets[0]}) is None
        finally:
            _cleanup(mongo_db, tickets)

    def test_legacy_heartbeat_without_positions_still_ok(self, live_account):
        """v1.24 EAs (no positions field) must continue working."""
        r = requests.post(f"{API}/bridge/heartbeat", json={
            "bridge_token": live_account["bridge_token"],
            "balance": 1000, "equity": 1000, "open_positions": 0,
            "account_login": int(live_account["account_number"]),
        }, timeout=10)
        assert r.status_code == 200

    def test_magic_0_external_others_auto(self, live_account, mongo_db):
        external = [TICKET_BASE + 5]
        bot = [TICKET_BASE + 6]
        configured = int(live_account["account_number"])
        try:
            # external (magic=0)
            requests.post(f"{API}/bridge/heartbeat",
                          json=_hb_with_positions(live_account["bridge_token"], configured, external, magic=0),
                          timeout=10)
            ext = mongo_db.trades.find_one({"mt5_ticket": external[0]})
            assert ext["origin"] == "external"
            assert ext["external_open"] is True
            # bot (magic != 0)
            requests.post(f"{API}/bridge/heartbeat",
                          json=_hb_with_positions(live_account["bridge_token"], configured, bot, magic=901234),
                          timeout=10)
            b = mongo_db.trades.find_one({"mt5_ticket": bot[0]})
            assert b["origin"] == "auto"
            assert b["external_open"] is False
        finally:
            _cleanup(mongo_db, external + bot)
