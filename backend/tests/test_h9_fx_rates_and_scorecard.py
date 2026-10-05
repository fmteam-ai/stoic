"""H9 live FX rates + security-agent observe scorecard.
H9  fx_rates resolves USD-per-quote-currency from the broker tick → public quote → approx table (flagged),
    and every sizing / guardian / settlement path uses it.
SC  scorecard counts proposals per rule and detects "would have hit a real user" with evidence.
Pure unit tests (fake async db) — run with DB_NAME="".
"""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── H9 ──────────────────────────────────────────────────────────────────────
def test_h9_rate_prefers_broker_tick_then_public_quote_then_table():
    import fx_rates
    fx_rates.reset_cache()

    async def tick(symbol, user_id, max_age_s=180):
        return {"USDJPY": 158.0}.get(symbol)

    async def quote(symbol):
        return {"GBPUSD": {"price": 1.31}}.get(symbol)
    with patch("intraday_features.broker_live_price", tick), patch("market.get_quote", quote):
        assert run(fx_rates.quote_usd("JPY", "u1")) == (pytest.approx(1 / 158.0), "broker_tick")
        assert run(fx_rates.quote_usd("GBP", "u1")) == (1.31, "public_quote")
        rate, src = run(fx_rates.quote_usd("CHF", "u1"))
        assert src == "approx_table" and rate == fx_rates.QUOTE_USD_APPROX["CHF"]
        assert run(fx_rates.quote_usd("USD")) == (1.0, "usd")
    snap = fx_rates.rates_snapshot()
    assert snap["stale_currencies"] == ["CHF"] and snap["rates"]["JPY"]["source"] == "broker_tick"


def test_h9_symbol_rate_only_for_non_usd_crosses_and_cached():
    import fx_rates
    fx_rates.reset_cache()
    calls = []

    async def tick(symbol, user_id, max_age_s=180):
        calls.append(symbol); return 1.09 if symbol == "EURUSD" else None

    async def quote(symbol):
        return None
    with patch("intraday_features.broker_live_price", tick), patch("market.get_quote", quote):
        assert run(fx_rates.quote_usd_for_symbol("EURUSD", "u1")) == (None, "n/a")
        assert run(fx_rates.quote_usd_for_symbol("USDCHF", "u1")) == (None, "n/a")
        assert run(fx_rates.quote_usd_for_symbol("XAUUSD", "u1")) == (None, "n/a")
        assert run(fx_rates.quote_usd_for_symbol("GBPEUR", "u1")) == (1.09, "broker_tick")    # EUR quote
        assert run(fx_rates.quote_usd_for_symbol("CHFEUR", "u1")) == (1.09, "broker_tick")    # cached, no second call
    assert calls == ["EURUSD"]


def test_h9_pip_value_live_uses_todays_rate():
    import fx_rates
    fx_rates.reset_cache()

    async def tick(symbol, user_id, max_age_s=180):
        return 1.31 if symbol == "GBPUSD" else None

    async def quote(symbol):
        return None
    with patch("intraday_features.broker_live_price", tick), patch("market.get_quote", quote):
        v, src = run(fx_rates.pip_value_usd_per_lot_live("EURGBP", "standard", price=0.85, user_id="u1"))
    assert src == "broker_tick" and v == pytest.approx(13.10)        # 10 GBP × 1.31 — not the table's 12.70


def test_h9_every_sizing_and_risk_path_is_wired():
    import risk, bot_runner, safety_guardian, execution
    import routes.trade_routes as tr, routes.bot_routes as br
    assert "quote_usd: float | None = None" in inspect.getsource(risk.compute_lot_for_account)
    assert "quote_usd=quote_usd" in inspect.getsource(risk.compute_lot_for_account)
    for mod in (bot_runner, tr, br):
        assert "quote_usd_for_symbol(" in inspect.getsource(mod) and "quote_usd=_qusd" in inspect.getsource(mod), mod.__name__
    sg = inspect.getsource(safety_guardian.audit_pre_trade)
    assert sg.count("pip_value_usd_per_lot_live(") == 2 and "pip_rate_live" in sg
    assert "pip_value_usd_per_lot_live(sym, \"standard\", price=price" in inspect.getsource(execution)
    from security_matrix import BOLA_MATRIX
    assert BOLA_MATRIX[("GET", "/api/diagnostic/fx-rates")] == "admin_only"


# ── scorecard ───────────────────────────────────────────────────────────────
def _act(rule, action, target, status="would_have_done", kind="ip", at=None):
    return {"_id": ObjectId(), "kind": "containment", "rule": rule, "action": action, "target_kind": kind,
            "target": target, "status": status, "at": (at or datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}


def test_scorecard_detects_real_user_hits_with_evidence():
    from security_agent import scorecard
    db = FakeDb()
    now = datetime.now(timezone.utc)
    # R1 — 5 IP proposals: one IP also created a login session → hit; others attackers
    for i in range(4):
        db.security_actions.rows.append(_act("R1", "block_ip", f"203.0.113.{i}"))
    db.security_actions.rows.append(_act("R1", "block_ip", "198.51.100.9"))
    db.auth_sessions.rows.append({"ip": "198.51.100.9", "user_id": "u123456", "created_at": now.isoformat()})
    # R6 — 6 proposals, none hits → safe
    for i in range(6):
        db.security_actions.rows.append(_act("R6", "block_ip", f"192.0.2.{i}"))
    # R2 — 2 proposals only → insufficient data
    db.security_actions.rows.append(_act("R2", "lock_login", "owner@x.io", kind="account"))
    db.security_actions.rows.append(_act("R2", "lock_login", "nobody@x.io", kind="account"))
    db.users.rows.append({"_id": ObjectId(), "email": "owner@x.io"})
    db.auth_sessions.rows.append({"ip": "1.1.1.1", "user_id": str(db.users.rows[0]["_id"]), "created_at": now.isoformat()})
    # R7 — frozen account kept trading → hit
    acc = ObjectId()
    for i in range(5):
        db.security_actions.rows.append(_act("R7", "freeze_new_entries", str(acc), kind="account", status="done"))
    db.trades.rows.append({"account_id": str(acc), "origin": "auto", "opened_at": now.isoformat(), "symbol": "XAUUSD"})
    out = run(scorecard.build(db, {"mode": "observe", "rules_enabled": ["R4"]}, days=14))
    by = {r["rule"]: r for r in out["rules"]}
    assert by["R1"]["proposals"] == 5 and by["R1"]["real_user_hits"] == 1 and by["R1"]["verdict"] == "review"
    assert "login session from 198.51.100.9" in by["R1"]["examples"][0]["evidence"]
    assert by["R6"]["verdict"] == "safe_to_enforce" and by["R6"]["false_positive_rate"] == 0.0
    assert by["R2"]["verdict"] == "insufficient_data" and by["R2"]["real_user_hits"] == 1
    assert by["R7"]["verdict"] == "review" and by["R7"]["done"] == 5 and by["R7"]["would_have_done"] == 0
    assert by["R4"]["enabled"] is True and by["R4"]["proposals"] == 0 and by["R4"]["verdict"] == "insufficient_data"


def test_scorecard_user_scope_and_token_hits():
    from security_agent.scorecard import real_user_hit
    db = FakeDb()
    assert run(real_user_hit(db, _act("R4", "revoke_sessions", "u1", kind="user"))) is not None
    acc = ObjectId()
    at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.accounts.rows.append({"_id": acc, "label": "ICM", "hb_sightings": [{"ip": "9.9.9.9", "at": (at + timedelta(minutes=5)).isoformat()}]})
    assert "kept heartbeating" in run(real_user_hit(db, _act("R5", "suspend_bridge_token", str(acc), kind="account", at=at)))
    db.accounts.rows[0]["hb_sightings"] = [{"ip": "9.9.9.9", "at": (at - timedelta(hours=2)).isoformat()}]   # only BEFORE the proposal
    assert run(real_user_hit(db, _act("R5", "suspend_bridge_token", str(acc), kind="account", at=at))) is None


def test_scorecard_endpoint_and_ui_wired():
    import routes.security_agent_routes as r
    assert '@router.get("/scorecard")' in inspect.getsource(r) and "require_admin(user)" in inspect.getsource(r.observe_scorecard)
    from security_matrix import BOLA_MATRIX
    assert BOLA_MATRIX[("GET", "/api/admin/security/scorecard")] == "admin_only"
    root = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "components", "security")
    assert "security-scorecard-verdict-" in open(os.path.join(root, "ObserveScorecard.jsx")).read()
    assert "<ObserveScorecard" in open(os.path.join(root, "SecurityHealthPanel.jsx")).read()
