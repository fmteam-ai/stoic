"""main99 review — Phase 2 (during the demo): A14-1 env templates, A14-2 fenced lease, N99-1 scalp
filters, N99-3 transition-only audit + admin clear, N99-6 seed groups, N99-7/N99-8 provenance, SEC-002."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── A14-1 ─────────────────────────────────────────────────────────────────────
def test_a14_1_dot_env_examples_are_not_tracked_and_every_reader_uses_deploy_env():
    tracked = subprocess.run(["git", "ls-files", ".env.example", "backend/.env.example"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    assert tracked == [], tracked
    gi = _src(".gitignore")
    assert "/.env.example" in gi and "/backend/.env.example" in gi and "!.env.example" not in gi
    assert "deploy/env/root.env.example deploy/env/backend.env.example" in _src(".github", "workflows", "release.yml")
    upd = _src("deploy", "update.sh")
    assert "sync_env_examples.py --check" in upd and upd.index("sync_env_examples.py") < upd.index("ensure_release_secrets || rollback")
    assert "env_templates_in_sync" in _src("scripts", "release_preflight.sh")
    assert 'deploy", "env", "backend.env.example' in _src("scripts", "check_compose_secrets.py")
    for rel in ("docs/RUNBOOK.md", "docs/SELF_HOSTING_GUIDE.md", "docs/DEPLOYMENT.md"):
        assert "deploy/env/" in _src(*rel.split("/")), rel
    # a fresh clone (no dot-files yet) is NOT drift
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import sync_env_examples as sync
    with patch.object(sync, "TEMPLATES", {os.path.join("deploy", "env", "root.env.example"): "nonexistent/.env.example"}):
        assert sync.main(["--check"]) == 0


# ── A14-2 ─────────────────────────────────────────────────────────────────────
def _caps():
    return {"auto_cap": 5, "total_cap": 0, "daily_cap": 0, "origin": "auto"}


def test_a14_2_fence_moves_on_every_acquisition_and_stale_holder_cannot_write():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    f1 = run(ar._acquire_lock(db, aid, "A"))
    assert f1 == 1
    # A pauses past its lease: expire it, B acquires → fence 2
    db.account_reservation_locks.rows[0]["locked_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    f2 = run(ar._acquire_lock(db, aid, "B"))
    assert f2 == 2 and db.account_reservation_locks.rows[0]["owner"] == "B"
    assert run(ar._lock_held(db, aid, "A", f1)) is False       # A's fence is stale
    assert run(ar._lock_held(db, aid, "B", f2)) is True
    assert run(ar._renew_lock(db, aid, "A", f1)) is False
    run(ar._release_lock(db, aid, "A", f1))                      # late release must not free B's lease
    assert db.account_reservation_locks.rows[0]["owner"] == "B"


def test_a14_2_reservation_write_rejected_when_lease_lost_and_caps_hold():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    calls = {"n": 0}
    real_held = ar._lock_held

    async def flaky_held(db_, account_id, owner, fence):
        calls["n"] += 1
        if calls["n"] == 2:                      # lease lost between the snapshot check and the write
            db.account_reservation_locks.rows[0]["fence"] += 1
            db.account_reservation_locks.rows[0]["owner"] = "B"
        return await real_held(db_, account_id, owner, fence)

    with patch.object(ar, "_lock_held", flaky_held):
        out = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="d1", caps=_caps(), symbol="XAUUSD"))
    assert out["ok"] is False and out["blocked"] == "reservation_lease_lost"
    active = [r for r in db.risk_reservations.rows if r.get("state") != "RELEASED"]
    assert active == []                                            # the write was undone — caps hold
    assert db.risk_reservations.rows and db.risk_reservations.rows[0]["release_reason"] == "lease_lost"
    # normal path still works and records the fence on the reservation
    db2 = FakeDb()
    ok = run(ar.reserve_entry(db2, account_id=aid, user_id="u", source="mt5", decision_id="d2", caps=_caps(), symbol="XAUUSD"))
    assert ok["ok"] and ok["reservation"]["lease_fence"] == 1
    assert db2.account_reservation_locks.rows[0]["owner"] is None   # released


def test_a14_2_slow_snapshot_renews_the_lease():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    slow = ar._now() + timedelta(seconds=ar.LOCK_TTL_SEC)       # pretend the snapshot took longer than TTL/2
    real_now = ar._now
    seq = iter([real_now(), slow, slow, slow, slow, slow, slow, slow, slow, slow])
    with patch.object(ar, "_now", lambda: next(seq, slow)), \
            patch.object(ar, "_renew_lock", AsyncMock(return_value=True)) as renew:
        out = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="d3", caps=_caps(), symbol="XAUUSD"))
    assert out["ok"] and renew.await_count >= 2                 # renewal after the slow snapshot + fence checks


# ── N99-1 ─────────────────────────────────────────────────────────────────────
def test_n99_1_scalp_trades_stay_origin_auto_with_engine_marker_and_separate_counter():
    import account_reservations as ar
    assert ar.trade_counter_filter("scalp") == {"origin": "auto", "engine": "scalp"}
    assert ar.trade_counter_filter("auto") == {"origin": "auto", "engine": {"$ne": "scalp"}}
    db = FakeDb()
    aid = str(ObjectId())
    today = datetime.now(timezone.utc).isoformat()
    db.trades.rows.append({"user_id": "u", "account_id": aid, "status": "open", "origin": "auto", "engine": "scalp", "symbol": "EURUSD", "opened_at": today})
    db.trades.rows.append({"user_id": "u", "account_id": aid, "status": "open", "origin": "auto", "symbol": "XAUUSD", "opened_at": today})
    main = run(ar.capacity_snapshot(db, account_id=aid, user_id="u", symbol="XAUUSD", counter_origin="auto"))
    scalp = run(ar.capacity_snapshot(db, account_id=aid, user_id="u", symbol="EURUSD", counter_origin="scalp"))
    assert main["auto_open"] == 1 and scalp["auto_open"] == 1 and main["total_open"] == 2 == scalp["total_open"]
    # every origin:"auto" safety filter now also sees scalp trades
    for rel, needle in (("circuit_breakers.py", '"origin": "auto"'), ("eod_flatten.py", '"origin": "auto"'),
                        ("entitlements.py", 'signal.get("origin") == "auto"')):
        assert needle in _src("backend", rel), rel
    assert '"engine": signal.get("engine")' in _src("backend", "execution.py")
    # scalp config is read and the account-wide total cap applies to scalp too
    db.bot_configs.rows.append({"user_id": "u", "account_id": aid, "max_concurrent_trades": 2,
                                "scalp": {"max_concurrent": 1, "max_trades_per_symbol_per_day": 4}})
    caps = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None, origin="scalp"))
    assert caps == {"auto_cap": 1, "total_cap": 2 + ar.total_positions_buffer(), "daily_cap": 4, "origin": "scalp"}


# ── N99-3 ─────────────────────────────────────────────────────────────────────
def test_n99_3_contradiction_audit_row_only_on_transition():
    import routes.bridge_routes as br
    db = FakeDb()
    acc = {"_id": ObjectId(), "mode": "live", "label": "D", "account_type": "demo", "server": "X-Demo", "broker_server": "X-Demo"}
    run(br._trade_mode_contradiction(db, acc, "real", "t0"))
    assert len(db.admin_audit_log.rows) == 1 and len(db.ops_alerts.rows) == 1
    acc["account_trade_mode"] = "real"                       # what the heartbeat stored
    for _ in range(5):
        run(br._trade_mode_contradiction(db, acc, "real", "t1"))
    assert len(db.admin_audit_log.rows) == 1                 # no row per heartbeat
    src = _src("backend", "routes", "admin_routes.py")
    assert "/admin/account-environments/{account_id}/clear-broker-mode" in src and "terminal_still_reports" in src
    jsx = _src("frontend", "src", "components", "admin", "AccountEnvironmentsPanel.jsx")
    assert "account-env-clear-broker-mode-" in jsx and "clear-broker-mode" in jsx


# ── N99-6 ─────────────────────────────────────────────────────────────────────
def test_n99_6_seed_index_groups_are_isolated():
    s = _src("backend", "seed.py")
    for g in ("_scalp_group", "_billing_group", "_auth_group", "_crypto_group", "_outbox_group"):
        assert f"async def {g}():" in s, g
    assert 'if _name == "scalp":' in s and 'if "scalp" not in _index_failures:' in s
    assert "SCALP SERVICE BLOCKED — unique index creation failed" not in s   # the single try-block is gone


# ── N99-7 / N99-8 ─────────────────────────────────────────────────────────────
def test_n99_7_broker_reconciled_only_when_ledger_gate_passes():
    import routes.performance_routes as pr
    db = FakeDb()
    uid = str(ObjectId())
    now = datetime.now(timezone.utc)
    db.accounts.rows.append({"_id": ObjectId(), "user_id": uid, "label": "A", "mode": "live", "status": "active",
                             "last_heartbeat": now.isoformat(), "last_reconciled_at": now.isoformat(), "reconciliation_seq": 1})
    db.broker_deals.rows.append({"user_id": uid, "account_id": "x", "deal_time": int(now.timestamp()), "profit": 5.0,
                                 "commission": 0, "swap": 0, "deal_entry": 1})
    with patch("broker_statement_ledger.ledger_gate", AsyncMock(return_value=["NO_RECONCILED_STATEMENT"])):
        out = run(pr._verified_payload(db, uid))
    assert out["provenance"]["source_kind"] == "derived" and out["share_allowed"] is False
    assert "NOT reconciled" in out["provenance"]["note"]
    with patch("broker_statement_ledger.ledger_gate", AsyncMock(return_value=[])):
        out = run(pr._verified_payload(db, uid))
    assert out["provenance"]["source_kind"] == "broker_reconciled"
    pamm = _src("backend", "modules", "pamm", "api", "__init__.py")
    assert 'source_kind="broker_reconciled" if not reasons else "derived"' in pamm


def test_n99_8_simulated_labels_claims_wording_turnstile_guard_marquee_and_status_requirements():
    assert "strategies-backtest-provenance" in _src("frontend", "src", "pages", "Strategies.jsx")
    assert "risk-commander-backtest-provenance" in _src("frontend", "src", "pages", "RiskCommander.jsx")
    assert "bot-health-session-provenance" in _src("frontend", "src", "pages", "BotHealth.jsx")
    import glob
    for p in glob.glob(os.path.join(ROOT, "frontend", "src", "pages", "*.jsx")):
        assert "RISK-CONTROLLED AUTOMATED TRADING" not in open(p, encoding="utf-8").read(), p
    adm = _src("backend", "routes", "admin_routes.py")
    assert "turnstile_hostnames_required" in adm and "is_production() and not expected_hostnames()" in adm
    tst = _src("frontend", "src", "components", "LandingTestimonials.jsx")
    assert "onKeyDown" in tst and 'data-paused' in tst and "tabIndex={0}" in tst
    assert '.tst-marquee[data-paused="true"] .tst-track' in _src("frontend", "src", "styles", "intro_trailer.css")
    assert '"requirements"' in _src("backend", "routes", "portal_routes.py")
    assert "status-requirements" in _src("frontend", "src", "pages", "StatusPage.jsx")


def test_sec_002_no_raw_exception_text_in_fixed_routes():
    assert "bundle_unavailable" in _src("backend", "routes", "admin_routes.py")
    assert "action_not_found" in _src("backend", "routes", "security_agent_routes.py")
    assert "passkey_registration_failed" in _src("backend", "routes", "webauthn_routes.py")
    assert 'detail=f"Passkey registration failed: {e}"' not in _src("backend", "routes", "webauthn_routes.py")
