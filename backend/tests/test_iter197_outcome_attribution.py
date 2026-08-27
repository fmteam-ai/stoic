"""iter-197 — Outcome Attribution engine (v56 §12/§13): every closed
trade decomposes into multi-category contributions; losses dominated by
execution/broker/infra noise are flagged NOT alpha-clean so AI learning
stops treating every loss as 'strategy bad'."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN = ("admin@trading.bot", "admin123")
TIMEOUT = 30
UID = "attr-test-user"


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _mk_trade(**over):
    doc = {"user_id": UID, "account_id": "acct-attr", "status": "closed",
           "symbol": "XAUUSD", "action": "BUY", "entry_price": 2400.0,
           "stop_loss": 2390.0, "take_profit": 2420.0, "exit_price": 2390.0,
           "lot_size": 0.1, "pnl": -100.0, "origin": "scalp",
           "opened_at": _iso(hours=2), "closed_at": _iso(hours=1)}
    doc.update(over)
    return doc


def _cleanup():
    db = _db()
    _run(db.trades.delete_many({"user_id": UID}))
    _run(db.trade_outcomes.delete_many({"user_id": UID}))


class TestAttributionEngine:
    def teardown_method(self):
        _cleanup()

    def _attr(self, trade_doc):
        from outcome_attribution import attribute_trade
        db = _db()
        r = _run(db.trades.insert_one(trade_doc))
        trade = _run(db.trades.find_one({"_id": r.inserted_id}))
        return _run(attribute_trade(db, trade)), str(r.inserted_id)

    def test_clean_loss_blames_alpha(self):
        """A clean full-SL loss with no anomalies → ALPHA_ERROR dominates
        and the outcome is alpha-clean (safe for AI learning)."""
        out, tid = self._attr(_mk_trade())
        assert out["result_r"] == -1.0 and out["r_source"] == "price"
        assert out["primary_category"] == "ALPHA_ERROR"
        assert out["attribution"]["ALPHA_ERROR"] >= 0.6
        assert out["alpha_clean"] is True
        assert abs(sum(out["attribution"].values()) - 1.0) < 0.02
        trade = _run(_db().trades.find_one({"user_id": UID}))
        assert trade["attribution_primary"] == "ALPHA_ERROR"

    def test_noisy_loss_is_not_alpha_clean(self):
        """Loss with heavy slippage + dispatch retries + broker reject →
        execution/broker/infra absorb blame; NOT alpha-clean."""
        out, _ = self._attr(_mk_trade(
            slippage=4.0, _dispatch_count=3,
            error="Requote: off quotes", external_open=True))
        a = out["attribution"]
        assert a.get("EXECUTION_ERROR", 0) > 0
        assert a.get("BROKER_ERROR", 0) > 0
        assert a.get("INFRASTRUCTURE_ERROR", 0) > 0
        noise = (a.get("EXECUTION_ERROR", 0) + a.get("BROKER_ERROR", 0)
                 + a.get("INFRASTRUCTURE_ERROR", 0))
        assert noise > a.get("ALPHA_ERROR", 0), \
            "noise must outweigh alpha blame on an infra-destroyed trade"
        assert out["alpha_clean"] is False
        assert abs(sum(a.values()) - 1.0) < 0.02

    def test_win_is_mostly_normal_variance(self):
        out, _ = self._attr(_mk_trade(exit_price=2420.0, pnl=200.0))
        assert out["result_r"] == 2.0
        assert out["primary_category"] == "NORMAL_VARIANCE"
        assert out["attribution"]["NORMAL_VARIANCE"] >= 0.6
        assert "ALPHA_ERROR" not in out["attribution"]

    def test_correlation_and_sizing_signals(self):
        db = _db()
        # two overlapping losers → correlation contribution on the third
        for i in range(2):
            _run(db.trades.insert_one(_mk_trade(
                symbol=f"EURUSD{i}", opened_at=_iso(hours=3),
                closed_at=_iso(minutes=30))))
        out, _ = self._attr(_mk_trade(authority_reduced=True))
        assert out["attribution"].get("CORRELATION_ERROR", 0) > 0
        assert out["attribution"].get("SIZING_ERROR", 0) == 0.15
        assert out["signals"]["concurrent_losers"] == 2

    def test_attribute_missing_backfill_idempotent(self):
        from outcome_attribution import attribute_missing
        db = _db()
        for _ in range(3):
            _run(db.trades.insert_one(_mk_trade()))
        n1 = _run(attribute_missing(db, limit=50,
                                    query={"user_id": UID}))
        assert n1 == 3
        # marked trades are not re-attributed
        n2 = _run(attribute_missing(db, limit=50,
                                    query={"user_id": UID}))
        assert n2 == 0
        remaining = _run(db.trades.count_documents(
            {"user_id": UID, "attribution": {"$exists": False}}))
        assert remaining == 0
        outs = _run(db.trade_outcomes.count_documents({"user_id": UID}))
        assert outs == 3


class TestAttributionApi:
    def setup_method(self):
        _cleanup()
        from outcome_attribution import attribute_trade
        db = _db()
        for over in ({}, {"exit_price": 2420.0, "pnl": 200.0},
                     {"slippage": 4.0, "error": "requote"}):
            r = _run(db.trades.insert_one(_mk_trade(**over)))
            t = _run(db.trades.find_one({"_id": r.inserted_id}))
            _run(attribute_trade(db, t))

    def teardown_method(self):
        _cleanup()

    def test_summary_endpoint(self):
        s = _login()
        r = s.get(f"{API}/attribution/summary?days=30", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 3
        assert "ALPHA_ERROR" in body["categories"]
        assert body["lesson"]
        assert body["worst_category"] is not None

    def test_trades_endpoint_and_detail(self):
        s = _login()
        r = s.get(f"{API}/attribution/trades?limit=10", timeout=TIMEOUT)
        assert r.status_code == 200
        outcomes = r.json()["outcomes"]
        assert len(outcomes) >= 3
        one = outcomes[0]
        r = s.get(f"{API}/attribution/trades/{one['trade_id']}",
                  timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.json()["attribution"]

    def test_requires_auth(self):
        r = requests.get(f"{API}/attribution/summary", timeout=TIMEOUT)
        assert r.status_code in (401, 403)

    def test_backfill_admin_only(self):
        s = _login()
        r = s.post(f"{API}/attribution/backfill", timeout=60)
        assert r.status_code == 200
        assert "attributed" in r.json()
