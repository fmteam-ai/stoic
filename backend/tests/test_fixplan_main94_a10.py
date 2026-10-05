"""main94 review — step A10: CI setup, R-1 rollback restores data, R-5 distinct late fills +
admin brake list, R-3 pairing grace, token shown once at creation, P1-02 WS re-validation,
R-6 netting volume-limited close, wording. Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── CI setup ─────────────────────────────────────────────────────────────────
def test_ci_setup_fixes_present():
    assert 'os.environ.setdefault("BRIDGE_TOKEN_HASH_KEY"' in _read("backend", "tests", "unit", "test_security_agent_sa4.py")
    src = _read("scripts", "check_compose_secrets.py")
    assert "--env-file" in src and ".env.example" in src and "ci-stub" in src
    assert "pip install -q cryptography requests" in _read(".github", "workflows", "ea-release.yml")


def test_compose_config_stubs_both_env_files_in_ci_and_cleans_up(tmp_path, monkeypatch):
    """CI checkout: neither ./.env nor backend/.env exists — compose `env_file: backend/.env` must
    still resolve, and no stub may survive the run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ccs", os.path.join(ROOT, "scripts", "check_compose_secrets.py"))
    ccs = importlib.util.module_from_spec(spec); spec.loader.exec_module(ccs)
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env.example").write_text("DB_NAME=\nMONGO_ROOT_USER=\n")
    (tmp_path / "backend" / ".env.example").write_text("APP_ENV=\nJWT_SECRET=\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    fake_bin = tmp_path / "bin"; fake_bin.mkdir()
    probe = tmp_path / "probe.txt"
    (fake_bin / "docker").write_text(
        "#!/bin/sh\n"
        f"test -f '{tmp_path}/backend/.env' || exit 7\n"
        "while [ $# -gt 0 ]; do if [ \"$1\" = --env-file ]; then shift; test -f \"$1\" || exit 8; "
        f"grep -q 'DB_NAME=ci-stub' \"$1\" || exit 9; fi; shift; done\n"
        f"cat '{tmp_path}/backend/.env' > '{probe}'\n")
    os.chmod(fake_bin / "docker", 0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setattr(ccs, "ROOT", str(tmp_path))
    r = ccs._compose_config()
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "APP_ENV=ci-stub" in probe.read_text() and "JWT_SECRET=ci-stub" in probe.read_text()
    assert not (tmp_path / "backend" / ".env").exists()                       # stub removed
    assert not [p for p in os.listdir("/tmp") if p.endswith(".env") and p.startswith("tmp") and os.path.getmtime(os.path.join("/tmp", p)) > time.time() - 2]


# ── R-1 rollback restores the pre-update database ────────────────────────────
def test_update_sh_rollback_restores_pre_update_backup():
    src = _read("deploy", "update.sh")
    assert 'STOIC_UPDATE_BACKUP="${PRE_BACKUP}"' in src                     # archive handed across the re-exec
    body = src[src.index("rollback() {"):]
    body = body[:body.index("\n}\n")]
    assert body.index("UPDATE_HOLD_ON_FAILURE") < body.index("git checkout --detach")   # hold still wins
    assert 'deploy/backup.sh restore "${PRE_BACKUP}"' in body
    assert body.index('deploy/backup.sh restore "${PRE_BACKUP}"') < body.index("compose_up")
    assert "UPDATE_ROLLBACK_RESTORE_DB" in body and "deploy/backup.sh backup" in body     # safety dump first


# ── R-5 brake: distinct trades, admin list ────────────────────────────────────
def _acc(**kw):
    return {"_id": ObjectId(), "user_id": None, "label": "Demo", **kw}


def test_late_fill_reported_twice_counts_once_and_brake_needs_two_trades():
    import execution_health as eh
    db = FakeDb()
    a = _acc(); db.accounts.rows.append(a)
    aid = str(a["_id"])
    now = datetime.now(timezone.utc)
    for i in range(3):   # the SAME trade reported three times (PANIC re-dispatch)
        db.execution_health_events.rows.append({"account_id": aid, "kind": "late_fill", "trade_id": "t1",
                                                "at": (now - timedelta(minutes=i)).isoformat(), "detail": ""})
    db.execution_health_events.rows.append({"account_id": aid, "kind": "late_fill", "trade_id": None,
                                            "at": (now - timedelta(minutes=9)).isoformat(), "detail": "no id"})
    ev = run(eh.window_events(db, aid))
    assert [e.get("trade_id") for e in ev] == ["t1", None]        # 3× t1 → 1, id-less entry counts once
    assert run(eh.evaluate(db, aid, acc=a)) is not None             # 2 distinct = threshold → brake
    assert eh.distinct_by_trade([{"trade_id": "x"}, {"trade_id": "x"}, {"trade_id": "y"}]) == [{"trade_id": "x"}, {"trade_id": "y"}]


def test_single_trade_double_report_does_not_brake():
    import execution_health as eh
    db = FakeDb()
    a = _acc(); db.accounts.rows.append(a)
    aid = str(a["_id"])
    now = datetime.now(timezone.utc).isoformat()
    db.execution_health_events.rows += [{"account_id": aid, "kind": "late_fill", "trade_id": "t1", "at": now, "detail": ""}] * 2
    assert run(eh.evaluate(db, aid, acc=a)) is None
    assert not eh.is_braked(db.accounts.rows[0])


def test_admin_brake_list_covers_every_owner():
    import execution_health as eh
    from routes import admin_routes
    db = FakeDb()
    u1 = {"_id": ObjectId(), "email": "one@x.io"}; u2 = {"_id": ObjectId(), "email": "two@x.io"}
    db.users.rows += [u1, u2]
    db.accounts.rows += [
        _acc(user_id=str(u1["_id"]), label="A", execution_brake={"active": True, "since": "2026-06-01T00:00:00+00:00", "reason": "r"}),
        _acc(user_id=str(u2["_id"]), label="B", execution_brake={"active": True, "since": "2026-06-02T00:00:00+00:00", "reason": "r"}),
        _acc(user_id=str(u2["_id"]), label="C", execution_brake={"active": False}),
    ]
    rows = run(eh.braked_accounts(db))
    assert [r["label"] for r in rows] == ["B", "A"] and rows[0]["owner_email"] == "two@x.io"
    src = inspect.getsource(admin_routes.admin_execution_brakes)
    assert "_admin_only(user)" in src and "braked_accounts" in src
    banner = _read("frontend", "src", "components", "ExecutionBrakeBanner.jsx")
    assert '"/admin/execution-brakes"' in banner and "owner_email" in banner


# ── R-3 pairing keeps the running EA alive ───────────────────────────────────
def test_pairing_prev_token_grace_until_first_new_heartbeat_capped_24h():
    from routes import setup_routes
    assert setup_routes.PAIRING_PREV_TOKEN_GRACE_H == 24
    assert "timedelta(hours=PAIRING_PREV_TOKEN_GRACE_H)" in inspect.getsource(setup_routes)
    # first use of the NEW token still retires the previous one immediately (S2)
    from routes import bridge_routes
    assert "await bt.retire_prev(db, acc)" in inspect.getsource(bridge_routes._account_by_token)


# ── A10-5 token shown once at creation ───────────────────────────────────────
def test_new_live_account_returns_token_once_paper_does_not():
    from routes import account_routes
    src = inspect.getsource(account_routes.create_account)
    assert 'out["bridge_token"] = _new_token' in src and "if not is_paper:" in src
    assert 'out["bridge_token_shown_once"] = True' in src
    acc_ui = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "data?.bridge_token" in acc_ui and "[data.id]: data.bridge_token" in acc_ui


# ── P1-02 open WebSockets die with the session ───────────────────────────────
def test_ws_closes_after_session_revocation():
    import server
    import auth
    db = FakeDb()
    u = {"_id": ObjectId(), "email": "ws@x.io", "role": "user", "status": "active"}
    db.users.rows.append(u)
    db.auth_sessions.rows.append({"session_id": "ws-sid", "revoked": False, "user_id": str(u["_id"])})
    client = TestClient(server.app)
    with patch.dict(os.environ, {"JWT_SECRET": "unit-secret", "CORS_ORIGINS": ""}), \
            patch.object(server, "get_db", lambda: db), patch.object(server, "WS_REVALIDATE_SECONDS", 1):
        tok = auth.create_access_token(str(u["_id"]), u["email"], sid="ws-sid")
        client.cookies.set("access_token", tok)
        t0 = time.monotonic()
        with client.websocket_connect("/api/ws", headers={"origin": "http://testserver", "host": "testserver"}) as ws:
            assert ws.receive_json()["type"] == "connected"
            db.auth_sessions.rows[0]["revoked"] = True          # logout / revoke-all / password change
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401
        assert time.monotonic() - t0 < 10


# ── R-6 netting: volume-limited close ────────────────────────────────────────
def test_netting_full_close_becomes_volume_limited_partial_close():
    from routes.bridge_routes import netting_close_command
    t = {"_id": ObjectId(), "mt5_ticket": 7, "symbol": "XAUUSD", "lot_size": 0.10, "live_volume": 0.30}
    m = {"type": "FULL_CLOSE", "intent_id": "i1", "seq": 3, "reason": "slippage_veto"}
    cmd = netting_close_command(t, m)
    assert cmd["type"] == "PARTIAL_CLOSE" and cmd["new_volume"] == 0.2 and cmd["close_volume"] == 0.1
    assert cmd["netting_volume_limited"] is True and cmd["intent_id"] == "i1" and cmd["seq"] == 3
    # the position is ours alone (or unknown) → plain FULL_CLOSE
    assert netting_close_command({**t, "live_volume": 0.10}, m)["type"] == "FULL_CLOSE"
    assert netting_close_command({**t, "live_volume": None}, m)["type"] == "FULL_CLOSE"
    # other commands untouched
    assert netting_close_command(t, {"type": "MODIFY_SL", "new_sl": 1.0})["type"] == "MODIFY_SL"


def test_netting_ack_maps_back_to_full_close_and_heartbeat_records_volume():
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes.modification_ack)
    assert 'payload.type == "PARTIAL_CLOSE" and _pm.get("type") == "FULL_CLOSE"' in src
    assert 'payload.type = "FULL_CLOSE"' in src and 'update["netting_volume_limited_close"] = True' in src
    hb = inspect.getsource(bridge_routes.heartbeat)
    assert '"live_volume": float(p.volume)' in hb
    poll = inspect.getsource(bridge_routes.poll_trades)
    assert "is_netting_account(db, acc)" in poll and "netting_close_command(t, m)" in poll


# ── Wording / UI ─────────────────────────────────────────────────────────────
def test_wording_and_admin_only_release_card():
    trailer = _read("frontend", "src", "pages", "WelcomeTrailer.jsx")
    for phrase in ("Zero excuses", "before it ever hurts you", "only the proven ones ever touch real money", "24/7"):
        assert phrase not in trailer, phrase
    assert "Past results never guarantee future ones" in trailer
    settings = _read("frontend", "src", "pages", "Settings.jsx")
    assert "stay signed in" not in settings and "logout()" in settings
    assert "stay signed in" not in _read("frontend", "src", "pages", "FAQ.jsx")
    assert 'user?.role === "admin" && <ReleaseIdentityCard />' in _read("frontend", "src", "pages", "BotHealth.jsx")
    assert "TRUST_CF_CONNECTING_IP" in _read("deploy", "install.sh")
