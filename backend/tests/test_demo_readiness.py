"""Demo-readiness checklist (main94 deploy checklist as one in-app page). Run with DB_NAME="" (pure unit)."""
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
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _by_id(checks):
    return {c["id"]: c for c in checks}


def test_env_checks_grade_secret_file_vs_env_vs_missing():
    import demo_readiness as dr
    c = _by_id(dr.env_checks({"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "x", "SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE": "/run/secrets/t",
                              "SECURITY_AGENT_TELEGRAM_CHAT_ID": "1", "BRIDGE_TOKEN_HASH_KEY": "k" * 40, "RESEND_API_KEY": "r",
                              "UPDATE_HOLD_ON_FAILURE": "1"}))
    assert {c[k]["status"] for k in ("telegram_secret", "telegram_chat", "bridge_hash_key", "resend", "hold_on_failure")} == {"pass"}
    c = _by_id(dr.env_checks({"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "x", "BRIDGE_TOKEN_HASH_KEY": "short"}))
    assert c["telegram_secret"]["status"] == "warn" and "backend/.env" in c["telegram_secret"]["detail"]
    assert c["bridge_hash_key"]["status"] == "warn" and c["resend"]["status"] == "fail" and c["telegram_chat"]["status"] == "fail"
    c = _by_id(dr.env_checks({}))
    assert c["telegram_secret"]["status"] == "fail" and "JWT_SECRET" in c["bridge_hash_key"]["detail"]
    assert c["client_ip"]["status"] == "info"


def _fleet_db(fresh=True, ea="1.60", attested=True, cap=3, braked=False):
    db = FakeDb()
    uid = str(ObjectId())
    hb = (datetime.now(timezone.utc) - timedelta(seconds=10 if fresh else 900)).isoformat()
    acc = {"_id": ObjectId(), "user_id": uid, "label": "Demo-1", "mode": "live", "trading_enabled": True,
           "last_heartbeat": hb, "ea_version": ea, "position_mode_override": {"mode": "netting", "by": "admin"},
           "execution_brake": {"active": braked}}
    db.accounts.rows.append(acc)
    db.users.rows.append({"_id": ObjectId(), "email": "admin@stoicaibot.com", "two_factor_enabled": True})
    db.bot_configs.rows.append({"user_id": uid, "account_id": None, "trade_of_day_cap": cap, "max_concurrent_trades": 2})
    return db, acc


def test_build_scores_fleet_and_manual_steps():
    import demo_readiness as dr
    db, acc = _fleet_db()
    with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@stoicaibot.com"}), \
            patch("broker_env.attested_environment", lambda a: "DEMO"), \
            patch("ea_capabilities.accepted_ea_sha256s", lambda: ["a" * 64]):
        out = run(dr.build(db))
    c = _by_id(out["checks"])
    assert c["token_migration"]["status"] == "pass" and c["admin_mfa"]["status"] == "pass" and c["brakes"]["status"] == "pass"
    assert c["ea_release"]["status"] == "pass"
    for k in ("fleet_heartbeat", "fleet_ea", "fleet_attested", "fleet_position_mode", "fleet_caps"):
        assert c[k]["status"] == "pass", k
    row = out["fleet"][0]
    assert row["position_mode"] == "netting" and row["position_mode_source"] == "admin" and row["caps_explicit"]
    assert len(out["manual"]) == len(dr.MANUAL_STEPS) and not out["score"]["ready"]      # manual steps still open
    assert out["score"]["total"] == len([x for x in out["checks"] if x["status"] != "info"]) + len(dr.MANUAL_STEPS)
    # tick every manual step → ready (env warnings do not block; fails do)
    for m in out["manual"]:
        run(dr.set_manual(db, m["id"], True, actor="admin@stoicaibot.com"))
    with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@stoicaibot.com"}), \
            patch("broker_env.attested_environment", lambda a: "DEMO"), \
            patch("ea_capabilities.accepted_ea_sha256s", lambda: ["a" * 64]):
        out2 = run(dr.build(db))
    assert out2["score"]["ready"] is True and all(m["checked"] and m["checked_by"] for m in out2["manual"])
    assert any(r["action"] == "demo_readiness_manual" for r in db.audit_log.rows)
    with pytest.raises(KeyError):
        run(dr.set_manual(db, "nope", True, actor="x"))


def test_build_flags_stale_heartbeat_old_ea_live_account_cap_and_brake():
    import demo_readiness as dr
    db, acc = _fleet_db(fresh=False, ea="1.57", cap=0, braked=True)
    acc["bridge_token"] = "plain"
    acc["execution_brake"].update({"since": "2026-06-01T00:00:00+00:00", "reason": "r"})
    with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@stoicaibot.com"}), \
            patch("broker_env.attested_environment", lambda a: "LIVE"), \
            patch("ea_capabilities.accepted_ea_sha256s", lambda: []):
        out = run(dr.build(db))
    c = _by_id(out["checks"])
    assert c["token_migration"]["status"] == "fail" and c["brakes"]["status"] == "fail" and c["ea_release"]["status"] == "warn"
    for k in ("fleet_heartbeat", "fleet_ea", "fleet_attested", "fleet_caps"):
        assert c[k]["status"] == "fail" and "Demo-1" in c[k]["detail"], k
    assert out["blockers"] and not out["score"]["ready"]


def test_empty_fleet_is_a_blocker():
    import demo_readiness as dr
    assert dr.fleet_checks([])[0]["status"] == "fail"


def test_audit_write_failure_raises_ops_alert_not_silence():
    import demo_readiness as dr
    db = FakeDb()

    class _Broken:
        async def insert_one(self, *_a, **_k):
            raise RuntimeError("mongo down")

    db.audit_log = _Broken()
    raised = []

    async def _fake_alert(_db, kind, severity, message, dedup_key=None, meta=None, **_k):
        raised.append((kind, severity, dedup_key))

    with patch("alerting.raise_alert", _fake_alert):
        out = run(dr.set_manual(db, "ci_green", True, actor="admin@x.io"))
    assert out["checked"] is True
    assert raised == [("audit_write_failed", "critical", "audit_write_failed:demo_readiness:ci_green")]


def test_routes_admin_only_and_ui_wired():
    from routes import admin_routes
    for fn in (admin_routes.admin_demo_readiness, admin_routes.admin_demo_readiness_manual):
        assert "_admin_only(user)" in inspect.getsource(fn)
    app = open(os.path.join(ROOT, "frontend", "src", "App.jsx"), encoding="utf-8").read()
    assert '<Route path="/admin/demo-readiness" element={<ProtectedRoute requireAdmin><DemoReadiness />' in app
    assert '"/admin/demo-readiness"' in open(os.path.join(ROOT, "frontend", "src", "components", "Sidebar.jsx"), encoding="utf-8").read()
    page = open(os.path.join(ROOT, "frontend", "src", "pages", "DemoReadiness.jsx"), encoding="utf-8").read()
    for tid in ("demo-readiness-score", "demo-check-${c.id}", "demo-manual-toggle-${m.id}", "demo-fleet-table", "demo-readiness-print"):
        assert tid in page, tid
