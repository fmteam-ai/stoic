"""main92 review — step SA5 (security agent fixes before enforce).
S1  private/docker/loopback ranges protected; shared-IP (proxy) refusal; CF-Connecting-IP only from Cloudflare; master admin login bypasses IP blocks.
S2  R6 ignores retired tokens / IPs that also heartbeat validly; threshold 100.
S3  R5 needs two installation ids or interleaving; suspension expires.
S4  R7 only for forged tokens; user target → sessions revoked + every live account frozen.
S5  log masking works on named loggers / late handlers / workers.
S7  is_blocked fails open with the last good cache.
S8  PANIC release re-applies the agent freeze from its own field.
S9  IPv6 identifiers split on the last ':'.
S10 contained findings not auto-resolved; expired containment resolves them.
S11 only successful deliveries count; failures retried.
S12 denied-response counts persisted; A7 reads them; blocks with scope "all" enforced by middleware.
S13 "no data" findings for D1/D2/I4/P5.
S14 Telegram token as a Docker secret on worker-security only.
S16 bad env value ignored with a warning; undo restores the exact prior.
Pure unit tests (fake async db) — run with DB_NAME="".
"""
import asyncio
import inspect
import logging
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


# ── S1 ──────────────────────────────────────────────────────────────────────
def test_s1_private_ranges_and_cloudflare_are_always_protected():
    from security_agent import rules
    for ip in ("127.0.0.1", "10.2.3.4", "172.18.0.5", "192.168.1.9", "100.64.0.1", "::1", "fd00::1", "104.16.1.1", "2606:4700::1"):
        assert rules.is_protected({}, "ip", ip), ip
    assert not rules.is_protected({}, "ip", "203.0.113.7")
    assert rules.is_cloudflare("173.245.48.9") and not rules.is_cloudflare("203.0.113.7")


def test_s1_cf_connecting_ip_trusted_only_from_cloudflare_hop():
    from security import client_ip

    class R:
        def __init__(self, headers, peer):
            self.headers, self.client = headers, type("C", (), {"host": peer})()
    with patch.dict(os.environ, {"TRUST_CF_CONNECTING_IP": "true"}):
        assert client_ip(R({"cf-connecting-ip": "198.51.100.5", "x-forwarded-for": "173.245.48.9"}, "10.0.0.2")) == "198.51.100.5"
        # direct hit on the origin with a forged header → the hop wins
        assert client_ip(R({"cf-connecting-ip": "198.51.100.5", "x-forwarded-for": "203.0.113.7"}, "10.0.0.2")) == "203.0.113.7"
    with patch.dict(os.environ, {"TRUST_CF_CONNECTING_IP": "false"}):
        assert client_ip(R({"cf-connecting-ip": "198.51.100.5"}, "203.0.113.8")) == "203.0.113.8"


def test_s1_block_ip_refused_when_one_ip_carries_most_traffic():
    from security_agent import actions
    db = FakeDb()
    now = datetime.now(timezone.utc).isoformat()
    for i in range(30):
        db.security_events.rows.append({"kind": "login_failed", "ip": "203.0.113.7" if i < 24 else f"198.51.100.{i}", "at": now})
    cfg = {"mode": "enforce", "rules_enabled": ["R1"], "max_actions_per_hour": 10}
    prop = {"rule": "R1", "action": "block_ip", "scope": "auth", "target_kind": "ip", "target": "203.0.113.7", "expires_min": 60, "undo": "unblock_ip"}
    fd = {"_id": ObjectId(), "dedup_key": "A1:ip:203.0.113.7", "check_id": "A1", "what_happened": "x"}
    row = run(actions.execute(db, cfg, prop, fd))
    assert row["status"] == "refused_shared_ip" and row["traffic_share"] >= 0.5
    assert not db.security_blocks.rows
    assert any(r.get("check_id") == "proxy_collapse_suspected" for r in db.security_findings.rows)


def test_s1_master_admin_login_bypasses_ip_block():
    import routes.auth_routes as ar
    src = inspect.getsource(ar.login)
    assert "master_admin = email ==" in src and 'if not master_admin:\n        await deny_if_blocked(db, "ip", ip, "auth")' in src
    assert 'await deny_if_blocked(db, "account_login", email)' in src       # account locks still apply (admin is protected anyway)


# ── S2 / S3 ─────────────────────────────────────────────────────────────────
def test_s2_b2_ignores_retired_tokens_and_valid_heartbeat_ips():
    from security_agent.checks.bridge_secrets_deps import B2
    db = FakeDb()
    now = datetime.now(timezone.utc).isoformat()
    db.security_events.rows += [{"kind": "bridge_invalid_token", "ip": "203.0.113.7", "at": now, "detail": {"count": 150}},
                                {"kind": "bridge_invalid_token", "ip": "203.0.113.8", "at": now, "detail": {"count": 150, "retired": True}},
                                {"kind": "bridge_invalid_token", "ip": "203.0.113.9", "at": now, "detail": {"count": 150}}]
    db.accounts.rows.append({"hb_sightings": [{"ip": "203.0.113.9", "terminal": "t1", "at": now}]})
    out = run(B2(db, {"thresholds": {}}))
    assert [o["evidence"]["ip"] for o in out] == ["203.0.113.7"]


def test_s3_b1_needs_two_terminals_or_interleaving():
    from security_agent.checks.bridge_secrets_deps import B1
    db = FakeDb()
    t = datetime.now(timezone.utc)
    def sight(ip, term, i):
        return {"ip": ip, "terminal": term, "at": (t - timedelta(seconds=50 - i)).isoformat()}
    # one terminal moving once (dynamic IP) → no finding
    db.accounts.rows.append({"_id": ObjectId(), "label": "a", "hb_sightings": [sight("1.1.1.1", "t1", 0), sight("1.1.1.1", "t1", 1), sight("2.2.2.2", "t1", 2), sight("2.2.2.2", "t1", 3)]})
    assert run(B1(db, {"thresholds": {}})) == []
    # same terminal interleaving A→B→A → finding
    db.accounts.rows[0]["hb_sightings"] = [sight("1.1.1.1", "t1", 0), sight("2.2.2.2", "t1", 1), sight("1.1.1.1", "t1", 2)]
    assert len(run(B1(db, {"thresholds": {}}))) == 1
    # two installation ids → finding
    db.accounts.rows[0]["hb_sightings"] = [sight("1.1.1.1", "t1", 0), sight("1.1.1.1", "t2", 1), sight("1.1.1.1", "t1", 2)]
    assert len(run(B1(db, {"thresholds": {}}))) == 1


def test_s3_r5_has_expiry_and_bridge_lifts_expired_suspension():
    from security_agent import rules
    props = rules.evaluate({"check_id": "B1", "dedup_key": "B1:account:a1", "evidence": {"account_id": "a1", "sources": [[1, 2], [3, 4]]}}, {"thresholds": {}})
    assert props and props[0]["rule"] == "R5" and props[0]["expires_min"] == 240
    import routes.bridge_routes as br
    src = inspect.getsource(br._account_by_token)
    assert 'susp.get("expires_at") and susp["expires_at"] <' in src and '"bridge_token_retired"' in src


# ── S4 ──────────────────────────────────────────────────────────────────────
def test_s4_r7_only_for_forged_tokens_and_targets_user_accounts():
    from security_agent import rules, actions
    th = {"thresholds": {}}
    assert rules.evaluate({"check_id": "A6", "dedup_key": "x", "evidence": {"event": {"user_id": "u1", "reason": "step_up_invalid"}}}, th) == []
    props = rules.evaluate({"check_id": "A6", "dedup_key": "x", "evidence": {"event": {"user_id": "u1", "reason": "step_up_forged"}}}, th)
    assert props[0]["target_kind"] == "user" and props[0]["revoke_sessions"] is True
    db = FakeDb()
    a1, a2 = ObjectId(), ObjectId()
    db.accounts.rows += [{"_id": a1, "user_id": "u1", "mode": "live", "trading_authority": "FULL"},
                         {"_id": a2, "user_id": "u1", "mode": "live"}, {"_id": ObjectId(), "user_id": "u1", "mode": "paper"}]
    revoked = []

    async def fake_revoke(db_, uid, reason):
        revoked.append(uid); return 2
    with patch("security.revoke_all_user_sessions", fake_revoke):
        out = run(actions._apply(db, {}, {**props[0]}, {"dedup_key": "x"}))
    assert revoked == ["u1"] and out["revoked_sessions"] == 2 and set(out["accounts"]) == {str(a1), str(a2)}
    assert all(r["trading_authority"] == "CLOSE_ONLY" and r["security_freeze"] for r in db.accounts.rows if r["mode"] == "live")
    import step_up
    assert "step_up_forged" in inspect.getsource(step_up.require_step_up) and "known = await db.step_up_tokens.find_one" in inspect.getsource(step_up.require_step_up)


# ── S5 ──────────────────────────────────────────────────────────────────────
def test_s5_named_loggers_and_late_handlers_are_masked():
    from security_agent.redact import install_log_filter
    install_log_filter()
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    h = H()                                     # created AFTER install, attached to a NAMED logger
    lg = logging.getLogger("httpx.test.s5")
    lg.addHandler(h); lg.setLevel(logging.INFO); lg.propagate = False
    tok = "1234567890:" + "AAH" + "x" * 32
    lg.warning("POST https://api.telegram.org/bot%s/sendMessage", tok)
    assert records and tok not in records[-1] and "[REDACTED]" in records[-1]
    assert "_install_redaction()" in open(os.path.join(ROOT, "backend", "workers", "base.py")).read()
    srv = open(os.path.join(ROOT, "backend", "server.py")).read()
    assert srv.index("logging.basicConfig(") < srv.index("_install_redaction()   # S5")


# ── S7 / S8 / S9 ────────────────────────────────────────────────────────────
def test_s7_is_blocked_fails_open_with_last_good_cache():
    import security

    class Boom:
        def find(self, *a, **k):
            raise RuntimeError("mongo down")

    class DB:
        security_blocks = Boom()
    security._BLOCK_CACHE.update(at=0.0, rows=[{"kind": "ip", "value": "9.9.9.9", "scope": "all"}])
    assert run(security.is_blocked(DB(), "ip", "9.9.9.9")) is not None     # last good list still served
    assert run(security.is_blocked(DB(), "ip", "8.8.8.8")) is None
    assert security._BLOCK_CACHE.get("error") == "RuntimeError"
    security._BLOCK_CACHE.update(at=0.0, rows=[], error=None)


def test_s8_panic_release_keeps_agent_freeze():
    import routes.panic_routes as pr
    src = inspect.getsource(pr.release_panic_locks)
    assert '"security_freeze": {"$exists": True}' in src and '"trading_authority": "CLOSE_ONLY"' in src


def test_s9_ipv6_identifier_split():
    from security_agent.checks import access
    assert inspect.getsource(access).count('rpartition(":")') == 2
    ip, _, email = "2001:db8::1:user@example.com".rpartition(":")
    assert ip == "2001:db8::1" and email == "user@example.com"


# ── S10 / S11 / S16 ─────────────────────────────────────────────────────────
def test_s10_contained_not_auto_resolved_and_expired_action_resolves():
    from security_agent.findings import auto_resolve_cleared
    from security_agent import actions
    db = FakeDb()
    fid = ObjectId()
    db.security_findings.rows.append({"_id": fid, "check_id": "A1", "status": "contained", "dedup_key": "A1:ip:1.1.1.1", "last_seen": "x"})
    assert run(auto_resolve_cleared(db, "A1", set())) == 0 and db.security_findings.rows[0]["status"] == "contained"
    db.security_actions.rows.append({"kind": "containment", "status": "done", "action": "block_ip", "finding_id": str(fid), "dedup_key": "A1:ip:1.1.1.1",
                                     "target": "1.1.1.1", "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
    assert run(actions.expire_finished(db)) == 1
    assert db.security_findings.rows[0]["status"] == "resolved" and db.security_actions.rows[0]["status"] == "expired"


def test_s11_failed_delivery_not_counted_and_retried_later():
    from security_agent import alerts
    db = FakeDb()
    fd = {"_id": ObjectId(), "dedup_key": "k", "severity": "critical", "check_id": "A1", "title": "t", "what_happened": "w", "evidence": {}}
    db.security_findings.rows.append(dict(fd))

    async def tg_fail(text): return False

    async def mail_fail(cfg, subj, text): return False
    run(alerts.deliver(db, {}, fd, repeat=False, send_tg=tg_fail, send_mail=mail_fail))
    row = db.security_findings.rows[0]
    assert row.get("alert_count") in (None, 0) and row["alert_failures"] == 1 and row.get("last_alert_attempt_at")
    assert "last_alert_attempt_at" in str(alerts._claim_filter({}))
    assert '"kind": "alert"' in inspect.getsource(alerts.deliver)


def test_s16_bad_env_value_ignored_and_undo_restores_exact_prior():
    from security_agent import config, actions
    cfg = config.from_env({"SECURITY_AGENT_IP_BLOCK_MIN": "sixty"})
    assert cfg["ip_block_min"] == config.DEFAULTS["ip_block_min"] and cfg["config_errors"]
    db = FakeDb()
    aid = ObjectId()
    db.accounts.rows.append({"_id": aid, "trading_authority": "CLOSE_ONLY", "authority_lock": {"reason": "security_agent"}, "security_freeze": {"rule": "R7"}})
    run(actions._unfreeze_account(db, aid, {"trading_authority": None, "authority_lock": None}))
    assert "trading_authority" not in db.accounts.rows[0] and "security_freeze" not in db.accounts.rows[0]


# ── S12 / S13 / S14 ─────────────────────────────────────────────────────────
def test_s12_a7_reads_persisted_denied_counts_and_feeds_r8():
    from security_agent.checks.access import A7
    from security_agent import rules
    db = FakeDb()
    minute = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    db.security_denied_counts.rows += [{"ip": "203.0.113.7", "minute": minute, "n": 1600}, {"ip": "203.0.113.8", "minute": minute, "n": 10}]
    out = run(A7(db, {"thresholds": {}}))
    assert out and out[0]["evidence"]["top_ip"] == "203.0.113.7" and out[0]["evidence"]["top_ip_rpm"] >= 300
    props = rules.evaluate({"check_id": "A7", "dedup_key": "A7:platform", "evidence": out[0]["evidence"]}, {"thresholds": {}})
    assert props and props[0]["rule"] == "R8" and props[0]["target"] == "203.0.113.7"
    srv = open(os.path.join(ROOT, "backend", "server.py")).read()
    assert "_security_agent_middleware" in srv and 'is_blocked(get_db(), "ip", ip, "all")' in srv


def test_s12_flush_denied_counts_persists_per_minute():
    from security_agent.events import flush_denied_counts
    db = FakeDb()
    state = {"minute": "2026-10-05T10:00", "by_ip": {"1.1.1.1": 3}}
    assert run(flush_denied_counts(db, state, force=True)) == 1
    assert db.security_denied_counts.rows[0]["n"] == 3 and state["by_ip"] == {}


def test_s13_no_data_findings():
    from security_agent.checks.bridge_secrets_deps import D1
    from security_agent.checks.integrity_platform_trading import I4
    db = FakeDb()
    out = run(D1(db, {"thresholds": {}}))
    assert out and out[0]["evidence"].get("no_data") is True
    with patch.dict(os.environ, {"STOIC_BACKUP_DIR": "/nonexistent/backups"}):
        out = run(I4(db, {"thresholds": {}}))
    assert out and out[0]["evidence"].get("no_data") is True


def test_s14_security_telegram_token_is_a_worker_only_docker_secret():
    compose = open(os.path.join(ROOT, "docker-compose.yml")).read()
    assert "SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE: /run/secrets/security_telegram_token" in compose
    assert compose.count("security_telegram_token") >= 3
    assert "security_telegram_token" in open(os.path.join(ROOT, "deploy", "install.sh")).read()


def test_s6_memory_is_scanned_except_test_credentials():
    allow = open(os.path.join(ROOT, "scripts", "secret_scan_allowlist.txt")).read().splitlines()
    rules_ = [l for l in allow if l and not l.startswith("#")]
    assert "memory/" not in rules_ and "memory/test_credentials.md" in rules_
