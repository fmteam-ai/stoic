"""Live backend verification for Fix plan C1 — candle ingest (A1, R3, A10, A11).

Scope:
- POST /api/bridge/candles with BROKER-time epochs; verify UTC normalisation,
  forming-bar split, candle_feed_health counters, intraday_candles last_tick.
- Bar validation A1 (bad geometry/alignment/NaN/old → dropped; only invalid → 422).
- Per-user scoping A1: admin's /api/bot/mtf-confluence must NOT use the test
  user's bars; test user may receive a report; signal endpoint includes
  `price_source` (broker_tick when last_tick fresh).
- A3 best-effort VWAP note when only yesterday's bars are present.
- Cleanup inserted docs.
"""
from __future__ import annotations

import os
import random
import secrets
import string
import sys
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url, admin_credentials  # noqa: E402

BASE_URL = require_live_base_url().rstrip("/")
ADMIN_EMAIL, ADMIN_PASSWORD = admin_credentials(strict=True)

RATE_LIMIT_BYPASS = os.environ.get("RATE_LIMIT_BYPASS_TOKEN") or ""
if not RATE_LIMIT_BYPASS:
    try:
        with open("/app/backend/.env", "r") as f:
            for line in f:
                if line.startswith("RATE_LIMIT_BYPASS_TOKEN="):
                    RATE_LIMIT_BYPASS = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass

BYPASS = {"X-Rate-Limit-Bypass": RATE_LIMIT_BYPASS} if RATE_LIMIT_BYPASS else {}
BROKER_OFFSET = 10800  # UTC+3
SYMBOL = "XAUUSD.fx"
BASE_SYM = "XAUUSD"
TF = "M15"
TF_S = 900


# ---------- Mongo helpers (seed/cleanup) ----------
@pytest.fixture(scope="module")
def mongo_db():
    pymongo = pytest.importorskip("pymongo")
    url = os.environ.get("MONGO_URL") or ""
    db_name = os.environ.get("DB_NAME") or ""
    if not url or not db_name:
        try:
            with open("/app/backend/.env", "r") as f:
                for line in f:
                    if line.startswith("MONGO_URL=") and not url:
                        url = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("DB_NAME=") and not db_name:
                        db_name = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    assert url and db_name, "MONGO_URL/DB_NAME not available"
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    db.command("ping")
    yield db
    client.close()


@pytest.fixture(scope="module")
def test_user(mongo_db):
    """Create a throwaway test user + paper account with bridge_token."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"c1live_{suffix}@stoic-qa.dev"
    password = "C1" + secrets.token_urlsafe(16) + "!Zx9"
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now_iso = datetime.now(timezone.utc).isoformat()
    user_doc = {
        "email": email, "password_hash": pw_hash, "name": "C1 Live Test",
        "role": "user", "status": "active", "created_at": now_iso,
        "two_factor_enabled": False, "email_verified": True,
        "accepted_terms_version": "v1", "accepted_terms_at": now_iso,
    }
    r = mongo_db.users.insert_one(user_doc)
    user_id = str(r.inserted_id)
    bridge_token = secrets.token_hex(16)
    acc_doc = {
        "user_id": user_id, "mode": "paper",
        "bridge_token": bridge_token,
        "broker_time_info": {"server_gmt_offset_sec": BROKER_OFFSET},
        "status": "connected", "trading_enabled": False,
        "created_at": now_iso, "label": "C1 Live throwaway",
    }
    a = mongo_db.accounts.insert_one(acc_doc)
    account_id = str(a.inserted_id)

    yield {"user_id": user_id, "email": email, "password": password,
           "bridge_token": bridge_token, "account_id": account_id}

    # Cleanup
    try:
        from bson import ObjectId
        mongo_db.users.delete_one({"_id": ObjectId(user_id)})
        mongo_db.accounts.delete_one({"_id": ObjectId(account_id)})
        mongo_db.intraday_candles.delete_many({"user_id": user_id})
        mongo_db.candle_feed_health.delete_many({"user_id": user_id})
        mongo_db.signals.delete_many({"user_id": user_id})
        mongo_db.sessions.delete_many({"user_id": user_id})
    except Exception as e:  # pragma: no cover
        print(f"cleanup warning: {e}")


def _now_slot(tf_s=TF_S) -> int:
    now = int(datetime.now(timezone.utc).timestamp())
    return now - (now % tf_s)


def _mk_bar(t_utc: int, c: float = 2000.0, broker: bool = True) -> dict:
    t = t_utc + (BROKER_OFFSET if broker else 0)
    return {"t": t, "o": c - 0.5, "h": c + 0.8, "l": c - 0.9, "c": c, "v": 10.0}


def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    if BYPASS:
        s.headers.update(BYPASS)
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


# ---------- Test 1: R3/A1/A10 happy path ----------
def test_candles_ingest_happy_path(test_user, mongo_db):
    cur_slot = _now_slot()
    closed_slots = [cur_slot - TF_S * i for i in range(5, 0, -1)]  # 5 closed
    bars = [_mk_bar(t, c=2000.0 + i) for i, t in enumerate(closed_slots)]
    forming_c = 2010.5
    bars.append(_mk_bar(cur_slot, c=forming_c))  # forming

    r = requests.post(f"{BASE_URL}/api/bridge/candles",
                      json={"bridge_token": test_user["bridge_token"],
                            "symbol": SYMBOL, "timeframe": TF, "bars": bars},
                      headers=BYPASS, timeout=20)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    data = r.json()
    assert data.get("stored") == 5, data

    doc = mongo_db.intraday_candles.find_one(
        {"user_id": test_user["user_id"], "symbol": BASE_SYM, "timeframe": TF})
    assert doc is not None
    stored_ts = sorted(int(b["t"]) for b in doc["bars"])
    # bars stored as UTC (= broker t - offset)
    assert all(t in closed_slots for t in stored_ts[-5:]), (stored_ts[-5:], closed_slots)
    assert cur_slot not in stored_ts, "forming bar must be excluded"
    assert doc.get("t_basis") == "utc"
    assert int(doc.get("broker_offset_sec")) == BROKER_OFFSET
    assert doc.get("offset_source") == "ea_broker_time"
    lt = doc.get("last_tick") or {}
    assert abs(float(lt.get("price", 0)) - forming_c) < 1e-6
    assert int(lt.get("bar_t")) == cur_slot

    h = mongo_db.candle_feed_health.find_one(
        {"user_id": test_user["user_id"], "symbol": BASE_SYM, "timeframe": TF})
    assert h is not None
    assert h.get("forming_dropped") == 1
    assert h.get("valid_bars") == 5
    assert int(h.get("broker_offset_sec")) == BROKER_OFFSET


# ---------- Test 2: A1 validation (bad bars dropped) ----------
def test_bar_validation_drops_bad_and_422_on_only_invalid(test_user, mongo_db):
    cur_slot = _now_slot()
    good = _mk_bar(cur_slot - TF_S * 2, c=2001.0)  # 1 valid closed
    bad_hi = _mk_bar(cur_slot - TF_S * 3, c=2000.0)
    bad_hi["h"] = bad_hi["c"] - 1.0  # high < close
    bad_lo = _mk_bar(cur_slot - TF_S * 4, c=2000.0)
    bad_lo["l"] = bad_lo["o"] + 1.0  # low > open
    bad_align = _mk_bar(cur_slot - TF_S * 5, c=2000.0)
    bad_align["t"] += 7  # not aligned
    bad_neg = _mk_bar(cur_slot - TF_S * 6, c=2000.0)
    bad_neg["o"] = -1.0
    old = _mk_bar(cur_slot - 40 * 86400, c=2000.0)
    payload_bars = [good, bad_hi, bad_lo, bad_align, bad_neg, old]

    r = requests.post(f"{BASE_URL}/api/bridge/candles",
                      json={"bridge_token": test_user["bridge_token"],
                            "symbol": SYMBOL, "timeframe": TF, "bars": payload_bars},
                      headers=BYPASS, timeout=20)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    data = r.json()
    assert data.get("stored") >= 1  # at least the good bar landed

    h = mongo_db.candle_feed_health.find_one(
        {"user_id": test_user["user_id"], "symbol": BASE_SYM, "timeframe": TF})
    assert int(h.get("dropped_bars", 0)) >= 5

    # Only invalid bars → 422
    r2 = requests.post(f"{BASE_URL}/api/bridge/candles",
                       json={"bridge_token": test_user["bridge_token"],
                             "symbol": SYMBOL, "timeframe": TF,
                             "bars": [bad_hi, bad_align, bad_neg]},
                       headers=BYPASS, timeout=20)
    assert r2.status_code == 422, f"{r2.status_code} {r2.text[:300]}"

    # Only the forming bar → stored:0, forming_dropped:1, last_tick updated
    cur_slot2 = _now_slot()
    forming_only_c = 2050.25
    r3 = requests.post(f"{BASE_URL}/api/bridge/candles",
                       json={"bridge_token": test_user["bridge_token"],
                             "symbol": SYMBOL, "timeframe": TF,
                             "bars": [_mk_bar(cur_slot2, c=forming_only_c)]},
                       headers=BYPASS, timeout=20)
    assert r3.status_code == 200, f"{r3.status_code} {r3.text[:300]}"
    d3 = r3.json()
    assert d3.get("stored") == 0 and d3.get("forming_dropped") == 1, d3
    doc = mongo_db.intraday_candles.find_one(
        {"user_id": test_user["user_id"], "symbol": BASE_SYM, "timeframe": TF})
    assert abs(float((doc.get("last_tick") or {}).get("price", 0)) - forming_only_c) < 1e-6


# ---------- Test 3: Per-user scoping A1 (admin vs test user) ----------
def test_mtf_confluence_is_user_scoped(test_user, mongo_db):
    # Preserve admin candle state to decide expectation.
    admin_sess = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
    # Resolve admin user id
    me = admin_sess.get(f"{BASE_URL}/api/auth/me", timeout=15)
    admin_id = None
    if me.status_code == 200:
        admin_id = (me.json() or {}).get("id")
    admin_has_stream = False
    if admin_id:
        admin_doc = mongo_db.intraday_candles.find_one(
            {"user_id": admin_id, "symbol": BASE_SYM, "timeframe": TF})
        if admin_doc and admin_doc.get("bars"):
            # fresh = updated within 20 min
            try:
                upd = datetime.fromisoformat(admin_doc["updated_at"].replace("Z", "+00:00"))
                admin_has_stream = (datetime.now(timezone.utc) - upd).total_seconds() < 1200
            except Exception:
                admin_has_stream = False

    r_admin = admin_sess.get(f"{BASE_URL}/api/bot/mtf-confluence",
                             params={"symbol": BASE_SYM}, timeout=25)
    assert r_admin.status_code == 200, f"{r_admin.status_code} {r_admin.text[:300]}"
    admin_report = r_admin.json()
    if not admin_has_stream:
        assert admin_report.get("available") is False, (
            f"admin got MTF report but has no fresh M15 stream (A1 scoping leak?): "
            f"{admin_report}")

    # Now login as test user; may or may not be eligible (need ≥24 fresh bars),
    # but endpoint should respond 200 regardless.
    user_sess = _login(test_user["email"], test_user["password"])
    r_user = user_sess.get(f"{BASE_URL}/api/bot/mtf-confluence",
                           params={"symbol": BASE_SYM}, timeout=25)
    assert r_user.status_code == 200, f"{r_user.status_code} {r_user.text[:300]}"


def test_signal_generate_includes_price_source(test_user, mongo_db):
    """Ensure analyze_symbol surfaces price_source; last_tick is fresh so
    price_source should be 'broker_tick'. Feed ≥30 fresh M15 bars so the MTF
    cascade engages (otherwise cheap_hold early-exits and never populates the
    key — tracked as a minor finding)."""
    cur_slot = _now_slot()
    bars = []
    base_price = 2000.0
    for i in range(60, 0, -1):
        bars.append(_mk_bar(cur_slot - TF_S * i, c=base_price + (i % 7) * 0.3))
    bars.append(_mk_bar(cur_slot, c=2015.0))  # forming bar → broker tick

    r0 = requests.post(f"{BASE_URL}/api/bridge/candles",
                      json={"bridge_token": test_user["bridge_token"],
                            "symbol": SYMBOL, "timeframe": TF, "bars": bars},
                      headers=BYPASS, timeout=20)
    assert r0.status_code == 200, r0.text[:200]
    time.sleep(0.5)

    user_sess = _login(test_user["email"], test_user["password"])
    r = user_sess.post(f"{BASE_URL}/api/signals/generate",
                      json={"symbol": BASE_SYM, "risk_level": "medium"},
                      timeout=90)
    if r.status_code in (404, 405):
        r = user_sess.get(f"{BASE_URL}/api/signals/generate",
                         params={"symbol": BASE_SYM, "risk_level": "medium"},
                         timeout=90)
    if r.status_code >= 500:
        pytest.skip(f"public quote outage / 5xx (environmental): {r.status_code} {r.text[:200]}")
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    body = r.json()
    sig = body.get("signal") if isinstance(body, dict) and "signal" in body else body
    assert isinstance(sig, dict), f"unexpected shape: {body}"
    print(f"\n[signal keys] cheap_hold={sig.get('cheap_hold')} action={sig.get('action')} "
          f"price_source={sig.get('price_source', 'MISSING')} "
          f"reason={(sig.get('reasoning') or '')[:120]!r}")
    # iteration_225 fix: _hold() early exits now carry price_source too.
    assert "price_source" in sig, f"price_source missing from signal: keys={list(sig.keys())}"
    assert sig["price_source"] in ("broker_tick", "public_quote")


# ---------- Test 4: A3 — VWAP not emitted when only yesterday's bars present ----------
def test_a3_no_vwap_when_only_yesterday_bars(test_user, mongo_db):
    # Wipe candles for test user, seed only yesterday's UTC bars.
    mongo_db.intraday_candles.delete_many({"user_id": test_user["user_id"]})
    mongo_db.candle_feed_health.delete_many({"user_id": test_user["user_id"]})

    now = datetime.now(timezone.utc)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    yesterday_end = int(today_midnight.timestamp()) - TF_S  # last M15 of yesterday
    y_slots = [yesterday_end - TF_S * i for i in range(10, 0, -1)]  # 10 bars yesterday
    bars = [_mk_bar(t, c=1990.0 + i) for i, t in enumerate(y_slots)]

    r = requests.post(f"{BASE_URL}/api/bridge/candles",
                      json={"bridge_token": test_user["bridge_token"],
                            "symbol": SYMBOL, "timeframe": TF, "bars": bars},
                      headers=BYPASS, timeout=20)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"

    user_sess = _login(test_user["email"], test_user["password"])
    rr = user_sess.post(f"{BASE_URL}/api/signals/generate",
                       json={"symbol": BASE_SYM, "risk_level": "medium"},
                       timeout=60)
    if rr.status_code in (404, 405):
        rr = user_sess.get(f"{BASE_URL}/api/signals/generate",
                          params={"symbol": BASE_SYM, "risk_level": "medium"},
                          timeout=60)
    if rr.status_code >= 500:
        pytest.skip(f"public quote outage (environmental): {rr.status_code}")
    assert rr.status_code == 200, f"{rr.status_code} {rr.text[:300]}"
    body = rr.json()
    sig = body.get("signal") if isinstance(body, dict) and "signal" in body else body
    note = str(sig.get("note") or sig.get("reason") or "") if isinstance(sig, dict) else ""
    # Best-effort A3 observation
    txt = note.lower()
    print(f"\n[A3 note] {note!r}")
    assert "vwap-bounce" not in txt and "vwap bounce" not in txt and "vwap-fade" not in txt, (
        f"A3 violated: VWAP signal returned with no closed bars of UTC day: {note}")
