"""Audit r23 — SEC-001 step-up MFA covers every risk-raising / guard-weakening
bot-config field; hardenings: request body cap, no X-Real-IP fallback."""
import os
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))


def test_every_guard_weakening_change_is_a_risk_raise():
    from routes.bot_routes import (GUARD_FLAG_FIELDS, GUARD_MODE_FIELDS, RISK_CAP_ZERO_UNLIMITED_FIELDS,
                                   RISK_LEVEL_RANK, RISK_LOWER_IS_RAISE_FIELDS, RISK_RAISE_FIELDS, _is_risk_raise)
    from models import BotConfigUpdate
    fields = set(BotConfigUpdate.model_fields)
    for group in (GUARD_FLAG_FIELDS, GUARD_MODE_FIELDS, RISK_CAP_ZERO_UNLIMITED_FIELDS,
                  RISK_LOWER_IS_RAISE_FIELDS, RISK_RAISE_FIELDS):
        assert set(group) <= fields, set(group) - fields
    assert RISK_LEVEL_RANK["medium"] == 1 and "middle" not in RISK_LEVEL_RANK
    assert _is_risk_raise({"risk_level": "medium"}, {"risk_level": "low"})          # auditor's low→medium gap
    assert not _is_risk_raise({"risk_level": "low"}, {"risk_level": "medium"})
    # guard disables (missing stored value falls back to the model default, never 0)
    for f in GUARD_FLAG_FIELDS:
        assert _is_risk_raise({f: False}, {}), f
        assert not _is_risk_raise({f: True}, {f: False}), f
    for f in GUARD_MODE_FIELDS:
        assert _is_risk_raise({f: "off"}, {}) and _is_risk_raise({f: "advisory"}, {}), f
        assert not _is_risk_raise({f: "enforce"}, {f: "off"}), f
    # loss-cap / concurrency / leverage increases vs stored or default
    assert _is_risk_raise({"daily_drawdown_enabled": False}, {"daily_drawdown_enabled": True})
    assert _is_risk_raise({"max_concurrent_trades": 10}, {"max_concurrent_trades": 1})
    assert not _is_risk_raise({"max_concurrent_trades": 1}, {"max_concurrent_trades": 3})
    assert _is_risk_raise({"daily_drawdown_pct": 5.0}, {}) and not _is_risk_raise({"daily_drawdown_pct": 2.0}, {})
    assert _is_risk_raise({"max_leverage": 50}, {}) and _is_risk_raise({"adaptive_risk_floor_pct": 1.0}, {})
    # strictness decreases
    assert _is_risk_raise({"min_calibrated_confidence": 30}, {}) and not _is_risk_raise({"min_calibrated_confidence": 80}, {})
    assert _is_risk_raise({"min_final_rr": 0.2}, {}) and _is_risk_raise({"sl_cooldown_minutes": 5}, {})
    # 0 = unlimited caps
    assert _is_risk_raise({"trade_of_day_cap": 0}, {}) and _is_risk_raise({"max_lot_size": 0}, {"max_lot_size": 1.0})
    assert not _is_risk_raise({"max_lot_size": 0.5}, {"max_lot_size": 1.0})
    # neutral / lowering payload never gates
    assert not _is_risk_raise({"label": "x", "daily_drawdown_pct": 1.0, "max_concurrent_trades": 1,
                               "spread_filter_enabled": True}, {})


def test_update_config_calls_step_up_for_guard_disable_on_live(monkeypatch):
    src = open(os.path.join(ROOT, "backend", "routes", "bot_routes.py")).read()
    assert "risk_raise = _is_risk_raise(update, current_cfg)" in src
    assert "if (wants_activation or risk_raise) and await _live_context(" in src
    assert 'await require_step_up(db, user, request, action)' in src


def test_request_body_limit_rejects_declared_and_streamed_bodies():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from security import RequestBodyLimit
    mini = FastAPI()

    @mini.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    mini.add_middleware(RequestBodyLimit, max_bytes=1000)
    c = TestClient(mini)
    assert c.post("/echo", content=b"a" * 999).json() == {"n": 999}
    assert c.post("/echo", content=b"a" * 1001).status_code == 413

    def gen():
        yield b"a" * 600
        yield b"a" * 600
    assert c.post("/echo", content=gen()).status_code == 413
    server_src = open(os.path.join(ROOT, "backend", "server.py")).read()
    assert "app.add_middleware(_RequestBodyLimit)" in server_src


def test_client_ip_ignores_x_real_ip():
    from fastapi import Request
    from security import client_ip
    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "client": ("10.0.0.9", 1),
             "headers": [(b"x-real-ip", b"1.2.3.4")]}
    assert client_ip(Request(scope)) == "10.0.0.9"
    scope["headers"] = [(b"x-forwarded-for", b"1.2.3.4, 203.0.113.7")]
    assert client_ip(Request(scope)) == "203.0.113.7"
