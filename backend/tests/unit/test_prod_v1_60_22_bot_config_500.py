"""prod v1.60.22 — Bot Config save 500: a function-local `from step_up import audit_event` inside one branch of
update_config() made `audit_event` local for the WHOLE function → UnboundLocalError on every step-up save
(live_activation / risk_raise, mode promotion/demotion) that did not include pattern_gate_enabled.
Secondary: the log-redaction filter collapsed uvicorn.access record.args → "expected 5, got 0" tracebacks."""
import ast
import asyncio
import logging
import os
import subprocess
import sys
import types

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _wire(monkeypatch, db):
    import routes.bot_routes as br
    monkeypatch.setattr(br, "get_db", lambda: db)
    calls = {"step_up": [], "audit": [], "applied": []}

    async def _step(db_, user, request, action):
        calls["step_up"].append(action)

    async def _audit(db_, uid, action, meta, request, step_up=False):
        calls["audit"].append((action, step_up))
    monkeypatch.setattr(br, "require_step_up", _step)
    monkeypatch.setattr(br, "audit_event", _audit)

    async def _ready(db_, acc):
        return []
    monkeypatch.setattr(br, "_activation_readiness", _ready)

    async def _apply(db_, uid, account_id, update, **kw):
        calls["applied"].append(update)
        await db_.bot_configs.update_one({"user_id": uid, "account_id": account_id}, {"$set": update})
    monkeypatch.setattr("config_promotion.apply_config_change", _apply)

    async def _floor(user):                                  # v1.60.39 _attach_cooldown_floor → entitlements → real Mongo; CI has no MONGO_URL
        return 1
    monkeypatch.setattr("entitlements.cooldown_floor", _floor)
    return calls


def _seed(db):
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1", "mode": "live", "status": "active", "label": "Main", "trading_enabled": True}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(oid), "active": False, "risk_level": "low", "max_concurrent_trades": 1}))
    return str(oid)


def _put(aid, **fields):
    import routes.bot_routes as br
    from models import BotConfigUpdate
    return _run(br.update_config(BotConfigUpdate(**fields), types.SimpleNamespace(headers={}, client=None), account_id=aid,
                                 user={"id": "u1", "email": "u@x", "role": "user"}))


def test_risk_raise_on_live_context_without_pattern_gate_saves(monkeypatch):
    db = FakeDb()
    aid = _seed(db)
    calls = _wire(monkeypatch, db)
    out = _put(aid, max_concurrent_trades=3)                 # prod repro: 500 UnboundLocalError before the fix
    assert out["max_concurrent_trades"] == 3
    assert calls["step_up"] == ["risk_raise"] and ("risk_raise", True) in calls["audit"]


def test_activation_on_live_context_without_pattern_gate_saves(monkeypatch):
    db = FakeDb()
    aid = _seed(db)
    calls = _wire(monkeypatch, db)
    out = _put(aid, active=True)
    assert out["active"] is True
    assert calls["step_up"] == ["live_activation"] and ("live_activation", True) in calls["audit"]


def test_pattern_gate_toggle_by_admin_still_audited(monkeypatch):
    import routes.bot_routes as br
    db = FakeDb()
    aid = _seed(db)
    calls = _wire(monkeypatch, db)
    monkeypatch.setattr("auth.require_admin", lambda u: None)
    from models import BotConfigUpdate
    out = _run(br.update_config(BotConfigUpdate(pattern_gate_enabled=True), types.SimpleNamespace(headers={}, client=None), account_id=aid,
                                user={"id": "u1", "email": "a@x", "role": "admin"}))
    assert out["pattern_gate_enabled"] is True and ("pattern_gate_enabled_changed", False) in calls["audit"]


def test_no_local_import_shadows_a_module_import_anywhere_in_backend():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_local_import_shadows as c
    assert c.shadows_in(os.path.join(ROOT, "backend", "routes", "bot_routes.py")) == []
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_local_import_shadows.py")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:]
    # the checker itself catches the exact prod shape
    bad = ast.parse("from step_up import audit_event\nasync def f(x):\n    if x:\n        from step_up import audit_event\n    await audit_event()\n")
    tmp = os.path.join(ROOT, "backend", "_shadow_probe_tmp.py")
    try:
        open(tmp, "w").write(ast.unparse(bad))
        assert c.shadows_in(tmp) == [(5, "f", "audit_event")]       # ast.unparse re-flows the body
    finally:
        os.remove(tmp)
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    assert "F823" in ci and "scripts/check_local_import_shadows.py" in ci


def test_redaction_filter_keeps_uvicorn_access_args_shape():
    from security_agent.redact import RedactingFilter
    f = RedactingFilter()
    secret_path = "/api/bridge/poll?token=" + "A" * 48
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", secret_path, "1.1", 200), None)
    assert f.filter(rec) is True
    assert isinstance(rec.args, tuple) and len(rec.args) == 5                 # uvicorn's AccessFormatter can still unpack
    client, method, path, version, status = rec.args
    assert "A" * 48 not in path and method == "GET" and status == 200
    rec.getMessage()                                                        # formats without "not enough values to unpack"
    # ordinary loggers keep the old behaviour (message masked in place)
    rec2 = logging.LogRecord("bot.routes", logging.INFO, "", 0, "token=%s", ("A" * 48,), None)
    f.filter(rec2)
    assert "A" * 48 not in rec2.getMessage() and rec2.args == ()
