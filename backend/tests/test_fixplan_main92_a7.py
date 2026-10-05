"""main92 review — step A7 (the open main90–91 list) + A7d execution-health brake.
N1  manual 'in' deals are never folded into a bot row (netting: own row + alert).
N2  /report late fill + slippage veto never $sets and $unsets close_reason together (500 loop).
N3  stats_excluded / late-fill stamps written directly, not only via the open-only close request.
N4  AUTO-REVIVE never undoes a PANIC / late-fill / in-flight FULL_CLOSE close.
N5  report path falls back to the ORDER ticket for the netting leg; merge upgrades it to the deal id.
N6  duplicate-archive age guard reads the newest timestamp of the row.
N8  the API integration test never borrows a real account.
N10 account list survives a registry error.
N11 BOLA matrix declares the admin environment / position-mode routes.
N12 fill after a broker reject is flagged + alerted.
N13 admin position-mode change audited BEFORE the write; wrong re-auth never refresh-retried.
H1  EA reports margin_mode; registry lookup prefers the EA-reported server.
stats_excluded honoured by every statistics reader.
A7d execution-health brake: events → engage → pulse skip / execute block → auto / manual release.
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


# ── N1 ──────────────────────────────────────────────────────────────────────
def test_n1_merge_refuses_manual_deal_into_bot_row():
    from routes.bridge_routes import _merge_duplicate_in_deal

    class P:
        mt5_ticket = 555
        deal_id = 9002
        position_volume = 0.2
    db = FakeDb()
    bot = {"_id": ObjectId(), "account_id": "a1", "mt5_ticket": 555, "status": "open", "origin": "auto", "position_leg": 9001, "opened_at": "2026-10-05T10:00:00+00:00"}
    db.trades.rows.append(bot)
    assert run(_merge_duplicate_in_deal(db, "a1", P(), {"origin": "manual", "position_leg": 9002})) is None
    assert 9002 not in (db.trades.rows[0].get("merged_deal_ids") or [])
    # a STOIC deal still merges
    assert run(_merge_duplicate_in_deal(db, "a1", P(), {"origin": "auto", "position_leg": 9001})) is not None


def test_n1_external_deal_netting_manual_add_becomes_own_row_with_alert():
    import routes.bridge_routes as br
    src = inspect.getsource(br.external_deal)
    assert "shared_with_bot_row" in src and "manual_volume_on_bot_position" in src
    assert 'existing.get("position_leg") != int(payload.deal_id)' in src and "await is_netting_account(db, acc)" in src


# ── N2 ──────────────────────────────────────────────────────────────────────
def test_n2_no_set_unset_path_conflict_on_late_fill_with_slippage_veto():
    import routes.bridge_routes as br
    src = inspect.getsource(br)
    assert 'unset = {k: "" for k in ("error", "close_reason") if k not in update}' in src
    update = {"status": "open", "close_reason": "slippage_veto"}
    unset = {k: "" for k in ("error", "close_reason") if k not in update}
    assert unset == {"error": ""}


# ── N3 ──────────────────────────────────────────────────────────────────────
def test_n3_late_fill_stamps_written_directly():
    import routes.bridge_routes as br
    s_panic = inspect.getsource(br.close_late_fill_after_panic)
    s_exp = inspect.getsource(br.close_late_fill_after_expiry)
    assert '"late_fill_after_panic": True, "stats_excluded": True}})' in s_panic and "await db.trades.update_one" in s_panic
    assert 'flag: True, "stats_excluded": True}})' in s_exp and 'reason == "late_fill_after_lock"' in s_exp
    assert 'stamp={"late_fill_after_panic": True, "stats_excluded": True}' in s_panic


# ── N4 ──────────────────────────────────────────────────────────────────────
def test_n4_intentional_closes_are_never_revived():
    from routes.bridge_routes import intentional_close, NO_REVIVE_FILTER
    assert intentional_close({"close_reason": "panic"})
    assert intentional_close({"late_fill_after_expiry": True})
    assert intentional_close({"close_requested": True})
    assert intentional_close({"pending_modification": {"type": "FULL_CLOSE"}})
    assert not intentional_close({"close_reason": "slippage_veto", "status": "closed"})
    assert "late_fill_after_panic" in NO_REVIVE_FILTER["close_reason"]["$nin"]
    import routes.bridge_routes as br
    src = inspect.getsource(br.heartbeat)
    assert "**NO_REVIVE_FILTER" in src and "not intentional_close(existing)" in src


# ── N5 ──────────────────────────────────────────────────────────────────────
def test_n5_order_ticket_leg_fallback_and_upgrade():
    import routes.bridge_routes as br
    src = inspect.getsource(br.report_trade)
    assert "payload.deal_ticket or payload.order_ticket" in src
    assert '"deal_ticket" if payload.deal_ticket else "order_ticket"' in src
    msrc = inspect.getsource(br._merge_duplicate_in_deal)
    assert 'existing.get("position_leg_source") == "order_ticket"' in msrc


# ── N6 ──────────────────────────────────────────────────────────────────────
def test_n6_age_guard_reads_newest_timestamp():
    from ops.ticket_duplicates import _ts
    row = {"opened_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T01:00:00",
           "live_snapshot_at": "2026-10-05T12:00:00", "pending_modification": {"requested_at": "2026-10-05T12:30:00"}}
    assert _ts(row) == "2026-10-05T12:30:00"
    assert _ts({}) == ""


# ── N8 / N10 / N11 / N12 / N13 ──────────────────────────────────────────────
def test_n8_integration_test_skips_instead_of_borrowing_real_account():
    src = open(os.path.join(os.path.dirname(__file__), "test_a6_api_integration.py")).read()
    assert "pytest.skip(" in src and "qa-teardown" not in src


def test_n10_account_list_survives_registry_error():
    import routes.account_routes as ar
    src = inspect.getsource(ar.list_accounts)
    assert '{"mode": "unknown", "source": "error"}' in src and "except Exception" in src


def test_n11_bola_matrix_declares_admin_env_and_position_mode_routes():
    from security_matrix import BOLA_MATRIX
    for key in [("GET", "/api/admin/account-environments"), ("POST", "/api/admin/account-environments/{account_id}"),
                ("GET", "/api/admin/account-position-modes"), ("POST", "/api/admin/account-position-modes/{account_id}"),
                ("GET", "/api/accounts/{account_id}/execution-health"),
                ("POST", "/api/accounts/{account_id}/execution-brake/release")]:
        assert key in BOLA_MATRIX, key
    md = open(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "BOLA_MATRIX.md")).read()
    assert "/api/admin/account-position-modes/{account_id}" in md


def test_n12_fill_after_reject_is_flagged_and_alerted():
    from execution_intents import record_late_fill
    db = FakeDb()
    db.execution_intents.rows.append({"intent_id": "i1", "status": "rejected", "history": []})
    alerts = []

    async def fake_alert(db_, kind, sev, msg, dedup_key=None, meta=None, **kw):
        alerts.append(kind)
    with patch("alerting.raise_alert", fake_alert):
        out = run(record_late_fill(db, "i1", ticket=77, via="report", prior="rejected"))
    assert out["status"] == "filled" and out["late_fill"] is True
    assert db.execution_intents.rows[0].get("late_fill_anomaly") is True
    assert alerts == ["late_fill_after_reject"]


def test_n13_audit_before_write_and_no_refresh_retry_on_reauth_failure():
    import routes.admin_routes as ar
    src = inspect.getsource(ar.admin_set_account_position_mode)
    assert src.index('"phase": "intent"') < src.index("position_mode_override") and '"phase": "applied"' in src
    assert "account_position_mode_set_failed" in src
    js = open(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "lib", "api.js")).read()
    assert 'detail?.code === "reauth_failed"' in js and "!reauthFailed" in js


# ── H1 leftover ─────────────────────────────────────────────────────────────
def test_h1_ea_reports_margin_mode_and_registry_prefers_ea_server():
    mq5 = open(os.path.join(os.path.dirname(__file__), "..", "static", "EmergentTradingBridge.mq5")).read()
    assert '"margin_mode\\":\\"%s\\"' in mq5 and "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) ? \"hedging\" : \"netting\"" in mq5
    from models import BridgeHeartbeat
    assert "margin_mode" in BridgeHeartbeat.model_fields
    import routes.bridge_routes as br
    hb = inspect.getsource(br.heartbeat)
    assert 'set_doc["margin_mode"] = hb_margin_mode' in hb
    res_src = inspect.getsource(br.position_mode_resolution)
    assert 'ident.get("broker_server")' in res_src and '"server_source": "ea" if ea_server else "user_typed"' in res_src
    # EA-reported margin mode wins over the registry
    db = FakeDb()
    out = run(br.position_mode_resolution(db, {"margin_mode": "netting"}))
    assert out == {"mode": "netting", "source": "ea"}


# ── stats_excluded gaps ─────────────────────────────────────────────────────
def test_stats_excluded_honoured_by_every_statistics_reader():
    import loss_cooldown, strategy_decay, meta_decision, rl_policy, rl_allocator, online_learning, ai_optimizer
    import routes.diagnostic_routes as dr, routes.analytics_routes as anr, routes.posture_routes as pr
    for mod in (loss_cooldown, strategy_decay, meta_decision, rl_policy, rl_allocator, online_learning, ai_optimizer, dr, anr, pr):
        assert '"stats_excluded": {"$ne": True}' in inspect.getsource(mod), mod.__name__


# ── A7d ─────────────────────────────────────────────────────────────────────
def _acc_row():
    return {"_id": ObjectId(), "user_id": "u1", "label": "ICM-1", "mode": "live"}


def test_a7d_only_late_fills_count_one_alerts_owner_two_in_24h_pause():
    """Operator spec: 1 late fill → owner (Telegram + in-app) + admins alerted; rejects / slippage /
    duplicates are recorded but never count; 2 late fills in 24 h → brake, no auto-release."""
    import execution_health as eh
    db = FakeDb()
    acc = _acc_row(); db.accounts.rows.append(acc)
    alerts, tg = [], []

    async def fake_alert(db_, kind, sev, msg, dedup_key=None, meta=None, **kw):
        alerts.append((kind, sev))

    async def fake_tg(user_id, event_type, title, lines):
        tg.append((user_id, event_type)); return True
    with patch("alerting.raise_alert", fake_alert), patch("notifier.send_telegram", fake_tg):
        # non-late-fill anomalies: recorded, never alert, never count
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "reject", trade_id="t2")) is None
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "slippage_veto", trade_id="t3")) is None
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "duplicate_ticket", trade_id="t4")) is None
        assert alerts == [] and tg == [] and not db.notifications.rows
        # first late fill → alert owner + admins, NOT braked
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "late_fill", trade_id="t1", detail="expired via report")) is None
        assert alerts == [("late_fill", "warning")] and tg == [("u1", "execution_brake")]
        assert db.notifications.rows[-1]["kind"] == "execution_late_fill"
        assert not eh.is_braked(db.accounts.rows[0])
        # second late fill → brake engaged, admin-only release, no release_after
        state = run(eh.record_event(db, str(acc["_id"]), "u1", "late_fill", trade_id="t5"))
    assert state and state["active"] and state["event_count"] == 2 and state["kinds"] == ["late_fill"]
    assert state["release_after"] is None and state["release_requires"] == "admin"
    assert ("execution_brake", "critical") in alerts
    assert eh.is_braked(db.accounts.rows[0])
    assert any(a["action"] == "execution_brake_engaged" for a in db.audit_log.rows)
    s = run(eh.summary(db, str(acc["_id"])))
    assert s["brake"]["active"] and s["events_in_window"] == 2 and s["counted_kinds"] == ["late_fill"] and s["release_min"] is None
    assert eh.THRESHOLD == 2 and eh.WINDOW_MIN == 1440


def test_a7d_no_auto_release_admin_release_audited_and_old_events_never_retrigger():
    import execution_health as eh
    db = FakeDb()
    acc = _acc_row()
    acc["execution_brake"] = {"active": True, "since": "x", "reason": "2 late fills in 24 h", "release_after": None}
    db.accounts.rows.append(acc)
    for i in range(3):
        db.execution_health_events.rows.append({"account_id": str(acc["_id"]), "kind": "late_fill",
                                                "at": (datetime.now(timezone.utc) - timedelta(minutes=5 + i)).isoformat()})
    # there is no automatic release — ever
    assert run(eh.maybe_auto_release(db, db.accounts.rows[0])) is False
    assert db.accounts.rows[0]["execution_brake"]["active"] is True
    # admin release: audited, step-up verified, released_at recorded
    st = run(eh.release(db, str(acc["_id"]), actor="admin:a@b", reason="admin resume after EA/VPS check"))
    assert st["active"] is False and st["last_reason"] == "2 late fills in 24 h" and st["released_by"] == "admin:a@b"
    assert any(a["action"] == "execution_brake_released" and a["step_up_verified"] for a in db.audit_log.rows)
    # M3 — the three OLD late fills no longer count: the brake does not re-engage on them
    assert run(eh.evaluate(db, str(acc["_id"]))) is None
    assert run(eh.window_events(db, str(acc["_id"]))) == []
    # one NEW late fill after the release counts again (alert only), a second one brakes again
    async def noop(*a, **k):
        return True
    with patch("alerting.raise_alert", noop), patch("notifier.send_telegram", noop):
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "late_fill")) is None
        assert run(eh.record_event(db, str(acc["_id"]), "u1", "late_fill"))["active"] is True


def test_a7d_enforced_at_dispatch_fence_and_admin_only_api():
    """M4 — the brake is checked where EVERY new order passes (poll-trades), so scalp-fast,
    manual and already-queued orders cannot bypass it; the API release is admin + step-up."""
    import routes.bridge_routes as br
    import routes.account_routes as ar
    from security_matrix import BOLA_MATRIX
    src = inspect.getsource(br.poll_trades)
    assert 'lock_reason = "execution_brake"' in src and "_eh_braked(acc)" in src
    rel = inspect.getsource(ar.account_execution_brake_release)
    assert "require_admin(user)" in rel and 'require_step_up(db, user, request, "execution_brake_release")' in rel
    assert '"user_id": user["id"]' not in rel.split("find_one")[1].split(")")[0]   # admin releases ANY account
    assert BOLA_MATRIX[("POST", "/api/accounts/{account_id}/execution-brake/release")] == "admin_only"
    root = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src")
    banner = open(os.path.join(root, "components", "ExecutionBrakeBanner.jsx")).read()
    assert "ADMIN RESUME" in banner and "isAdmin" in banner and "release_after" not in banner


def test_a7d_wired_into_runner_executor_bridge_api_and_ui():
    import bot_runner, execution
    import routes.bridge_routes as br
    import routes.account_routes as ar
    from step_up import STEP_UP_ACTIONS
    assert "EXECUTION BRAKE" in inspect.getsource(bot_runner) and "_eh_braked(target_account)" in inspect.getsource(bot_runner)
    assert '"blocked": "execution_brake"' in inspect.getsource(execution)
    bsrc = inspect.getsource(br)
    for kind in ("late_fill", "reject", "slippage_veto", "duplicate_ticket"):
        assert f'"{kind}"' in bsrc and "record_event" in bsrc
    assert "execution_brake_release" in STEP_UP_ACTIONS
    assert '@router.post("/{account_id}/execution-brake/release")' in inspect.getsource(ar)
    root = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src")
    assert "execution-brake-banner" in open(os.path.join(root, "components", "ExecutionBrakeBanner.jsx")).read()
    assert "<ExecutionBrakeBanner />" in open(os.path.join(root, "pages", "Dashboard.jsx")).read()
    assert "bot-pulse-exec-brake-" in open(os.path.join(root, "components", "BotPulsePanel.jsx")).read()


# ── main92 follow-up corrections ────────────────────────────────────────────
def test_followup_bola_matrix_covers_every_sensitive_route_in_process():
    """The matrix test builds the spec from server.app — mirror it here so the 14 formerly
    undeclared routes (incl. the N11 admin ones) can never silently drop out again."""
    from security_matrix import BOLA_MATRIX
    for key in [("POST", "/api/admin/account-position-modes/{account_id}"), ("POST", "/api/admin/account-environments/{account_id}"),
                ("GET", "/api/accounts/{account_id}/bridge-token"), ("POST", "/api/accounts/{account_id}/trust-terminal"),
                ("DELETE", "/api/authority/inventory/orphan-bots/{bot_id}"), ("GET", "/api/pamm/investor/programs/{program_id}"),
                ("GET", "/api/v1/accounts/{account_id}/certificate"), ("POST", "/api/infra/installations/{installation_id}/device-key/revoke")]:
        assert key in BOLA_MATRIX, key
    assert BOLA_MATRIX[("DELETE", "/api/authority/inventory/orphan-bots/{bot_id}")] == "admin_only"


def test_followup_ci_admin_totp_enrolment_script_and_playwright_totp():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    ci = open(os.path.join(root, ".github", "workflows", "ci.yml")).read()
    assert 'ADMIN_MFA_ENFORCED: "true"' in ci and 'ADMIN_MFA_ENFORCED: "false"' not in ci
    assert "scripts/ci_enrol_admin_totp.py" in ci and "::add-mask::" in ci and 'E2E_ADMIN_TOTP_SECRET="$E2E_ADMIN_TOTP_SECRET"' in ci
    setup = open(os.path.join(root, "e2e", "tests", "auth.setup.ts")).read()
    assert "totpCode(ADMIN_TOTP_SECRET)" in setup and 'getByTestId("login-2fa-input")' in setup
    # the script refuses production and non-CI databases
    import subprocess, sys as _sys
    env = {**os.environ, "APP_ENV": "production", "DB_NAME": "stoic_e2e", "ADMIN_EMAIL": "x@y"}
    assert subprocess.run([_sys.executable, os.path.join(root, "scripts", "ci_enrol_admin_totp.py")], env=env, capture_output=True).returncode == 2
    env = {**os.environ, "APP_ENV": "preview", "DB_NAME": "ai_trading_bot", "ADMIN_EMAIL": "x@y"}
    assert subprocess.run([_sys.executable, os.path.join(root, "scripts", "ci_enrol_admin_totp.py")], env=env, capture_output=True).returncode == 2


def test_followup_playwright_totp_matches_pyotp():
    """e2e/tests/totp.ts must produce the same code as the backend's pyotp for the same secret + time."""
    import json, shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    import pyotp
    secret = pyotp.random_base32()
    now_ms = 1_750_000_000_000
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    ts = open(os.path.join(root, "e2e", "tests", "totp.ts")).read()
    js = ts.replace('import { createHmac } from "node:crypto";', 'const { createHmac } = require("node:crypto");')
    js = js.replace("export function", "function").replace(": Buffer", "").replace(": string", "").replace(": number[]", "")
    js += f'\nprocess.stdout.write(JSON.stringify(totpCode({json.dumps(secret)}, {now_ms})));\n'
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == pyotp.TOTP(secret).at(now_ms // 1000)
