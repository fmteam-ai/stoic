"""Security & Health Agent — SA1/SA2 unit tests: findings dedup/status/auto-resolve, redaction,
each check raises on seeded bad data and stays quiet on normal data, failing check → agent_check_failed,
config precedence, admin-only routes registered in the matrix. No live Mongo (fake_mongo)."""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def iso(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


CFG = __import__("security_agent.config", fromlist=["from_env"]).from_env({})


# ── findings ────────────────────────────────────────────────────────────────
def test_findings_dedup_status_and_auto_resolve():
    from security_agent import findings as F
    db = FakeDb()
    fd = F.build("A1", "A1:ip:1.2.3.4", severity="high", area="access", title="x", what_happened="20 fails", evidence={"ip": "1.2.3.4"})
    a = run(F.open_or_update(db, fd))
    b = run(F.open_or_update(db, {**fd, "severity": "critical", "what_happened": "40 fails"}))
    assert a["created"] and not b["created"] and a["id"] == b["id"]
    row = db.security_findings.rows[0]
    assert row["occurrences"] == 2 and row["severity"] == "critical" and row["status"] == "open" and row["what_happened"] == "40 fails"
    assert row["expires_at"] - datetime.now(timezone.utc) > timedelta(days=179)      # 180-day retention
    assert run(F.set_status(db, row["_id"], "acknowledged", "adm")) is True
    assert run(F.auto_resolve_cleared(db, "A1", set())) == 1 and row["status"] == "resolved" and row["resolved_by"] == "security_agent"
    c = run(F.open_or_update(db, fd))
    assert c["created"] and c["id"] != a["id"]                                          # resolved → a new record next time
    with pytest.raises(ValueError):
        F.build("A1", "k", severity="urgent", area="access", title="t", what_happened="w")
    with pytest.raises(ValueError):
        run(F.set_status(db, row["_id"], "deleted", "adm"))                            # append-only: no delete state


# ── redaction ───────────────────────────────────────────────────────────────
def test_redaction_masks_every_secret_class_in_text_evidence_and_logs():
    from security_agent import redact
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    tg = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
    txt = f"login password=Hunter2Secret! token: {jwt} bot {tg} key sk_live_abcdefghijklmnopqrstuv bridge A6-{'f' * 24} hash {'a' * 40}"
    out = redact.mask(txt)
    for secret in ("Hunter2Secret!", jwt, tg, "sk_live_abcdefghijklmnopqrstuv", "f" * 24, "a" * 40):
        assert secret not in out
    assert "password=[REDACTED]" in out and out.count("[REDACTED]") >= 5
    assert redact.mask("normal trade closed at 4001.5 for user 42")== "normal trade closed at 4001.5 for user 42"
    ev = redact.mask_obj({"lines": [f"Authorization: Bearer {jwt}"], "n": 3, "nested": {"pw": "secret=abcdef123"}})
    assert jwt not in str(ev) and ev["n"] == 3 and "abcdef123" not in str(ev)
    log = logging.getLogger("sa.test")
    log.addFilter(redact.RedactingFilter())
    records = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    log.addHandler(H())
    log.setLevel(logging.INFO)
    redact.hits(reset=True)
    log.info("refresh %s for user", jwt)
    assert records and jwt not in records[-1] and "[REDACTED]" in records[-1]
    assert redact.hits().get("jwt", 0) >= 1


# ── checks: fire on seeded bad data, quiet on normal data ───────────────────
def _rl(db, key, n):
    db.rate_limits.rows.append({"_id": key, "n": n, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)})


def test_access_checks_fire_and_stay_quiet():
    from security_agent.checks import access as A
    db = FakeDb()
    assert run(A.A1(db, CFG)) == [] and run(A.A2(db, CFG)) == [] and run(A.A3(db, CFG)) == [] and run(A.A4(db, CFG)) == [] and run(A.A6(db, CFG)) == []
    _rl(db, "login:9.9.9.9:victim@x.com:1700000000", 25)
    out = run(A.A1(db, CFG))
    assert [f["dedup_key"] for f in out] == ["A1:ip:9.9.9.9"] and out[0]["severity"] == "high" and "25 failed logins" in out[0]["what_happened"]
    for i in range(6):
        _rl(db, f"login:9.9.9.9:u{i}@x.com:1700000000", 2)
    assert run(A.A3(db, CFG))[0]["dedup_key"] == "A3:ip:9.9.9.9"
    _rl(db, "2fa:victim@x.com:1700000000", 12)
    assert run(A.A2(db, CFG))[0]["dedup_key"] == "A2:2fa:victim@x.com"
    db.security_events.rows.append({"kind": "refresh_token_reuse", "at": iso(), "ip": "1.1.1.1", "detail": {"user_id": "u1"}, "processed": False})
    db.security_events.rows.append({"kind": "step_up_missing", "at": iso(), "ip": "1.1.1.1", "detail": {"user_id": "adm", "action": "panic_release"}, "processed": False})
    a4, a6 = run(A.A4(db, CFG)), run(A.A6(db, CFG))
    assert a4[0]["severity"] == "critical" and a6[0]["severity"] == "critical" and "panic_release" in a6[0]["what_happened"]
    assert all(e["processed"] for e in db.security_events.rows)
    assert run(A.A4(db, CFG)) and run(A.A4(db, CFG))      # same key → dedup handled by findings layer, check is idempotent


def test_bridge_secrets_deps_checks():
    from security_agent.checks import bridge_secrets_deps as B
    db = FakeDb()
    assert run(B.B1(db, CFG)) == [] and run(B.B2(db, CFG)) == [] and run(B.B3(db, CFG)) == [] and run(B.D1(db, CFG)) == []
    db.accounts.rows.append({"_id": "acc1", "label": "L", "hb_sightings": [
        {"ip": "1.1.1.1", "terminal": "inst-a", "at": iso(seconds=30)}, {"ip": "2.2.2.2", "terminal": "inst-b", "at": iso(seconds=20)},
        {"ip": "1.1.1.1", "terminal": "inst-a", "at": iso(seconds=10)}]})
    b1 = run(B.B1(db, CFG))
    assert b1 and b1[0]["severity"] == "critical" and b1[0]["dedup_key"] == "B1:account:acc1"
    for _ in range(31):
        db.security_events.rows.append({"kind": "bridge_invalid_token", "at": iso(), "ip": "7.7.7.7", "detail": {}, "processed": False})
    assert run(B.B2(db, CFG))[0]["dedup_key"] == "B2:ip:7.7.7.7"
    db.security_events.rows.append({"kind": "order_auth_invalid", "at": iso(), "ip": None, "detail": {"account_id": "acc1", "reason": "bad_signature"}, "processed": False})
    assert "bad_signature" in run(B.B3(db, CFG))[0]["what_happened"]
    db.platform_state.rows.append({"_id": "dependency_audit_python", "vulnerabilities": [
        {"package": "accelerate", "version": "1.14.0", "id": "PYSEC-2026-3804", "score": 7.5, "fix": None}]})
    d1 = run(B.D1(db, CFG))
    assert d1[0]["severity"] == "high" and d1[0]["dedup_key"] == "D1:accelerate:PYSEC-2026-3804"
    from unittest.mock import patch
    with patch.dict(os.environ, {"JWT_SECRET": "short", "DEBUG": "true", "CORS_ORIGINS": "*", "APP_ENV": "production"}):
        s2 = run(B.S2(db, CFG))
    keys = {f["dedup_key"] for f in s2}
    assert {"S2:weak:JWT_SECRET", "S2:debug:DEBUG", "S2:cors:wildcard"} <= keys and "short" not in str(s2)   # never the value
    from security_agent import redact
    redact.hits(reset=True)
    redact.mask("token=supersecretvalue123")
    s1 = run(B.S1(db, CFG))
    assert s1 and s1[0]["dedup_key"] == "S1:pattern:password_kv" and run(B.S1(db, CFG)) == []   # counter consumed


def test_integrity_platform_trading_checks():
    from security_agent.checks import integrity_platform_trading as I  # noqa: N812
    from unittest.mock import AsyncMock, patch
    db = FakeDb()
    assert run(I.P1(db, CFG)) == [] and run(I.T1(db, CFG)) == [] and run(I.T2(db, CFG)) == [] and run(I.P3(db, CFG)) == []
    db.ops_alerts.rows.append({"kind": "worker_loop_crashloop", "acked_at": None, "dedup_key": "w:protection", "message": "protection crash-looping"})
    db.worker_leases.rows.append({"_id": "trading", "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5)})
    p1 = run(I.P1(db, CFG))
    assert {f["dedup_key"] for f in p1} == {"P1:worker_loop_crashloop:w:protection", "P1:lease:trading"}
    for i in range(4):
        db.accounts.rows.append({"_id": f"a{i}", "mode": "live", "trading_enabled": True, "last_heartbeat": iso(minutes=10)})
    assert run(I.T1(db, CFG))[0]["dedup_key"] == "T1:platform"
    db.trades.rows.append({"status": "pending", "mt5_ticket": None, "_dispatched_at": iso(seconds=300)})
    assert "T2:unacked_orders" in {f["dedup_key"] for f in run(I.T2(db, CFG))}
    db.runtime_crash_log.rows.append({"at": iso(minutes=1), "blocked_for_s": 4.2, "process": "api"})
    assert run(I.P3(db, CFG))[0]["dedup_key"] == "P3:blocked:api"
    with patch("seed.duplicate_tickets", AsyncMock(return_value=[{"account_id": "a1", "mt5_ticket": 5, "count": 2}])):
        assert run(I.I1(db, CFG))[0]["dedup_key"] == "I1:account:a1:ticket:5"
    with patch("audit_chain.verify_chain", AsyncMock(return_value={"ok": False, "broken_at": 17})):
        i3 = run(I.I3(db, CFG))
    assert i3[0]["severity"] == "critical" and "17" in i3[0]["what_happened"]
    with patch("audit_chain.verify_chain", AsyncMock(return_value={"ok": True})):
        assert run(I.I3(db, CFG)) == []
    i2 = run(I.I2(db, CFG))                                   # fake db has no indexes → every required one is missing
    assert len(i2) == len(I.REQUIRED_INDEXES) and all(f["severity"] == "critical" for f in i2)


# ── runner: failing check → agent_check_failed; registry complete ───────────
def test_runner_isolates_failing_check_and_records_runs():
    from security_agent import runner
    from security_agent.checks import CHECKS
    assert len(CHECKS) == 28 and set(CHECKS) >= {"A1", "A7", "B3", "S5", "D2", "I4", "P5", "T2"}
    db = FakeDb()
    db.platform_state.rows.append({"_id": "security_agent", "mode": "enforce", "thresholds": {"A1": {"ip_fail_5m": 5}}})

    async def boom(db_, cfg):
        raise RuntimeError("token=verysecretvalue broke")
    run_ = run(runner.run_check(db, "A1", boom, CFG))
    assert run_["status"] == "failed"
    fd = db.security_findings.rows[0]
    assert fd["check_id"] == "agent_check_failed" and fd["dedup_key"] == "agent_check_failed:A1" and "verysecretvalue" not in str(fd)
    assert any(r.get("_id") == "latest:A1" for r in db.security_check_runs.rows)

    async def quiet(db_, cfg):
        return []
    run(runner.run_check(db, "A1", quiet, CFG))
    assert fd["status"] == "resolved"                           # the check runs clean again → failure finding auto-resolves
    from security_agent import config
    cfg = run(config.load(db))
    assert cfg["mode"] == "enforce" and cfg["thresholds"]["A1"]["ip_fail_5m"] == 5 and cfg["thresholds"]["A1"]["account_fail_10m"] == 50
    assert config.from_env({"SECURITY_AGENT_MODE": "bogus", "SECURITY_AGENT_RULES": "R1,R4", "SECURITY_AGENT_MAX_ACTIONS_PER_HOUR": "3"}) \
        .items() >= {"mode": "observe", "max_actions_per_hour": 3}.items()
    assert config.from_env({"SECURITY_AGENT_RULES": "R1,R4"})["rules_enabled"] == ["R1", "R4"]


def test_routes_are_admin_only_and_in_security_matrix():
    import inspect
    from fastapi import HTTPException
    import routes.security_agent_routes as r
    from security_matrix import BOLA_MATRIX, ADM
    paths = {p for (m, p) in BOLA_MATRIX if p.startswith("/api/admin/security/")}
    assert paths >= {"/api/admin/security/status", "/api/admin/security/findings", "/api/admin/security/findings/{finding_id}", "/api/admin/security/check-runs"}
    assert all(v == ADM for (m, p), v in BOLA_MATRIX.items() if p in paths)
    for fn in (r.security_status, r.list_findings, r.get_finding, r.check_runs, r.list_actions, r.finding_status, r.set_mode, r.test_alert, r.get_report):
        assert "require_admin(user)" in inspect.getsource(fn)
    for fn in (r.finding_status, r.set_mode, r.test_alert):                       # SA3 writes need step-up MFA
        assert "require_step_up(" in inspect.getsource(fn)
    with pytest.raises(HTTPException) as ei:
        run(r.get_finding("abc", user={"id": "u", "role": "user"}))
    assert ei.value.status_code == 403
    from security_agent.playbooks import PLAYBOOKS
    from security_agent.checks import CHECKS
    assert set(CHECKS) <= set(PLAYBOOKS)                        # every check has reviewed solution/verify text
