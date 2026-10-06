"""main93 review — step A9 (a: deploy blockers, b: trading path, c: security agent, d: account
security, e: release evidence). BEHAVIOUR tests on a fake async db wherever the review said the
previous test only read source text (S8). Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import json
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


def _now():
    return datetime.now(timezone.utc)


# ── A9a · N-R3 ─────────────────────────────────────────────────────────────
def test_nr3_finding_detail_declares_action_id_before_use():
    src = open(os.path.join(ROOT, "frontend", "src", "components", "security", "FindingDetail.jsx")).read()
    assert src.index("const actionId =") < src.index("const canUndo =")


# ── A9a · N-R2 ─────────────────────────────────────────────────────────────
def test_nr2_update_path_creates_every_compose_secret():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    ensure = lib[lib.index("ensure_release_secrets()"):]
    ensure = ensure[:ensure.index("\n}\n")]
    assert "secrets/security_telegram_token" in ensure and "umask 077" in ensure
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_compose_secrets.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    assert "scripts/check_compose_secrets.py" in ci


# ── A9a · N-R4 / S8 — BEHAVIOUR: PANIC release keeps the agent freeze ──────────
def test_nr4_panic_release_reapplies_security_freeze_behaviour():
    from routes.panic_routes import release_panic_locks, USER_LOCK_MATCH
    db = FakeDb()
    frozen = {"_id": ObjectId(), "user_id": "u1", "trading_authority": "CLOSE_ONLY",
              "authority_lock": {"reason": "panic", "by": "u1", "scope": "user"},
              "security_freeze": {"rule": "R7", "by": "security_agent", "at": "2026-10-05T00:00:00"}}
    plain = {"_id": ObjectId(), "user_id": "u1", "trading_authority": "CLOSE_ONLY",
             "authority_lock": {"reason": "panic", "by": "u1", "scope": "user"}}
    db.accounts.rows.extend([frozen, plain])

    async def _bump(*a, **k):
        return None
    with patch("canonical_decision.bump_authority_version", _bump):
        n = run(release_panic_locks(db, {"user_id": "u1", **USER_LOCK_MATCH}, actor="u1", via="test"))
    assert n == 2
    f = next(r for r in db.accounts.rows if r["_id"] == frozen["_id"])
    p = next(r for r in db.accounts.rows if r["_id"] == plain["_id"])
    assert f["trading_authority"] == "CLOSE_ONLY" and f["authority_lock"]["reason"] == "security_agent"   # freeze survives
    assert "trading_authority" not in p and "authority_lock" not in p                                      # plain PANIC lifted


# ── A9a · N-R1 — trusted proxy chain ──────────────────────────────────────────
class _Req:
    def __init__(self, xff=None, cf=None, peer="10.0.0.9"):
        self.headers = {k: v for k, v in (("x-forwarded-for", xff), ("cf-connecting-ip", cf)) if v}
        self.client = type("c", (), {"host": peer})()


def test_nr1_client_ip_walks_trusted_proxy_chain_from_the_right():
    from security import client_ip
    with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "172.16.0.0/12,10.0.0.0/8", "TRUST_CF_CONNECTING_IP": "false"}):
        # Caddy (172.18.0.5) appended by nginx; client 198.51.100.7 appended by Caddy
        assert client_ip(_Req("198.51.100.7, 172.18.0.5")) == "198.51.100.7"
        # spoofed left entry behind an untrusted edge peer: the untrusted peer wins (SEC-002)
        assert client_ip(_Req("1.2.3.4, 198.51.100.7, 172.18.0.5")) == "198.51.100.7"
        # single ingress hop (preview / k8s): rightmost as before
        assert client_ip(_Req("203.0.113.9")) == "203.0.113.9"
        # all hops trusted → leftmost
        assert client_ip(_Req("10.1.1.1, 172.18.0.5")) == "10.1.1.1"
    with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "172.16.0.0/12", "TRUST_CF_CONNECTING_IP": "true"}):
        # Cloudflare edge is the first untrusted hop → CF-Connecting-IP authoritative
        assert client_ip(_Req("198.51.100.7, 103.21.244.10, 172.18.0.5", cf="198.51.100.7")) == "198.51.100.7"
        # direct hit on the origin with a forged CF header → ignored
        assert client_ip(_Req("203.0.113.9, 172.18.0.5", cf="9.9.9.9")) == "203.0.113.9"
    with patch.dict(os.environ, {"TRUSTED_PROXY_CIDRS": "", "TRUST_CF_CONNECTING_IP": "false"}):
        assert client_ip(_Req("198.51.100.7, 172.18.0.5")) == "172.18.0.5"   # unconfigured = legacy behaviour
    nginx = open(os.path.join(ROOT, "deploy", "nginx.conf")).read()
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in nginx
    assert "TRUSTED_PROXY_CIDRS" in open(os.path.join(ROOT, "deploy", "install.sh")).read()
    assert "TRUSTED_PROXY_CIDRS" in open(os.path.join(ROOT, "deploy", "update.sh")).read()


# ── A9a · N-R5 — FX sanity band ───────────────────────────────────────────────
def test_nr5_bad_fx_tick_is_rejected_and_table_used():
    import fx_rates
    from pip_utils import QUOTE_USD_APPROX
    fx_rates.reset_cache()
    alerts = []

    async def bad_price(symbol, user_id):
        return 15000.0, "broker_tick"          # USDJPY 15000 instead of ~150

    async def fake_alert(db_, kind, sev, msg, dedup_key=None, meta=None, **kw):
        alerts.append(kind)
    with patch.object(fx_rates, "_live_price", bad_price), patch("alerting.raise_alert", fake_alert), \
            patch("database.get_db", lambda: FakeDb()):
        rate, source = run(fx_rates.quote_usd("JPY"))
    assert source == "rejected_live" and abs(rate - QUOTE_USD_APPROX["JPY"]) < 1e-9
    assert alerts == ["fx_rate_rejected"]
    snap = fx_rates.rates_snapshot()
    assert "JPY" in snap["rejected"] and "JPY" in snap["stale_currencies"] and snap["sanity_band"] == 0.25
    fx_rates.reset_cache()

    async def ok_price(symbol, user_id):
        return 150.0, "broker_tick"
    with patch.object(fx_rates, "_live_price", ok_price):
        rate, source = run(fx_rates.quote_usd("JPY"))
    assert source == "broker_tick" and abs(rate - 1 / 150.0) < 1e-9
    assert fx_rates.rate_is_sane("JPY", 1 / 150.0) and not fx_rates.rate_is_sane("JPY", 1 / 15000.0)
    fx_rates.reset_cache()


# ── A9a · N-R6 — dual EA hash ─────────────────────────────────────────────────
def test_nr6_current_and_previous_signed_hash_accepted(tmp_path):
    import ea_capabilities as ec
    ec._EXPECTED_CACHE.clear()
    with patch.dict(os.environ, {"EA_RELEASE_SHA256": "a" * 64, "EA_RELEASE_SHA256_PREVIOUS": "b" * 64}):
        assert ec.accepted_ea_sha256s() == ["a" * 64, "b" * 64] and ec.expected_ea_sha256() == "a" * 64
        acc = {"ea_binary_sha256": "b" * 64, "ea_binary_sha256_method": "installer_attested",
               "broker_env": {"attested_environment": "LIVE"}}
        with patch("broker_env.attested_environment", lambda a: "LIVE"), patch("broker_env.broker_environment", lambda a: "LIVE"):
            src = inspect.getsource(ec)
            assert 'if reported not in {expected.lower(), *accepted_ea_sha256s()}:' in src       # previous hash never → EA_BINARY_HASH_MISMATCH
    with patch.dict(os.environ, {"EA_RELEASE_SHA256": "", "EA_RELEASE_SHA256_PREVIOUS": ""}):
        ec._EXPECTED_CACHE.clear()
        assert ec.expected_ea_sha256() is None or len(ec.expected_ea_sha256()) == 64
    # verify_ea_release.record() keeps the last SIGNED release as `previous` on a version change
    vsrc = open(os.path.join(ROOT, "scripts", "verify_ea_release.py")).read()
    assert '"previous": previous' in vsrc and 'ea.get("version") != version' in vsrc


def test_ea_renamed_to_1_60_everywhere_and_hash_recorded():
    mq5 = open(os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5"), encoding="utf-8", errors="replace").read()
    assert '#property version   "1.60"' in mq5 and '#define EA_CLIENT_VERSION "1.60"' in mq5 and "margin_mode" in mq5
    rec = json.load(open(os.path.join(ROOT, "docs", "RELEASE_HASHES.json")))["ea"]
    assert rec["version"] == "1.60"
    import hashlib
    raw = open(os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5"), "rb").read().replace(b"\r\n", b"\n")
    assert rec["mq5_sha256"] == hashlib.sha256(raw).hexdigest()
    for rel in ("frontend/src/components/EaVersionStrip.jsx", "frontend/src/pages/Accounts.jsx", "backend/routes/diagnostic_routes.py"):
        assert "1.60" in open(os.path.join(ROOT, rel)).read()


# ── A9b · N1 — absorb only bot backfill rows (BEHAVIOUR) ──────────────────────
def test_n1_manual_row_on_netted_ticket_is_never_absorbed():
    from routes.bridge_routes import _absorb_duplicate_ticket_row
    db = FakeDb()
    keep = ObjectId()
    manual = {"_id": ObjectId(), "account_id": "a1", "mt5_ticket": 77, "status": "open", "origin": "manual",
              "netting_shared_with": str(keep), "execution_intent_id": None}
    backfill = {"_id": ObjectId(), "account_id": "a1", "mt5_ticket": 77, "status": "open", "origin": "auto",
                "backfilled_from_snapshot": True, "execution_intent_id": None, "live_pnl": 3.0}
    db.trades.rows.extend([manual, backfill, {"_id": keep, "account_id": "a1", "mt5_ticket": 77, "status": "open", "origin": "auto"}])
    absorbed = run(_absorb_duplicate_ticket_row(db, "a1", 77, None, keep))
    assert absorbed == str(backfill["_id"])
    assert next(r for r in db.trades.rows if r["_id"] == manual["_id"])["status"] == "open"     # manual survives
    assert run(_absorb_duplicate_ticket_row(db, "a1", 77, None, keep)) is None                    # nothing else eligible


# ── A9b · N12 — real prior status (BEHAVIOUR) ─────────────────────────────────
def test_n12_late_fill_reads_real_prior_status():
    from execution_intents import record_late_fill
    db = FakeDb()
    db.execution_intents.rows.append({"intent_id": "i1", "status": "rejected", "history": []})
    alerts = []

    async def fake_alert(db_, kind, sev, msg, dedup_key=None, meta=None, **kw):
        alerts.append(kind)
    with patch("alerting.raise_alert", fake_alert):
        out = run(record_late_fill(db, "i1", ticket=5, via="report", prior=""))   # caller hint EMPTY
    assert out["status"] == "filled" and out["late_fill_prior_status"] == "rejected" and out["late_fill_anomaly"] is True
    assert alerts == ["late_fill_after_reject"]


# ── A9b · N3 / N4 ─────────────────────────────────────────────────────────────
def test_n3_lock_cancelled_late_fill_gets_a_policy_and_n4_revive_recloses():
    import routes.bridge_routes as br
    src = inspect.getsource(br.handle_late_fill)
    assert 'policy = "lock"' in src and 'reason="late_fill_after_lock"' in src
    assert "late_fill_after_lock" in br.INTENTIONAL_CLOSE_REASONS and "late_fill_after_lock" in br.NO_REVIVE_FILTER
    hb = inspect.getsource(br.heartbeat)
    assert hb.count("revive_and_reclose(") == 2          # open_tickets path + snapshot path
    rsrc = inspect.getsource(br.revive_and_reclose)
    assert '"status": "open"' in rsrc and 'pending_modification={"type": "FULL_CLOSE", "reason": reason}' in rsrc


def test_n5_leg_upgrade_reachable_on_existing_branch_and_p3_empty_cap():
    import routes.bridge_routes as br
    import bot_runner
    src = inspect.getsource(br)
    assert '"leg_upgraded": True' in src and 'note="leg_upgraded_to_deal_id"' in src
    bsrc = inspect.getsource(bot_runner)
    assert 'trade_of_day_cap_default = _tod_raw in (None, "")' in bsrc


# ── A9c · M1 / M2 / S5 / S16 / M5 ─────────────────────────────────────────────
def test_m1_per_ip_last_valid_heartbeat_record_feeds_b2_and_scorecard():
    import routes.bridge_routes as br
    from security_agent.checks import bridge_secrets_deps as bsd
    from security_agent import scorecard
    assert "hb_valid_ips" in inspect.getsource(br.record_heartbeat_sighting) and "hb_last_valid_at" in inspect.getsource(br.record_heartbeat_sighting)
    assert "hb_valid_ips" in inspect.getsource(bsd.B2)
    assert "hb_valid_ips" in inspect.getsource(scorecard._ip_hit) and "hb_last_valid_at" in inspect.getsource(scorecard._token_hit)


def test_m2_shared_ip_share_measured_against_successful_traffic():
    from security_agent import actions, rules
    db = FakeDb()
    now = _now().isoformat()
    # attacker: 500 failures, 0 sessions → share 0 (blockable)
    for _ in range(500):
        db.security_events.rows.append({"kind": "login_failed", "ip": "203.0.113.7", "at": now})
    for i in range(max(rules.SHARED_IP_MIN_EVENTS, 10)):
        db.auth_sessions.rows.append({"ip": f"198.51.100.{i}", "created_at": now})
    assert run(actions.shared_ip_share(db, "203.0.113.7")) == 0.0
    # proxy: every successful session resolves to one IP → share 1 (refused)
    for s in db.auth_sessions.rows:
        s["ip"] = "172.18.0.5"
    assert run(actions.shared_ip_share(db, "172.18.0.5")) == 1.0


def test_s5_tracebacks_are_masked():
    from security_agent.redact import RedactingFilter
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), None)
    fake = "1234567890:" + "AA" + "x" * 33          # synthetic BotFather-shaped token, assembled at runtime
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{fake}/sendMessage failed")
    except RuntimeError:
        rec.exc_info = sys.exc_info()
    RedactingFilter().filter(rec)
    assert rec.exc_info is None and rec.exc_text and fake not in rec.exc_text


def test_s16_in_process_agent_takes_the_security_lease_and_m5_background_write():
    from security_agent import runner
    import llm_timeout
    assert '_try_acquire(db, "security")' in inspect.getsource(runner.loop)
    src = inspect.getsource(llm_timeout.send_with_timeout)
    assert "_record_bg(" in src and "await _record(" not in src


# ── A9d · P1-02 / P1-03 — BEHAVIOUR through the shared validator ──────────────
def _user(**kw):
    return {"_id": ObjectId(), "email": "u@x.io", "role": "user", "status": "active", **kw}


def test_p1_02_access_token_dies_with_its_session_and_valid_after():
    import auth
    db = FakeDb()
    u = _user(); db.users.rows.append(u)
    uid = str(u["_id"])
    with patch.dict(os.environ, {"JWT_SECRET": "unit-secret"}):
        tok = auth.create_access_token(uid, u["email"], sid="sid1")
        db.auth_sessions.rows.append({"session_id": "sid1", "jti": "j1", "revoked": False, "user_id": uid})
        assert run(auth.validate_access_token(db, tok, path="/api/accounts"))["id"] == uid
        # logout / revoke-all → session revoked → the still-unexpired access token is refused
        db.auth_sessions.rows[0]["revoked"] = True
        with pytest.raises(auth.TokenRejected) as e:
            run(auth.validate_access_token(db, tok, path="/api/accounts"))
        assert e.value.status == 401 and e.value.detail["code"] == "session_revoked"
        # tokens_valid_after (password change) refuses tokens minted before it — even sid-less ones
        db.auth_sessions.rows[0]["revoked"] = False
        legacy = auth.create_access_token(uid, u["email"])
        u["tokens_valid_after"] = (_now() + timedelta(seconds=5)).isoformat()
        with pytest.raises(auth.TokenRejected):
            run(auth.validate_access_token(db, legacy, path="/api/accounts"))
        # a token without iat (pre-upgrade) is refused outright
        import jwt as _jwt
        old = _jwt.encode({"sub": uid, "email": u["email"], "type": "access", "exp": _now() + timedelta(minutes=5)}, "unit-secret", algorithm="HS256")
        with pytest.raises(auth.TokenRejected):
            run(auth.validate_access_token(db, old, path="/api/accounts"))
    from security import revoke_all_user_sessions
    db2 = FakeDb(); u2 = _user(); db2.users.rows.append(u2)
    run(revoke_all_user_sessions(db2, str(u2["_id"]), "password_change"))
    assert db2.users.rows[0].get("tokens_valid_after")


def test_p1_03_forced_password_change_gates_everything_but_the_change_paths():
    import auth
    db = FakeDb()
    u = _user(must_change_password=True); db.users.rows.append(u)
    with patch.dict(os.environ, {"JWT_SECRET": "unit-secret"}):
        tok = auth.create_access_token(str(u["_id"]), u["email"], sid="s")
        for ok_path in ("/api/auth/me", "/api/auth/change-password", "/api/auth/logout", "/api/auth/refresh"):
            assert run(auth.validate_access_token(db, tok, path=ok_path))["email"] == "u@x.io"
        for bad in ("/api/accounts", "/api/trades", "/api/bot/config"):
            with pytest.raises(auth.TokenRejected) as e:
                run(auth.validate_access_token(db, tok, path=bad))
            assert e.value.status == 403 and e.value.detail["code"] == "password_change_required"
        with pytest.raises(auth.TokenRejected):           # WebSockets (path=None) refuse too
            run(auth.validate_access_token(db, tok, path=None))
    import server
    assert "validate_access_token(get_db(), token, path=None)" in inspect.getsource(server.ws_endpoint)
    assert "password_change_required" in open(os.path.join(ROOT, "frontend", "src", "lib", "api.js")).read()


# ── A9d · P1-01 — hashed bridge tokens (BEHAVIOUR) ────────────────────────────
def test_p1_01_bridge_tokens_hashed_lookup_rotation_retired_and_migration():
    import bridge_tokens as bt
    with patch.dict(os.environ, {"BRIDGE_TOKEN_HASH_KEY": "k"}):
        db = FakeDb()
        legacy = {"_id": ObjectId(), "bridge_token": "tok_legacy", "bridge_token_prev": "tok_older",
                  "bridge_token_suspended": {"token": "tok_legacy", "rule": "R5"}}
        db.accounts.rows.append(legacy)
        assert run(bt.migrate_plaintext(db)) == 1
        row = db.accounts.rows[0]
        assert "bridge_token" not in row and "bridge_token_prev" not in row
        assert row["bridge_token_hash"] == bt.token_hash("tok_legacy") and row["bridge_token_last4"] == "gacy"
        assert row["bridge_token_prev_hash"] == bt.token_hash("tok_older")
        assert row["bridge_token_suspended"]["token_hash"] == bt.token_hash("tok_legacy") and "token" not in row["bridge_token_suspended"]
        # lookups by hash; the EA still sends the same plaintext
        assert run(bt.find_by_current(db, "tok_legacy"))["_id"] == legacy["_id"]
        assert run(bt.find_by_current(db, "tok_wrong")) is None
        assert bt.is_suspended(row, "tok_legacy") and not bt.is_suspended(row, "tok_older")
        # rotation: new hash, old → prev (grace) and retired list; nothing plaintext
        upd = bt.rotation_update(row, "tok_new_1", grace_until="2999-01-01", suspended=False)
        assert "bridge_token" not in upd["$set"] and upd["$set"]["bridge_token_hash"] == bt.token_hash("tok_new_1")
        assert upd["$set"]["bridge_token_prev_hash"] == bt.token_hash("tok_legacy")
        assert upd["$addToSet"]["bridge_token_retired_hashes"] == bt.token_hash("tok_legacy")
        run(db.accounts.update_one({"_id": row["_id"]}, upd))
        assert run(bt.find_by_prev(db, "tok_legacy", "2026-01-01"))["_id"] == legacy["_id"]
        assert run(bt.is_retired(db, "tok_legacy")) and not run(bt.is_retired(db, "tok_never"))
        # S2 — several retired tokens remembered
        run(db.accounts.update_one({"_id": row["_id"]}, bt.rotation_update(db.accounts.rows[0], "tok_new_2", grace_until=None, suspended=True)))
        assert len(db.accounts.rows[0]["bridge_token_retired_hashes"]) == 2
        assert db.accounts.rows[0]["bridge_token_prev_hash"] is None            # suspended → no grace
    # the serializer never returns a hash or plaintext; rotate/revoke are step-up actions
    from step_up import STEP_UP_ACTIONS
    assert {"bridge_token_rotate", "bridge_token_revoke"} <= STEP_UP_ACTIONS
    import routes.account_routes as ar
    ser = inspect.getsource(ar._serialize)
    assert "_bt.strip_plaintext(doc)" in ser and '"bridge_token_hash"' in ser
    assert 'require_step_up(db, user, request, "bridge_token_rotate")' in inspect.getsource(ar.rotate_token)
    assert 'require_step_up(db, user, request, "bridge_token_revoke")' in inspect.getsource(ar.revoke_bridge_token)
    import routes.bridge_routes as br
    assert "bt.find_by_current(db, token)" in inspect.getsource(br._account_by_token)
    import routes.setup_routes as sr
    assert "fresh_token" in inspect.getsource(sr.claim_pairing_token)        # pairing issues a fresh token once
    import seed
    assert "_bt.migrate_plaintext(db)" in inspect.getsource(seed.ensure_indexes)
    import glob
    for f in glob.glob(os.path.join(ROOT, "backend", "routes", "*.py")) + glob.glob(os.path.join(ROOT, "backend", "*.py")):
        if f.endswith("bridge_tokens.py"):
            continue
        assert 'find_one({"bridge_token": ' not in open(f).read(), f    # no plaintext lookups remain


# ── A9d · P1-04 / P1-07 ───────────────────────────────────────────────────────
def test_p1_04_a3_proposes_ip_block_rule_and_target_kinds_are_validated():
    from security_agent import rules
    th = {"thresholds": {"A3": {"accounts_per_ip_10m": 5}}, "ip_block_min": 60, "account_lock_min": 30, "protected_ips": []}
    props = rules.evaluate({"check_id": "A3", "dedup_key": "A3:ip:203.0.113.7", "evidence": {"ip": "203.0.113.7", "accounts": 9}}, th)
    assert props and props[0]["rule"] == "R9" and props[0]["action"] == "block_ip" and props[0]["target_kind"] == "ip" and props[0]["scope"] == "auth"
    assert "A3" not in rules.RULES["R2"]["checks"]
    assert rules.target_kind_valid("lock_login", "account") and not rules.target_kind_valid("lock_login", "ip")
    assert "R9" in open(os.path.join(ROOT, "frontend", "src", "components", "security", "SecurityHealthPanel.jsx")).read()


def test_p1_07_welcome_wording_no_overclaim():
    src = open(os.path.join(ROOT, "frontend", "src", "pages", "WelcomeTrailer.jsx")).read()
    assert "infrastructure trade gold and bitcoin" not in src
    assert "designed for automated gold and bitcoin strategies" in src and "live availability depends on verified connectivity" in src


# ── A9e ───────────────────────────────────────────────────────────────────────
def test_a9e_release_identity_on_health_scorecard_checklist_and_release_evidence():
    import server
    src = inspect.getsource(server.health_release)
    assert '"release_identity"' in src and "accepted_ea_sha256s()" in src and "STOIC_IMAGE_DIGEST" in src
    assert "require_admin(user)" in src  # audit r30 — admin-only probe
    assert '"release_identity"' not in inspect.getsource(server.health)
    assert os.path.exists(os.path.join(ROOT, "frontend", "src", "components", "ReleaseIdentityCard.jsx"))
    assert "<ReleaseIdentityCard />" in open(os.path.join(ROOT, "frontend", "src", "pages", "BotHealth.jsx")).read()
    from security_agent import scorecard
    assert "checklist" in inspect.getsource(scorecard.build) and scorecard.MIN_TARGETS >= 3 and scorecard.MIN_OBSERVE_DAYS >= 14
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "evidence/coverage.xml" in rel and "evidence/junit-critical.xml" in rel and "--cov-report=xml" in rel
    assert "security-scorecard-checklist-" in open(os.path.join(ROOT, "frontend", "src", "components", "security", "ObserveScorecard.jsx")).read()
