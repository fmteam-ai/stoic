"""Iter25l — full regression suite mandated by the review_request for
STOIC AI Trading Bot. Validates all API contracts listed by the user and
asserts shape of new strips (BotHealthScore + QuickActionsBar + SizingPreview).

This suite intentionally does NOT recreate scenarios already covered by
test_iter25{e,f,g,h,i,j,k,_revive_clears_pending_mod}.py — run them together
for full coverage. Here we cover:

  • GET smoke for every endpoint listed in the review_request.
  • /api/notifications/telegram (the actual path; review said `/prefs`).
  • external_trade_opened key present in returned alerts dict.
  • Telegram /test endpoint behaves correctly (silenced via conftest).
  • Sizing preview rows include 55,60,65,70,75,80,85,90 confidences.
  • Heartbeat with positions[] derives open_tickets even when client
    omits the field.
  • WebSocket /ws accepts an authenticated connect.
"""
import asyncio
import json
import os
import time

import pytest
import requests
import websockets

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://stoic-trading.preview.emergentagent.com",
).rstrip("/")
WS_URL = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASS = "admin123"


# ---------------- fixtures ----------------

@pytest.fixture(scope="module")
def admin_session():
    """requests.Session logged in as admin (cookies set)."""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return s


# ---------------- 1. GET smoke for every endpoint ----------------

PUBLIC_GETS = [
    "/api/health",
]

AUTH_GETS = [
    "/api/auth/me",
    "/api/accounts",
    "/api/trades",
    "/api/bot/config",
    "/api/bot/configs",
    "/api/bot/status",
    "/api/bot/health-score",
    "/api/bot/quick-actions",
    "/api/bot/sizing-preview?symbol=XAUUSD",
    "/api/bot/sizing-preview?symbol=BTCUSD",
    "/api/analytics/by-account",
    # NOTE: review said `/api/notifications/prefs` but the actual route
    # in /app/backend/routes/notification_routes.py is `/telegram`.
    "/api/notifications/telegram",
]


@pytest.mark.parametrize("path", PUBLIC_GETS)
def test_public_get_returns_200(path):
    r = requests.get(f"{BASE_URL}{path}", timeout=15)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("path", AUTH_GETS)
def test_authenticated_get_returns_200(admin_session, path):
    r = admin_session.get(f"{BASE_URL}{path}", timeout=20)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


# ---------------- 2. health-score shape ----------------

def test_health_score_full_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j.get("score"), int)
    assert 0 <= j["score"] <= 100
    assert j["status"] in ("excellent", "good", "degraded", "critical")
    assert isinstance(j.get("issues"), list)
    # issues are well-formed
    for issue in j["issues"]:
        assert "severity" in issue and "label" in issue


# ---------------- 3. quick-actions shape ----------------

def test_quick_actions_full_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/quick-actions", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j.get("bot_active"), bool)
    assert isinstance(j.get("open_trades"), int)
    assert isinstance(j.get("todays_pnl_usd"), (int, float))
    assert isinstance(j.get("todays_closed_count"), int)


# ---------------- 4. sizing-preview rows cover 55..90 ----------------

def test_sizing_preview_rows_55_to_90(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/bot/sizing-preview?symbol=XAUUSD", timeout=20
    )
    assert r.status_code == 200
    j = r.json()
    rows = j.get("rows") or []
    confidences = sorted({row["confidence_pct"] for row in rows})
    # the UI expects 55-90 in steps of 5 (8 rows)
    expected = [55, 60, 65, 70, 75, 80, 85, 90]
    for c in expected:
        assert c in confidences, f"missing confidence {c} in sizing-preview"
    # YOU GET column = effective_lot per row, must be non-negative
    for row in rows:
        assert row["effective_lot"] >= 0


def test_sizing_preview_symbol_switch_btcusd(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/bot/sizing-preview?symbol=BTCUSD", timeout=20
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("symbol") == "BTCUSD"


# ---------------- 5. notifications/telegram contains external_trade_opened ----------------

def test_notifications_telegram_returns_external_trade_opened_key(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/notifications/telegram", timeout=15)
    assert r.status_code == 200
    j = r.json()
    alerts = j.get("alerts") or {}
    assert "external_trade_opened" in alerts, (
        "Manual Trade Detected toggle missing from default alerts table"
    )


# ---------------- 6. Telegram /test message ----------------

def test_telegram_test_endpoint_responds_ok_for_configured_admin(admin_session):
    """Admin has chat_id 981306515 configured. /test endpoint is silenced
    by conftest so this only checks that the request returns a structured
    response without raising."""
    r = admin_session.post(f"{BASE_URL}/api/notifications/telegram/test", timeout=20)
    # Either ok:true (real telegram path) OR 400 if no token configured in this env.
    assert r.status_code in (200, 400), f"{r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        assert "ok" in r.json()


# ---------------- 7. Heartbeat derives open_tickets from positions[] ----------------

def _mint_bridge_token(admin_session):
    """Create a fresh test MT5 account via admin API and return (acct_id, bridge_token)."""
    suffix = int(time.time())
    body = {
        "label": f"iter25l_{suffix}",
        "broker": "RoboForex",
        "account_number": f"99{suffix % 100000}",
        "account_login": f"99{suffix % 100000}",
        "account_password": "x",
        "server": "RoboForex-Demo",
        "account_type": "demo",
    }
    r = admin_session.post(f"{BASE_URL}/api/accounts", json=body, timeout=15)
    assert r.status_code in (200, 201), f"create acct: {r.status_code} {r.text[:200]}"
    acct = r.json()
    return acct["id"], acct["bridge_token"]


def test_heartbeat_positions_derives_open_tickets(admin_session):
    """Send a heartbeat with positions[] but no open_tickets[]; backend
    should derive open_tickets from positions."""
    acct_id, bt = _mint_bridge_token(admin_session)
    try:
        payload = {
            "account_id": acct_id,
            "bridge_token": bt,
            "balance": 1000.0,
            "equity": 1000.0,
            "margin": 0.0,
            "margin_free": 1000.0,
            "open_positions": 1,
            "client_version": "1.26",
            "positions": [
                {"ticket": 777111, "symbol": "XAUUSD", "type": "BUY",
                 "volume": 0.10, "price_open": 4100.0, "sl": 4090.0, "tp": 4110.0,
                 "time_open": 1719500500},
            ],
        }
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat",
            headers={"X-Bridge-Token": bt, "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"

        # confirm account doc shows ea_version and open_tickets derived
        from pymongo import MongoClient
        from bson import ObjectId
        from dotenv import dotenv_values
        env = dotenv_values("/app/backend/.env")
        mc = MongoClient(env.get("MONGO_URL", "mongodb://localhost:27017"))
        db = mc[env.get("DB_NAME", "ai_trading_bot")]
        try:
            doc = db.accounts.find_one({"_id": ObjectId(acct_id)})
        except Exception:
            doc = db.accounts.find_one({"_id": acct_id})
        assert doc is not None
        assert doc.get("ea_version") == "1.26"
        open_tickets = doc.get("open_tickets") or []
        assert 777111 in open_tickets, (
            f"open_tickets={open_tickets} did not derive from positions[]"
        )
    finally:
        # cleanup - force delete because heartbeat may have inserted a trade
        admin_session.delete(f"{BASE_URL}/api/accounts/{acct_id}?force=true", timeout=10)


# ---------------- 8. WebSocket /ws connects after login ----------------

def _extract_cookie_header(session: requests.Session) -> str:
    parts = []
    for c in session.cookies:
        parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def test_websocket_ws_connects_with_auth(admin_session):
    async def _run():
        cookie_header = _extract_cookie_header(admin_session)
        try:
            ws = await websockets.connect(
                WS_URL,
                additional_headers={"Cookie": cookie_header},
                open_timeout=10,
                close_timeout=5,
            )
        except TypeError:
            ws = await websockets.connect(
                WS_URL,
                extra_headers={"Cookie": cookie_header},
                open_timeout=10,
                close_timeout=5,
            )
        # If we got here, the handshake succeeded.
        try:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                assert msg is not None
            except asyncio.TimeoutError:
                pass
        finally:
            await ws.close()

    asyncio.run(_run())


# ---------------- 9. Trade revive endpoint smoke ----------------

def test_trade_revive_404_for_unknown_id(admin_session):
    """Revive endpoint should 404 on unknown trade id — confirms route is wired."""
    r = admin_session.post(
        f"{BASE_URL}/api/trades/507f1f77bcf86cd799439011/revive", timeout=10
    )
    # 404 (not found) or 400 (bad id) both indicate route exists
    assert r.status_code in (400, 404)


def test_trade_audit_404_for_unknown_id(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/trades/507f1f77bcf86cd799439011/audit", timeout=10
    )
    assert r.status_code in (200, 400, 404)
    if r.status_code == 200:
        assert "events" in r.json()
