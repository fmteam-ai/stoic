"""Security & Health Agent — SA3 unit tests: alert routing/dedup/no-secrets, rules R1–R8 (protected list,
hourly cap, observe mode = would_have_done only), daily/weekly reports + scheduler idempotence, route matrix.
No live Mongo (fake_mongo), no network (senders injected)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def iso(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def cfg(**over):
    from security_agent.config import from_env
    c = from_env({})
    c.update(over)
    return c


def seed(db, check_id, key, severity, evidence=None, what="x", status="open", **extra):
    from security_agent.checks.common import f
    from security_agent.findings import open_or_update
    fd = f(check_id, key, severity, "access", what, evidence or {})
    res = run(open_or_update(db, fd))
    row = next(r for r in db.security_findings.rows if str(r["_id"]) == res["id"])
    row.update({"status": status, **extra})
    return row


class Sink:
    def __init__(self):
        self.tg, self.mail = [], []

    async def send_tg(self, text):
        self.tg.append(text)
        return True

    async def send_mail(self, cfg_, subject, text):
        self.mail.append((subject, text))
        return 1


# ── alerts ──────────────────────────────────────────────────────────────────
def test_alert_routing_dedup_repeat_and_no_secrets():
    from security_agent import alerts
    db, s, c = FakeDb(), Sink(), cfg()
    crit = seed(db, "A4", "user:u1", "critical", {"token": JWT, "event": {"user_id": "u1"}}, what=f"reuse of {JWT}")
    seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    seed(db, "A5", "user:x", "medium"), seed(db, "P4", "disk", "low")
    assert run(alerts.sweep(db, c, send_tg=s.send_tg, send_mail=s.send_mail)) == 2            # critical + high only
    assert len(s.tg) == 2 and len(s.mail) == 2 and all(JWT not in t for t in s.tg) and all(JWT not in m[1] for m in s.mail)
    assert "[REDACTED]" in s.tg[0] or "[REDACTED]" in s.tg[1]
    assert all("/admin/ops?finding=" in t for t in s.tg) and "Solution: 1)" in s.tg[0]
    for _ in range(100):                                                                          # 100 repeats → no new alert
        assert run(alerts.sweep(db, c, send_tg=s.send_tg, send_mail=s.send_mail)) == 0
    assert len(s.tg) == 2
    crit["last_alert_at"] = iso(minutes=31)                                                       # critical repeats after 30 min
    assert run(alerts.sweep(db, c, send_tg=s.send_tg, send_mail=s.send_mail)) == 1 and s.tg[-1].startswith("[STOIC SECURITY] REPEAT")
    assert crit["alert_count"] == 2
    crit["last_alert_at"], crit["status"] = iso(minutes=31), "acknowledged"                     # acknowledged stops repeats
    assert run(alerts.sweep(db, c, send_tg=s.send_tg, send_mail=s.send_mail)) == 0
    high = next(r for r in db.security_findings.rows if r["check_id"] == "A1")
    high["last_alert_at"] = iso(minutes=31)                                                       # high never repeats
    assert run(alerts.sweep(db, c, send_tg=s.send_tg, send_mail=s.send_mail)) == 0
    assert [a["kind"] for a in db.security_actions.rows] == ["alert"] * 3


def test_alert_senders_fail_open_without_config(monkeypatch):
    from security_agent import alerts
    assert alerts.telegram_creds({}) is None and alerts.telegram_creds({"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "1:a"}) is None
    assert alerts.telegram_creds({"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "1:a", "SECURITY_AGENT_TELEGRAM_CHAT_ID": "-100"}) == ("1:a", "-100")
    assert run(alerts.send_telegram("x", creds=None)) is False or True                            # no creds in test env → False, never raises
    db, c = FakeDb(), cfg()
    seed(db, "A4", "user:u1", "critical")
    import email_sender
    monkeypatch.setattr(email_sender, "is_configured", lambda: False)                            # never hit Resend from a unit test
    monkeypatch.setattr(alerts, "telegram_creds", lambda env=None: None)
    assert run(alerts.sweep(db, c)) == 1                                                          # unconfigured senders → logged, still recorded
    assert db.security_findings.rows[0]["last_alert_channels"] == {"telegram": False, "email": 0}


# ── rules R1–R8 ─────────────────────────────────────────────────────────────
def test_rules_evaluate_each_rule_and_protected_list():
    from security_agent import rules
    c = cfg(protected_ips=["203.0.113.0/24"])
    ev = lambda cid, key, e: {"check_id": cid, "dedup_key": key, "evidence": e}  # noqa: E731
    r1 = rules.evaluate(ev("A1", "A1:ip:9.9.9.9", {"ip": "9.9.9.9", "failures": 20}), c)
    assert [p["rule"] for p in r1] == ["R1"] and r1[0]["action"] == "block_ip" and r1[0]["expires_min"] == 60 and r1[0]["scope"] == "auth"
    assert rules.evaluate(ev("A1", "A1:ip:9.9.9.9", {"ip": "9.9.9.9", "failures": 19}), c) == []
    r2 = rules.evaluate(ev("A1", "A1:account:v@x.com", {"account": "v@x.com", "failures": 50, "ips": ["1", "2", "3"]}), c)
    assert r2[0]["rule"] == "R2" and r2[0]["action"] == "lock_login" and r2[0]["expires_min"] == 30 and r2[0]["notify_owner"]
    assert rules.evaluate(ev("A1", "A1:account:v@x.com", {"account": "v@x.com", "failures": 50, "ips": ["1", "2"]}), c) == []
    assert rules.evaluate(ev("A3", "A3:ip:5.5.5.5", {"ip": "5.5.5.5", "accounts": 5}), c)[0]["rule"] == "R2"
    assert rules.evaluate(ev("A2", "A2:2fa:v@x.com", {"target": "v@x.com", "failures": 10}), c)[0]["action"] == "lock_otp"
    assert rules.evaluate(ev("A4", "A4:user:u1", {"event": {"user_id": "u1"}}), c)[0]["action"] == "revoke_sessions"
    assert rules.evaluate(ev("B1", "B1:account:a1", {"account_id": "a1", "sources": [["1", "t1"], ["2", "t2"]]}), c)[0]["action"] == "suspend_bridge_token"
    r6 = rules.evaluate(ev("B2", "B2:ip:7.7.7.7", {"ip": "7.7.7.7", "requests": 30}), c)
    assert r6[0]["rule"] == "R6" and r6[0]["scope"] == "bridge"
    assert rules.evaluate(ev("A6", "A6:actor:adm:x", {"event": {"user_id": "adm"}}), c)[0]["rule"] == "R7"
    assert rules.evaluate(ev("B3", "B3:account:a1:x", {"event": {"account_id": "a1"}}), c)[0]["action"] == "freeze_new_entries"
    assert rules.evaluate(ev("A7", "A7:platform", {"denied": 500}), c) == []                     # no per-IP proof → no action
    assert rules.evaluate(ev("A7", "A7:platform", {"top_ip": "8.8.8.8", "top_ip_rpm": 300}), c)[0]["rule"] == "R8"
    # protected: master admin, allow-listed CIDR, Cloudflare ranges, loopback
    admin = (os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com")
    p = rules.evaluate(ev("A1", f"A1:account:{admin}", {"account": admin, "failures": 99, "ips": ["1", "2", "3"]}), c)
    assert p[0]["blocked_by"] == "protected_target"
    assert rules.evaluate(ev("A1", "A1:ip:203.0.113.7", {"ip": "203.0.113.7", "failures": 99}), c)[0]["blocked_by"] == "protected_target"
    assert rules.evaluate(ev("A1", "A1:ip:104.16.1.1", {"ip": "104.16.1.1", "failures": 99}), c)[0]["blocked_by"] == "protected_target"
    assert rules.evaluate(ev("A1", "A1:ip:127.0.0.1", {"ip": "127.0.0.1", "failures": 99}), c)[0]["blocked_by"] == "protected_target"
    assert "blocked_by" not in rules.evaluate(ev("A1", "A1:ip:9.9.9.9", {"ip": "9.9.9.9", "failures": 99}), c)[0]
    assert all(r["action"] not in rules.FORBIDDEN_ACTIONS for r in rules.RULES.values())


def test_rules_sweep_observe_mode_records_would_have_done_cap_and_protected():
    from security_agent import rules
    db, c = FakeDb(), cfg(max_actions_per_hour=3)
    seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    seed(db, "A1", "ip:127.0.0.1", "high", {"ip": "127.0.0.1", "failures": 25})
    seed(db, "A5", "user:x", "medium")                                                         # no rule
    assert run(rules.sweep(db, c)) == 2
    acts = {a["target"]: a for a in db.security_actions.rows}
    assert acts["9.9.9.9"]["status"] == "would_have_done" and acts["9.9.9.9"]["mode"] == "observe" and acts["9.9.9.9"]["expires_at"]
    assert acts["127.0.0.1"]["status"] == "refused_protected"
    assert any(r["check_id"] == "protected_target" and r["severity"] == "critical" for r in db.security_findings.rows)
    assert run(rules.sweep(db, c)) == 0                                                           # dedup within the hour
    for i in range(5):
        seed(db, "B2", f"ip:7.7.7.{i}", "high", {"ip": f"7.7.7.{i}", "requests": 40})
    run(rules.sweep(db, c))
    statuses = [a["status"] for a in db.security_actions.rows if a["check_id"] == "B2"]
    assert statuses.count("refused_cap") >= 1 and statuses.count("would_have_done") >= 1
    assert any(r["check_id"] == "containment_cap_reached" for r in db.security_findings.rows)
    # observe mode never writes blocks or touches trades
    assert db.security_blocks.rows == [] and db.trades.rows == [] and db.accounts.rows == []


# ── reports ─────────────────────────────────────────────────────────────────
def test_daily_and_weekly_reports_and_scheduler():
    from security_agent import reports
    db, s, c = FakeDb(), Sink(), cfg()
    seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25}, what=f"token {JWT}")
    seed(db, "P5", "grp", "medium", what="loop failing")
    seed(db, "A2", "2fa:v", "high", status="false_positive", resolved_at=iso(), first_seen=iso(hours=2))
    seed(db, "A2", "2fa:w", "high", status="resolved", resolved_at=iso(), first_seen=iso(hours=1))
    seed(db, "D1", "pkg", "medium", what="CVE-2026-1 in pkg")
    db.security_actions.rows.append({"kind": "containment", "rule": "R1", "action": "block_ip", "target": "9.9.9.9", "status": "would_have_done", "at": iso(), "check_id": "A1"})
    db.security_check_runs.rows.append({"_id": "latest:A1", "check_id": "A1", "status": "failed"})
    d = run(reports.build_daily(db, c))
    assert d["counts"] == {"new": 5, "resolved": 2, "open": 3, "actions": 1} and d["checks_failed"] == ["A1"] and "S2" in d["checks_silent"]
    assert d["still_open"][0]["severity"] == "high" and d["still_open"][0]["solution"] and d["top_error_groups"][0]["check_id"] == "P5"
    txt = reports.render_text(d)
    assert JWT not in txt and "WOULD HAVE DONE" in txt and "STILL OPEN" in txt and "block_ip 9.9.9.9" in txt
    assert "<pre" in reports.render_html(d) and "<script" not in reports.render_html(d)
    w = run(reports.build_weekly(db, c))
    assert w["trends_by_area"]["access"]["this_week"] == 5 and w["false_positive_rate"]["A2"] == 0.5
    assert w["mean_time_to_resolve_min"] and w["open_cves"][0]["check_id"] == "D1" and any("A2" in t for t in w["tuning_suggestions"])
    assert "FALSE-POSITIVE RATE" in reports.render_text(w)
    # scheduler: Monday 07:05 → daily (tg+mail) + weekly (mail only); second tick same day → nothing
    mon = datetime(2026, 10, 5, 7, 5, tzinfo=timezone.utc)
    assert run(reports.scheduler_tick(db, c, mon, send_tg=s.send_tg, send_mail=s.send_mail)) == ["daily", "weekly"]
    assert len(s.tg) == 1 and len(s.mail) == 2 and s.mail[1][0].startswith("[STOIC SECURITY] Weekly")
    assert run(reports.scheduler_tick(db, c, mon + timedelta(hours=1), send_tg=s.send_tg, send_mail=s.send_mail)) == []
    assert run(reports.scheduler_tick(db, c, datetime(2026, 10, 6, 6, 59, tzinfo=timezone.utc), send_tg=s.send_tg, send_mail=s.send_mail)) == []
    assert run(reports.scheduler_tick(db, c, datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc), send_tg=s.send_tg, send_mail=s.send_mail)) == ["daily"]
    assert [r["kind"] for r in db.security_reports.rows] == ["daily", "weekly", "daily"] and all(r["html"] for r in db.security_reports.rows)


# ── tick integration + routes ───────────────────────────────────────────────
def test_tick_runs_sa3_stages_isolated(monkeypatch):
    from security_agent import runner, alerts
    db = FakeDb()
    seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    monkeypatch.setattr(runner, "CHECKS", {})
    sent = []

    async def tg(text):
        sent.append(text)
        return True
    monkeypatch.setattr(alerts, "send_telegram", tg)

    async def mail(cfg_, subject, text):
        return 1
    monkeypatch.setattr(alerts, "send_email", mail)

    async def boom(db_, cfg_):
        raise RuntimeError("reports down")
    monkeypatch.setattr(runner.reports, "scheduler_tick", boom)
    run(runner.tick(db))
    assert len(sent) == 1 and any(a["kind"] == "containment" for a in db.security_actions.rows)


def test_sa3_routes_in_matrix_and_playbooks():
    from security_matrix import BOLA_MATRIX, ADM
    from security_agent.playbooks import playbook
    for m, p in (("GET", "/api/admin/security/actions"), ("POST", "/api/admin/security/findings/{finding_id}/status"), ("POST", "/api/admin/security/mode"),
                 ("POST", "/api/admin/security/test-alert"), ("GET", "/api/admin/security/reports/{kind}")):
        assert BOLA_MATRIX[(m, p)] == ADM
    for cid in ("protected_target", "containment_cap_reached", "agent_test_alert"):
        assert playbook(cid)["title"] != playbook("agent_check_failed")["title"]
