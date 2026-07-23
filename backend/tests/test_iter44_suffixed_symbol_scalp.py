"""iter-44 · Verify broker-suffixed FX symbols (EURUSD#) now route correctly
into the scalp fast path.

Bug fix under test:
 * pip_utils.base_symbol('EURUSD#') → 'EURUSD' via new FX_MAJOR_BASES tuple
 * POST /api/bridge/ticks with symbol='EURUSD#' must be ACCEPTED (not ignored)
 * POST /api/bridge/candles with symbol='EURUSD#' must persist under base 'EURUSD'
 * /api/scalp/status runner keys use base 'EURUSD' with counters.ticks > 0
 * Control-plane permissions transition away from 'stale — fail closed'
 * /api/trades/live payloads include account_id
 * Regression: unapproved symbols (USDCAD) still ignored
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from helpers import base_url, mark_email_verified, mongo_db  # noqa: E402

API = f"{base_url()}/api"


# ---------------- unit-level check on pip_utils ----------------

def test_base_symbol_strips_common_broker_suffixes():
    from pip_utils import base_symbol
    assert base_symbol("EURUSD#") == "EURUSD"
    assert base_symbol("EURUSD.r") == "EURUSD"
    assert base_symbol("EURUSDm") == "EURUSD"
    assert base_symbol("EURUSD-ECN") == "EURUSD"
    assert base_symbol("GBPUSD#") == "GBPUSD"
    assert base_symbol("USDCAD.pro") == "USDCAD"
    # XAUUSD-family remains alias-resolved via PIP_SIZE prefix match
    assert base_symbol("XAUUSD.fx") == "XAUUSD"
    assert base_symbol("GOLD#") == "XAUUSD"
    # Unknown symbol falls through unchanged (uppercased)
    assert base_symbol("random_thing") == "RANDOM_THING"


# ---------------- integration fixtures ----------------

@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    email = f"TEST_iter44_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"terms_agreed": True, "email": email,
                     "password": "Vx7#Qm2pL9wTzK4e", "name": "iter44"},
               timeout=30)
    assert r.status_code == 200, r.text
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": "Vx7#Qm2pL9wTzK4e"}, timeout=30)
    assert r.status_code == 200, r.text

    r = s.post(f"{API}/accounts", json={
        "label": "TEST_iter44_acc", "broker": "TestBroker", "server": "T",
        "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
        "base_currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    acc = r.json()

    # enable shadow scalp on EURUSD
    r = s.post(f"{API}/scalp/config", json={
        "account_id": acc["id"], "symbol": "EURUSD",
        "enabled": True, "mode": "shadow"}, timeout=15)
    assert r.status_code == 200, r.text

    yield {"s": s, "email": email, "acc": acc}

    # teardown — kill scalp config + account + user (best effort)
    try:
        db = mongo_db()
        db.scalp_configs.delete_many({"account_id": acc["id"]})
        db.scalp_owners.delete_many({"account_id": acc["id"]})
        db.scalp_decisions.delete_many({"account_id": acc["id"]})
        db.intraday_candles.delete_many({"user_id": None})  # noop guard
        u = db.users.find_one({"email": email.lower()})
        if u:
            uid = str(u["_id"])
            db.intraday_candles.delete_many({"user_id": uid})
            db.accounts.delete_many({"user_id": uid})
            db.users.delete_one({"_id": u["_id"]})
    except Exception as e:  # noqa: BLE001
        print(f"teardown warning: {e}")


# ---------------- BUG VERIFICATION (main) ----------------

def _make_ticks(count: int = 12):
    """Impulse-pullback style stream: recent tm values, increasing."""
    now = int(time.time() * 1000)
    base = 1.08500
    ticks = []
    for i in range(count):
        mid = base + i * 0.00002
        ticks.append({"tm": now - (count - i) * 100,
                      "b": round(mid - 0.00003, 5),
                      "a": round(mid + 0.00003, 5)})
    return now, ticks


def test_ticks_suffixed_symbol_accepted(ctx):
    """POST /bridge/ticks with 'EURUSD#' must NOT be ignored — must return
    status='ok' and the runner counters.ticks must climb."""
    now, ticks = _make_ticks(20)
    r = requests.post(f"{API}/bridge/ticks", json={
        "bridge_token": ctx["acc"]["bridge_token"],
        "symbol": "EURUSD#",
        "sent_at_ms": now, "ticks": ticks}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", f"suffixed EURUSD# was rejected: {body}"
    assert body.get("ticks") == 20
    assert body.get("enabled") is True


def test_scalp_status_shows_base_symbol_runner_with_ticks(ctx):
    """Runner must be keyed to 'EURUSD' (not 'EURUSD#') and counters.ticks>0."""
    r = ctx["s"].get(f"{API}/scalp/status?account_id={ctx['acc']['id']}",
                     timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    runners = body.get("runners") or []
    assert runners, f"no runners returned: {body}"
    keys = [(rr.get("symbol"), rr.get("counters", {}).get("ticks", 0))
            for rr in runners]
    print(f"runner keys/ticks: {keys}")
    # There must be an EURUSD runner (base) — never EURUSD#
    assert any(rr.get("symbol") == "EURUSD" for rr in runners), \
        f"no EURUSD (base) runner: {keys}"
    assert not any(rr.get("symbol") == "EURUSD#" for rr in runners), \
        f"orphan EURUSD# runner exists: {keys}"
    tgt = next(rr for rr in runners if rr.get("symbol") == "EURUSD")
    assert tgt["counters"]["ticks"] > 0, f"tick counter did not advance: {tgt}"


def test_candles_suffixed_stored_under_base(ctx):
    """POST /bridge/candles with 'EURUSD#' → Mongo doc under symbol='EURUSD'."""
    # >=12 M15 bars, recent
    now_s = int(time.time())
    bar_step = 15 * 60
    bars = []
    price = 1.08500
    for i in range(24):
        t = now_s - (24 - i) * bar_step
        o = price
        h = price + 0.0004
        l = price - 0.0004  # noqa: E741
        c = price + 0.0002
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 100})
        price = c
    r = requests.post(f"{API}/bridge/candles", json={
        "bridge_token": ctx["acc"]["bridge_token"],
        "symbol": "EURUSD#", "timeframe": "M15", "bars": bars}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok"
    assert body.get("stored") >= 12

    # verify in Mongo — doc stored under base 'EURUSD', NOT 'EURUSD#'
    db = mongo_db()
    u = db.users.find_one({"email": ctx["email"].lower()})
    assert u
    uid = str(u["_id"])
    doc_base = db.intraday_candles.find_one({"user_id": uid, "symbol": "EURUSD",
                                             "timeframe": "M15"})
    doc_suffixed = db.intraday_candles.find_one({"user_id": uid, "symbol": "EURUSD#",
                                                 "timeframe": "M15"})
    assert doc_base is not None, "intraday_candles doc under 'EURUSD' missing"
    assert doc_suffixed is None, "orphan intraday_candles doc under 'EURUSD#' exists"
    assert doc_base.get("source_symbol") == "EURUSD#"
    assert len(doc_base.get("bars") or []) >= 12


def test_permissions_transition_away_from_stale(ctx):
    """After posting candles + fresh tick batches, /scalp/status permissions
    must not report the 'control-plane permissions stale — fail closed' string.
    A computed regime or a specific non-stale reason both prove the refresh
    pipeline is running on tick ingestion (bug was: it never ran because
    ticks were ignored for suffixed symbols)."""
    STALE = "control-plane permissions stale — fail closed"
    # push a few more fresh tick batches to trigger permission refresh
    for _ in range(3):
        now, ticks = _make_ticks(10)
        rr = requests.post(f"{API}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"],
            "symbol": "EURUSD#", "sent_at_ms": now, "ticks": ticks}, timeout=15)
        assert rr.status_code == 200 and rr.json().get("status") == "ok"
        time.sleep(0.4)

    deadline = time.time() + 12
    perms_seen = None
    reasons_seen = None
    while time.time() < deadline:
        r = ctx["s"].get(f"{API}/scalp/status?account_id={ctx['acc']['id']}",
                         timeout=15)
        assert r.status_code == 200
        runners = r.json().get("runners") or []
        eur = next((x for x in runners if x.get("symbol") == "EURUSD"), None)
        if eur:
            perms_seen = eur.get("permissions") or {}
            reasons_seen = perms_seen.get("reasons") or []
            if STALE not in reasons_seen:
                break
            regime = perms_seen.get("regime")
            if regime and regime != "UNKNOWN":
                break
        # kick another tick batch to speed up refresh
        now, ticks = _make_ticks(5)
        requests.post(f"{API}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"],
            "symbol": "EURUSD#", "sent_at_ms": now, "ticks": ticks}, timeout=15)
        time.sleep(1.0)

    print(f"final permissions: {perms_seen}")
    assert perms_seen is not None, "no permissions block observed"
    assert STALE not in (reasons_seen or []), \
        f"permissions still stale after 12s poll: {perms_seen}"
    # any of these constitute proof the refresh pipeline ran:
    # - non-UNKNOWN regime, OR an explicit non-stale reason string
    ok = perms_seen.get("regime") not in (None, "UNKNOWN") or bool(reasons_seen)
    assert ok, f"permissions block empty: {perms_seen}"


# ---------------- /api/trades/live account_id enrichment ----------------

@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=30)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text}")
    return s


def test_trades_live_includes_account_id(admin_session):
    r = admin_session.get(f"{API}/trades/live", timeout=20)
    assert r.status_code == 200, r.text
    trades = r.json()
    assert isinstance(trades, list)
    for t in trades:
        assert "account_id" in t, f"missing account_id in trade: {t}"
    print(f"admin /trades/live returned {len(trades)} open trades — all have account_id")


def test_trades_live_account_id_filter(admin_session):
    # first fetch any trade to get a valid account_id
    r = admin_session.get(f"{API}/trades/live", timeout=20)
    assert r.status_code == 200
    trades = r.json() or []
    if not trades:
        pytest.skip("admin has no open trades to filter")
    # pick the first non-null account_id
    aid = next((t.get("account_id") for t in trades if t.get("account_id")), None)
    if not aid:
        pytest.skip("no trade with account_id to filter by")
    r = admin_session.get(f"{API}/trades/live?account_id={aid}", timeout=20)
    assert r.status_code == 200
    filtered = r.json()
    assert all(t.get("account_id") == aid for t in filtered), \
        f"account_id filter leak: {[t.get('account_id') for t in filtered]}"


# ---------------- Regression: /scalp/status + unapproved symbol still ignored ----------------

def test_scalp_status_admin_regression(admin_session):
    r = admin_session.get(f"{API}/scalp/status", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "runners" in body and isinstance(body["runners"], list)


def test_unapproved_symbol_still_ignored(ctx):
    """USDCAD ticks must still be ignored (only EURUSD is approved). Regression
    guard so the base_symbol suffix fix did not accidentally widen the universe."""
    now = int(time.time() * 1000)
    r = requests.post(f"{API}/bridge/ticks", json={
        "bridge_token": ctx["acc"]["bridge_token"],
        "symbol": "USDCAD", "sent_at_ms": now,
        "ticks": [{"tm": now - 100, "b": 1.3500, "a": 1.3502}]}, timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ignored", f"USDCAD unexpectedly accepted: {body}"
    assert "not in approved scalp universe" in (body.get("reason") or "")


def test_unapproved_suffixed_symbol_still_ignored(ctx):
    """Also verify USDCAD# → base USDCAD → ignored (base_symbol resolves but
    universe still rejects)."""
    now = int(time.time() * 1000)
    r = requests.post(f"{API}/bridge/ticks", json={
        "bridge_token": ctx["acc"]["bridge_token"],
        "symbol": "USDCAD#", "sent_at_ms": now,
        "ticks": [{"tm": now - 100, "b": 1.3500, "a": 1.3502}]}, timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ignored", f"USDCAD# unexpectedly accepted: {body}"
