"""Security & Health Agent — SA4 unit tests: containment actions (protected list, hourly cap, observe = dry run,
rule gating, expiry, undo restores exact prior state, audit chain), enforcement helpers (is_blocked scopes,
cache, rightmost-XFF), and the no-trade-effect invariant. No live Mongo (fake_mongo)."""
import asyncio
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from fake_mongo import FakeDb  # noqa: E402

# P1-01 — the sweep hashes the suspended bridge token; the isolated unit job carries no
# JWT_SECRET, so give bridge_tokens a throwaway (non-secret) key for this process only.
os.environ.setdefault("BRIDGE_TOKEN_HASH_KEY", "unit-test-not-a-secret")

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def cfg(**over):
    from security_agent.config import from_env
    c = from_env({})
    c.update(over)
    return c


ENFORCE_ALL = dict(mode="enforce", rules_enabled=["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"])


def seed(db, check_id, key, severity, evidence, what="x"):
    from security_agent.checks.common import f
    from security_agent.findings import open_or_update
    res = run(open_or_update(db, f(check_id, key, severity, "access", what, evidence)))
    return next(r for r in db.security_findings.rows if str(r["_id"]) == res["id"])


def snapshot(db):
    return copy.deepcopy({c: db[c].rows for c in ("trades", "orders", "bot_configs", "pending_modifications", "ops_outbox")})


def test_observe_and_disabled_rule_are_dry_runs():
    from security_agent import actions
    db = FakeDb()
    seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    assert run(actions.sweep(db, cfg())) == 1
    assert db.security_actions.rows[0]["status"] == "would_have_done" and db.security_blocks.rows == []
    db2 = FakeDb()
    seed(db2, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    run(actions.sweep(db2, cfg(mode="enforce", rules_enabled=["R4"])))
    assert db2.security_actions.rows[0]["status"] == "would_have_done" and "not enabled" in db2.security_actions.rows[0]["note"]
    assert db2.security_blocks.rows == [] and db2.admin_audit_log.rows == []


def test_enforce_block_ip_expiry_undo_extend_and_audit():
    from security_agent import actions
    from security import active_blocks, invalidate_block_cache, is_blocked
    db, c = FakeDb(), cfg(**ENFORCE_ALL)
    fd = seed(db, "A1", "ip:9.9.9.9", "high", {"ip": "9.9.9.9", "failures": 25})
    assert run(actions.sweep(db, c)) == 1
    act = db.security_actions.rows[0]
    assert act["status"] == "done" and act["rule"] == "R1" and act["block_id"]
    blk = db.security_blocks.rows[0]
    assert blk["kind"] == "ip" and blk["value"] == "9.9.9.9" and blk["scope"] == "auth" and blk["active"]
    assert timedelta(minutes=59) < blk["expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=60)   # rule 4: every block expires
    assert fd["status"] == "contained" and fd["fixed"] == "contained" and "R1 block_ip 9.9.9.9" in fd["action_taken"]
    assert db.admin_audit_log.rows[-1]["action"] == "security_action_block_ip" and db.admin_audit_log.rows[-1]["target_kind"] == "security_agent"
    invalidate_block_cache()
    assert run(is_blocked(db, "ip", "9.9.9.9", "auth")) and run(is_blocked(db, "ip", "9.9.9.9", "bridge")) is None   # scope auth ≠ bridge
    assert run(is_blocked(db, "ip", "9.9.9.9", "all")) and run(is_blocked(db, "ip", "1.1.1.1", "auth")) is None
    assert run(actions.sweep(db, c)) == 0                                                                   # already contained → no repeat
    ext = run(actions.extend(db, act["_id"], 5, "adm@x"))
    assert timedelta(minutes=4) < blk["expires_at"] - datetime.now(timezone.utc) <= timedelta(minutes=5) and ext["expires_at"]
    und = run(actions.undo(db, act["_id"], "adm@x", "false alarm"))
    assert und["status"] == "undone" and blk["active"] is False and fd["status"] == "open" and fd["fixed"] == "no"
    assert db.admin_audit_log.rows[-1]["action"] == "security_undo_block_ip" and db.admin_audit_log.rows[-1]["actor_email"] == "adm@x"
    invalidate_block_cache()
    assert run(is_blocked(db, "ip", "9.9.9.9", "auth")) is None and run(active_blocks(db)) == []
    with pytest.raises(ValueError):
        run(actions.undo(db, act["_id"], "adm@x"))                                                           # idempotent: already undone
    with pytest.raises(LookupError):
        run(actions.undo(db, "000000000000000000000000", "adm@x"))


def test_protected_targets_never_blocked_and_cap_switches_to_alert_only():
    from security_agent import actions
    db, c = FakeDb(), cfg(max_actions_per_hour=2, protected_ips=["203.0.113.0/24"], **ENFORCE_ALL)
    admin = os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com"
    seed(db, "A1", f"account:{admin}", "high", {"account": admin, "failures": 99, "ips": ["1", "2", "3"]})
    seed(db, "A1", "ip:203.0.113.9", "high", {"ip": "203.0.113.9", "failures": 99})
    seed(db, "A1", "ip:172.64.1.1", "high", {"ip": "172.64.1.1", "failures": 99})                            # Cloudflare range
    c["admin_user_id"] = "adminoid"
    seed(db, "A4", "user:adminoid", "critical", {"event": {"user_id": "adminoid"}})                           # audit #5 SEC-001: admin by user-id
    for i in range(4):
        seed(db, "B2", f"ip:7.7.7.{i}", "high", {"ip": f"7.7.7.{i}", "requests": 40})
    run(actions.sweep(db, c))
    st = [a["status"] for a in db.security_actions.rows]
    assert st[:4] == ["refused_protected"] * 4 and st[4:].count("done") == 2 and st[4:].count("refused_cap") == 2
    assert db.auth_sessions.rows == [] and not any(a["action"] == "revoke_sessions" and a["status"] == "done" for a in db.security_actions.rows)
    assert len(db.security_blocks.rows) == 2 and all(b["value"].startswith("7.7.7.") for b in db.security_blocks.rows)
    kinds = {r["check_id"] for r in db.security_findings.rows}
    assert {"protected_target", "containment_cap_reached"} <= kinds
    assert all(r["severity"] == "critical" for r in db.security_findings.rows if r["check_id"] in ("protected_target", "containment_cap_reached"))


def test_account_actions_lock_login_otp_freeze_suspend_revoke_with_prior_state_restore():
    from security_agent import actions
    from security import invalidate_block_cache, is_blocked
    db, c = FakeDb(), cfg(**ENFORCE_ALL)
    db.users.rows += [{"_id": "u1", "email": "v@x.com"}]
    db.accounts.rows += [{"_id": "acc1", "user_id": "u1", "label": "Live-1", "bridge_token": "tok-live", "trading_authority": "FULL", "trading_enabled": True},
                         {"_id": "acc2", "user_id": "u1", "label": "Panicked", "trading_authority": "LOCKED", "authority_lock": {"reason": "panic", "by": "admin", "scope": "platform"}}]
    db.auth_sessions.rows += [{"_id": "s1", "user_id": "u1", "revoked": False}, {"_id": "s2", "user_id": "u2", "revoked": False}]
    db.trades.rows += [{"_id": "t1", "account_id": "acc1", "status": "open", "sl": 1.0, "tp": 2.0}]
    before = snapshot(db)
    seed(db, "A1", "account:v@x.com", "high", {"account": "v@x.com", "failures": 60, "ips": ["1", "2", "3"]})
    seed(db, "A2", "2fa:v@x.com", "high", {"target": "v@x.com", "failures": 12})
    seed(db, "B3", "account:acc1:x", "critical", {"event": {"account_id": "acc1"}})
    seed(db, "B3", "account:acc2:x", "critical", {"event": {"account_id": "acc2"}})
    seed(db, "B1", "account:acc1", "critical", {"account_id": "acc1", "sources": [["1", "a"], ["2", "b"]]})
    seed(db, "A4", "user:u1", "critical", {"event": {"user_id": "u1"}})
    run(actions.sweep(db, c))
    by = {(a["action"], a["target"]): a for a in db.security_actions.rows}
    assert all(a["status"] == "done" for a in db.security_actions.rows), [(a["action"], a["status"]) for a in db.security_actions.rows]
    invalidate_block_cache()
    assert run(is_blocked(db, "account_login", "V@X.COM")) and run(is_blocked(db, "account_otp", "v@x.com"))
    acc1 = db.accounts.rows[0]
    assert acc1["trading_authority"] == "CLOSE_ONLY" and acc1["authority_lock"]["reason"] == "security_agent"      # frozen, closes allowed
    from bridge_tokens import token_hash as _th
    assert acc1["bridge_token_suspended"]["token_hash"] == _th("tok-live")   # hash key: module-level dummy (CI has no JWT_SECRET) and "token" not in acc1["bridge_token_suspended"]   # P1-01
    acc2 = db.accounts.rows[1]
    assert acc2["trading_authority"] == "LOCKED" and acc2["authority_lock"]["reason"] == "panic" and "never overrides a PANIC lock" in by[("freeze_new_entries", "acc2")]["note"] and acc2["security_freeze"]   # never overrides PANIC (S8: freeze kept in its own field)
    assert db.auth_sessions.rows[0]["revoked"] is True and db.auth_sessions.rows[1]["revoked"] is False
    assert len([n for n in db.notifications.rows if n["user_id"] == "u1"]) == 5                              # R2, R3, R5, R7 ×2 owner notices (S8: freeze recorded on the PANIC-locked account too)
    assert all(n["kind"].startswith("security_") for n in db.notifications.rows)
    assert snapshot(db) == before                                                                            # rule 3: trades/orders/configs untouched
    # undo restores the exact prior state
    run(actions.undo(db, by[("freeze_new_entries", "acc1")]["_id"], "adm@x"))
    assert acc1.get("trading_authority") in (None, "FULL") and acc1.get("authority_lock") is None and "security_freeze" not in acc1   # S16: exact prior restored
    run(actions.undo(db, by[("suspend_bridge_token", "acc1")]["_id"], "adm@x"))
    assert "bridge_token_suspended" not in acc1
    run(actions.undo(db, by[("lock_login", "v@x.com")]["_id"], "adm@x"))
    invalidate_block_cache()
    assert run(is_blocked(db, "account_login", "v@x.com")) is None and run(is_blocked(db, "account_otp", "v@x.com"))
    with pytest.raises(ValueError):
        run(actions.undo(db, by[("revoke_sessions", "u1")]["_id"], "adm@x"))                                 # sessions: user logs in again
    with pytest.raises(LookupError):
        run(actions.extend(db, by[("freeze_new_entries", "acc1")]["_id"], 10, "adm@x"))                      # extend applies to blocks only


def test_enforcement_helpers_rightmost_xff_cache_and_deny():
    from fastapi import HTTPException
    from starlette.requests import Request
    from security import client_ip, deny_if_blocked, invalidate_block_cache, is_blocked
    db = FakeDb()
    db.security_blocks.rows.append({"_id": "b1", "kind": "ip", "value": "9.9.9.9", "scope": "all", "active": True,
                                    "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)})
    db.security_blocks.rows.append({"_id": "b2", "kind": "ip", "value": "8.8.8.8", "scope": "all", "active": True,
                                    "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)})                 # expired → ignored
    invalidate_block_cache()
    assert run(is_blocked(db, "ip", "9.9.9.9", "bridge")) and run(is_blocked(db, "ip", "8.8.8.8")) is None

    def req(xff):
        return Request({"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": ("10.0.0.1", 1), "method": "GET", "path": "/", "query_string": b""})
    os.environ.pop("TRUST_CF_CONNECTING_IP", None)
    assert client_ip(req("1.2.3.4, 9.9.9.9")) == "9.9.9.9"                     # spoofed leftmost cannot dodge the block
    assert client_ip(req("9.9.9.9, 1.2.3.4")) == "1.2.3.4"                     # nor misdirect it onto an innocent IP
    with pytest.raises(HTTPException) as ei:
        run(deny_if_blocked(db, "ip", client_ip(req("1.2.3.4, 9.9.9.9")), "auth"))
    assert ei.value.status_code == 403 and ei.value.detail["code"] == "blocked_by_security_agent"
    db.security_blocks.rows[0]["active"] = False
    assert run(is_blocked(db, "ip", "9.9.9.9"))                                 # cached ≤10 s …
    invalidate_block_cache()
    assert run(is_blocked(db, "ip", "9.9.9.9")) is None                         # … until invalidated / expired


def test_sa4_routes_in_matrix_and_hooks_present():
    import inspect
    import routes.security_agent_routes as r
    from security_matrix import BOLA_MATRIX, ADM
    for m, p in (("GET", "/api/admin/security/blocks"), ("POST", "/api/admin/security/actions/{action_id}/undo"), ("POST", "/api/admin/security/actions/{action_id}/extend")):
        assert BOLA_MATRIX[(m, p)] == ADM
    for fn in (r.undo_action, r.extend_action):
        assert "require_step_up(" in inspect.getsource(fn) and "require_admin(user)" in inspect.getsource(fn)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    auth_src = open(os.path.join(root, "routes", "auth_routes.py")).read()
    bridge_src = open(os.path.join(root, "routes", "bridge_routes.py")).read()
    assert auth_src.count("deny_if_blocked(") >= 4 and 'deny_if_blocked(db, "ip", ip, "auth")' in auth_src
    assert 'deny_if_blocked(get_db(), "ip", client_ip(request), "bridge")' in bridge_src and "bridge_token_suspended" in bridge_src
