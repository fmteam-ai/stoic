from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-239 — audit round 10 corrections.

P0-02 credential scanner · P1-01 inventory fails closed in production + two-admin
expectation · P1-03 user decision dominant reason · P1-02 /state/readiness dominated
by the canonical decision · P1-04 mandatory challenge_ts / production hostname
allowlist · P1-05 server-side degraded routing for tokenless login · P1-06
break-glass step-up + second-admin approval · P1-08 container keepalive ·
P2-02 risk-reducing policy · P2-03 immutable id.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


class TestCredentialScanner:
    def test_working_tree_is_clean_and_top_level_tests_dir_is_scanned(self):
        import secret_scan
        findings = secret_scan.scan_tree(ROOT)
        assert findings == [], findings[:5]
        assert not os.path.exists(os.path.join(ROOT, "tests", "round9_live_test.py"))

    def test_scanner_catches_the_round9_leak_class(self, tmp_path):
        import secret_scan
        (tmp_path / "tests").mkdir()
        who, pw = "adm" + "in@trad" + "ing.bot", "Zq8#rTv2" + "LmN9pXs4Qw"           # assembled: never a literal
        (tmp_path / "tests" / "live.py").write_text(
            f'ADMIN_EMAIL = "{who}"\nADMIN_PASS = "{pw}"\n'
            f'r = s.post(url, json={{"email": "{who}", "password": "{pw}"}})\n')
        kinds = {f["kind"] for f in secret_scan.scan_tree(str(tmp_path))}
        assert "high-entropy-credential-literal" in kinds and "login-credential-pair" in kinds
        (tmp_path / "tests" / "live.py").write_text('PW = os.environ["TEST_ADMIN_PASSWORD"]\nX = "PASTE_YOUR_TOKEN_HERE"\n')
        assert secret_scan.scan_tree(str(tmp_path)) == []

    def test_rotated_credentials_are_refused_by_live_suites(self):
        import live_target
        import hashlib
        lines = [l.split()[0] for l in open(os.path.join(ROOT, "scripts", "retired_credentials.sha256"))
                 if l.strip() and not l.startswith("#")]
        assert len(lines) >= 2 and all(len(h) == 64 for h in lines)
        assert live_target.is_known_default_password("adm" + "in123")
        assert hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest() not in lines
        with pytest.raises(RuntimeError, match="production"):
            live_target.refuse_production_target("https://app.stoicaibot.com")

    def test_ci_and_release_run_the_scanner(self):
        ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
        vr = open(os.path.join(ROOT, "scripts", "verify_release.sh")).read()
        assert "secret_scan.py --history" in ci and "secret_scan.py" in vr and ("adm" + "in123") not in vr


class TestInventoryFailsClosed:
    def test_missing_expectation_or_approval_blocks_in_production(self, monkeypatch):
        from inventory_projection import projection, set_expectation
        from trading_authority import inventory_domain
        db = _db()
        saved = _run(db.platform_state.find_one({"_id": "inventory_expectation"}))
        _run(db.platform_state.delete_one({"_id": "inventory_expectation"}))
        try:
            monkeypatch.setenv("APP_ENV", "production")
            assert _run(inventory_domain(db, None))["level"] == "CLOSE_ONLY"
            p = _run(projection(db, "no-such-user"))
            assert p["blocking"] and any("fails closed" in v for v in p["violations"])
            with pytest.raises(HTTPException):
                _run(set_expectation(db, {"accounts": 6, "enabled": 3, "bots": 3}, "x@stoic.test"))
            monkeypatch.setenv("APP_ENV", "preview")
            assert _run(inventory_domain(db, None))["level"] == "FULL"
        finally:
            if saved:
                _run(db.platform_state.replace_one({"_id": "inventory_expectation"}, saved, upsert=True))

    def test_expectation_requires_a_second_admin(self):
        from inventory_projection import propose_expectation, approve_expectation
        db = _db()
        saved = _run(db.platform_state.find_one({"_id": "inventory_expectation"}))
        try:
            _run(propose_expectation(db, {"accounts": 6, "enabled": 3, "bots": 3, "account_ids": ["a", "b", "c"]}, "one@stoic.test"))
            with pytest.raises(HTTPException) as e:
                _run(approve_expectation(db, "one@stoic.test"))
            assert e.value.detail["code"] == "second_admin_required"
            doc = _run(approve_expectation(db, "two@stoic.test"))
            assert doc["approved_by"] == "two@stoic.test" and doc["proposed_by"] == "one@stoic.test"
            assert _run(db.platform_state.find_one({"_id": "inventory_expectation_pending"})) is None
        finally:
            _run(db.platform_state.delete_one({"_id": "inventory_expectation_pending"}))
            if saved:
                _run(db.platform_state.replace_one({"_id": "inventory_expectation"}, saved, upsert=True))
            else:
                _run(db.platform_state.delete_one({"_id": "inventory_expectation"}))

    def test_projection_exposes_platform_uuid_and_broker_identity_tuple(self):
        src = open(os.path.join(_BACKEND_DIR, "inventory_projection.py")).read()
        assert '"immutable_id"' not in src and '"broker_identity"' in src


class TestCanonicalDecisionRound10:
    def test_user_dominant_code_comes_from_the_worst_blocker(self, monkeypatch):
        import canonical_decision as cd

        async def _platform(db):
            return {"decision_id": "p", "snapshot_id": None, "state": "DEGRADED", "level": "REDUCED",
                    "new_exposure_allowed": True, "dominant_code": "TERMINAL_STALE", "reason_codes": ["TERMINAL_STALE"],
                    "blockers": [{"domain": "infrastructure", "code": "TERMINAL_STALE", "state": "DEGRADED", "level": "REDUCED", "reason": "x"}],
                    "domains": {}, "scope": "platform", "account_id": None, "computed_at": "now", "dominance": cd.STATES[::-1]}

        class _Cursor:
            def __init__(self, rows):
                self.rows = rows

            def __aiter__(self):
                async def gen():
                    for r in self.rows:
                        yield r
                return gen()

        class _Accounts:
            def find(self, q):
                return _Cursor([{"_id": "acc1", "label": "L1", "mode": "live"}])

        class _DB:
            accounts = _Accounts()

        async def _account(db, acc):
            return {"decision_id": "a", "state": "BLOCKED", "reason_codes": ["IDENTITY_MISMATCH", "EXECUTION_UNKNOWN"],
                    "blockers": [{"domain": "execution", "code": "EXECUTION_UNKNOWN", "state": "CLOSE_ONLY", "level": "CLOSE_ONLY", "reason": "u"},
                                 {"domain": "account", "code": "IDENTITY_MISMATCH", "state": "BLOCKED", "level": "LOCKED", "reason": "m"}],
                    "account_id": "acc1", "new_exposure_allowed": False}
        monkeypatch.setattr(cd, "decide_platform", _platform)
        monkeypatch.setattr(cd, "decide_account", _account)
        d = _run(cd.decide_user(_DB(), "u1"))
        assert d["state"] == "BLOCKED" and d["dominant_code"] == "IDENTITY_MISMATCH" and d["dominant_account_id"] == "acc1"
        assert [b["state"] for b in d["blockers"]] == ["BLOCKED", "CLOSE_ONLY", "DEGRADED"]
        assert d["reason_codes"][0] == "IDENTITY_MISMATCH" and d["blockers"][0]["account_label"] == "L1"
        real = cd.from_snapshot({"domains": {"a": {"level": "EMERGENCY", "reason": "x"}}, "level": "EMERGENCY"})
        assert real["risk_reducing_allowed"] is True and real["close_allowed"] is True and real["state"] == "EMERGENCY"

    def test_state_readiness_is_dominated_by_the_canonical_decision(self):
        src = open(os.path.join(_BACKEND_DIR, "trading_readiness.py")).read()
        assert "decide_user" in src and "dominant(level, dec[\"state\"])" in src
        src2 = open(os.path.join(_BACKEND_DIR, "canonical_decision.py")).read()
        assert 'or True' not in src2


class TestTurnstileRound10:
    def test_missing_or_future_challenge_ts_fails(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.delenv("TURNSTILE_EXPECTED_HOSTNAMES", raising=False)
        ok, codes = tg.bind_claims({"success": True, "action": "login"}, "login")
        assert not ok and "challenge-ts-missing" in codes
        ok, codes = tg.bind_claims({"success": True, "action": "login", "challenge_ts": "garbage"}, "login")
        assert not ok and "challenge-ts-missing" in codes
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        ok, codes = tg.bind_claims({"success": True, "action": "login", "challenge_ts": future}, "login")
        assert not ok and "token-from-future" in codes

    def test_production_requires_hostname_allowlist(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "s")
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "k")
        monkeypatch.delenv("TURNSTILE_EXPECTED_HOSTNAMES", raising=False)
        assert "TURNSTILE_EXPECTED_HOSTNAMES" in tg.production_config_violation(True)
        monkeypatch.setenv("TURNSTILE_EXPECTED_HOSTNAMES", "app.example.com")
        assert tg.production_config_violation(True) is None

    def test_tokenless_login_routes_to_otp_only_on_server_degraded_state(self, monkeypatch):
        import turnstile_gate as tg
        import turnstile_break_glass as tbg
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "k")
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "s")
        monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", "otp_required")
        monkeypatch.setenv("APP_ENV", "preview")
        monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)

        async def _enabled(db):
            return True

        async def _no_bg(db):
            return None
        called = []

        async def _verify(token, ip=None, action=None):
            called.append(token)
            return {"ok": False, "state": "client_token_invalid", "outage": False,
                    "error_codes": ["missing-input-response"], "hostname": None, "action": action, "challenge_ts": None}
        monkeypatch.setattr(tg, "is_enabled", _enabled)
        monkeypatch.setattr(tbg, "active", _no_bg)
        monkeypatch.setattr(tg, "verify_token", _verify)
        # client CLAIMS degradation (tokenless) but the server saw nothing → denied as before
        monkeypatch.setattr(tg, "provider_recently_degraded", lambda: False)
        d = _run(tg.evaluate(object(), "", "1.1.1.1", action="login"))
        assert not d.allow and d.error.status_code == 403
        # server itself recently failed siteverify → typed degraded decision, no verify call
        monkeypatch.setattr(tg, "provider_recently_degraded", lambda: True)
        called.clear()
        d = _run(tg.evaluate(object(), "", "1.1.1.1", action="login"))
        assert d.allow and d.mode == "degraded_otp_required" and called == []
        # never for register, never with closed policy
        d = _run(tg.evaluate(object(), "", "1.1.1.1", action="register"))
        assert not d.allow
        monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", "closed")
        d = _run(tg.evaluate(object(), "", "1.1.1.1", action="login"))
        assert not d.allow

    def test_break_glass_mutations_require_step_up(self):
        src = open(os.path.join(_BACKEND_DIR, "routes", "admin_routes.py")).read()
        for fn in ("admin_break_glass_request", "admin_break_glass_approve", "admin_break_glass_deactivate", "admin_break_glass_review"):
            body = src[src.index(f"async def {fn}"):]
            body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
            assert "require_step_up(" in body, fn


class TestContainerAndCopy:
    def test_dockerfile_keepalive_is_bounded(self):
        df = open(os.path.join(ROOT, "Dockerfile.backend")).read()
        import re
        m = re.search(r'"--timeout-keep-alive",\s*"(\d+)"', df)
        assert m and int(m.group(1)) <= 300
        srv = open(os.path.join(_BACKEND_DIR, "server.py")).read()
        assert "300)" in srv[srv.index("_extend_uvicorn_keepalive"):][:1200]

    def test_no_static_trust_copy(self):
        login = open(os.path.join(ROOT, "frontend", "src", "pages", "Login.jsx")).read()
        bar = open(os.path.join(ROOT, "frontend", "src", "components", "StatusBar.jsx")).read()
        assert "// SYSTEM READY" not in login and 'data-testid="login-system-state"' in login
        assert "EU SERVERS" not in bar and "deployment?.region" in bar

    def test_build_sha_resolves_in_checkout(self):
        from modules.pamm.strategy_guard import GIT_COMMIT
        head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
        assert GIT_COMMIT == head


class TestProdShorthandAgreement:
    """Audit round 11 SEC-001 — every round-9/10 guardrail must treat APP_ENV=prod as production."""

    def test_inventory_turnstile_signer_agree_on_prod(self, monkeypatch):
        import inventory_projection as ip
        import turnstile_gate as tg
        import release_signing as rs
        from trading_authority import inventory_domain
        db = _db()
        saved = _run(db.platform_state.find_one({"_id": "inventory_expectation"}))
        _run(db.platform_state.delete_one({"_id": "inventory_expectation"}))
        try:
            for val in ("prod", "production", "PROD"):
                monkeypatch.setenv("APP_ENV", val)
                assert ip.production_mode() and tg.is_production() and rs._is_prod({"APP_ENV": val}), val
                assert _run(inventory_domain(db, None))["level"] == "CLOSE_ONLY", val
                assert _run(ip.projection(db, "no-such-user"))["blocking"] is True, val
            monkeypatch.setenv("APP_ENV", "preview")
            assert not ip.production_mode() and not tg.is_production()
        finally:
            if saved:
                _run(db.platform_state.replace_one({"_id": "inventory_expectation"}, saved, upsert=True))


class TestRound11Corrections:
    """Audit round 11 — P1-01/02/03/04/05, P2-01/02/03."""

    @pytest.fixture
    def fleet(self):
        import uuid
        from bson import ObjectId
        db = _db()
        uid = f"iter241_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for i, (mode, en) in enumerate([("live", True), ("live", True), ("live", True), ("live", False), ("demo", False), ("demo", False)]):
            n = str(820000 + i)
            rows.append({"_id": ObjectId(), "user_id": uid, "label": f"R11-{i}", "mode": mode, "trading_enabled": en,
                         "status": "connected", "last_heartbeat": now, "open_positions": 0, "account_number": n,
                         "account_login": n, "bridge_token": f"r11_{uuid.uuid4().hex}",
                         "verified_identity": {"account_number": n, "broker_server": "X"}, "created_at": now})
        _run(db.accounts.insert_many(rows))
        for r in rows:
            _run(db.bot_configs.insert_one({"user_id": uid, "account_id": str(r["_id"]), "active": r["trading_enabled"], "created_at": now}))
        yield uid, rows
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.bot_configs.delete_many({"user_id": {"$in": [uid, uid + "_x"]}}))
        _run(db.inventory_config_events.delete_many({"actor": "iter241@stoic.test"}))

    def test_duplicate_and_orphan_bots_block_and_are_reported(self, fleet):
        from inventory_projection import projection, approve_current
        uid, rows = fleet
        db = _db()
        clean = _run(projection(db, uid))
        assert clean["violations"] == [] and clean["counts"]["bots_active_raw"] == 3
        # (user_id, account_id) is unique-indexed, so a duplicate can only arrive from another tenant row
        _run(db.bot_configs.insert_one({"user_id": uid + "_x", "account_id": str(rows[0]["_id"]), "active": True}))
        _run(db.bot_configs.insert_one({"user_id": uid, "account_id": "000000000000000000000000", "active": True}))
        p = _run(projection(db, None if False else uid, include_foreign_bots=True))
        assert p["blocking"] and p["counts"]["bots_duplicate"] == 1 and p["counts"]["bots_orphan"] == 1
        assert p["counts"]["bots_active_raw"] == 5 and p["counts"]["bots_enabled"] == 3
        assert any("2 bot configurations" in v for v in p["violations"]) and any("unknown accounts" in v for v in p["violations"])
        with pytest.raises(HTTPException) as e:                 # P2-03: invalid inventory is never "approved"
            _run(approve_current(db, "iter241@stoic.test", "should be refused", uid))
        assert e.value.status_code == 409

    def test_expectation_policy_validation(self, monkeypatch):
        from inventory_projection import validate_expectation
        bad = [{"accounts": -1, "enabled": 0, "bots": 0}, {"accounts": 2, "enabled": 3, "bots": 3},
               {"accounts": 6, "enabled": 3, "bots": 2}, {"accounts": 6, "enabled": 3, "bots": 3, "account_ids": ["a", "a", "b"]},
               {"accounts": 6, "enabled": 3, "bots": 3, "account_ids": ["a", "b"]}]
        for b in bad:
            with pytest.raises(HTTPException):
                validate_expectation(b, require_policy=False)
        ok = {"accounts": 6, "enabled": 3, "bots": 3, "account_ids": ["a", "b", "c"]}
        assert validate_expectation(ok, require_policy=True)["enabled"] == 3
        with pytest.raises(HTTPException) as e:
            validate_expectation({"accounts": 4, "enabled": 4, "bots": 4, "account_ids": list("abcd")}, require_policy=True)
        assert any("6/3/3" in x for x in e.value.detail["errors"])
        # a migration signed by the release key is the only way to change the target
        import json as _json
        from release_signing import sign_hex
        body = _json.dumps({"accounts": 4, "enabled": 4, "bots": 4, "migration_id": "MIG-1"}, sort_keys=True, separators=(",", ":")).encode()
        mig = {"accounts": 4, "enabled": 4, "bots": 4, "account_ids": list("abcd"),
               "policy_migration": {"migration_id": "MIG-1", "signature_hex": sign_hex(body)}}
        assert validate_expectation(mig, require_policy=True)["accounts"] == 4
        mig["policy_migration"]["signature_hex"] = "00" * 64
        with pytest.raises(HTTPException):
            validate_expectation(mig, require_policy=True)

    def test_model_manifest_gates_joblib_load(self, tmp_path, monkeypatch):
        import model_manifest as mm
        monkeypatch.setattr(mm, "MODEL_DIR", tmp_path)
        monkeypatch.setattr(mm, "MANIFEST", tmp_path / "MODEL_MANIFEST.json")
        monkeypatch.setattr(mm, "QUARANTINE", tmp_path / "_quarantine")
        (tmp_path / "u1").mkdir()
        f = tmp_path / "u1" / "gbm_ensemble.joblib"
        f.write_bytes(b"\x80\x04model-bytes")
        with pytest.raises(mm.ModelRefused, match="no signed"):
            mm.verify_model(f)
        mm.sign(["a@x", "b@y"])
        assert mm.verify_model(f) == mm.sha256_file(f)
        f.write_bytes(b"\x80\x04model-bytez")                       # one-byte mutation
        with pytest.raises(mm.ModelRefused, match="digest mismatch"):
            mm.verify_model(f)
        assert list((tmp_path / "_quarantine").glob("*.refused.json"))
        f.write_bytes(b"\x80\x04model-bytes")
        with pytest.raises(mm.ModelRefused, match="feature schema"):
            mm.verify_model(f, expected_schema="ens-features-v9")
        doc = json.loads((tmp_path / "MODEL_MANIFEST.json").read_text())
        doc["body"]["approvers"] = ["evil@x"]
        (tmp_path / "MODEL_MANIFEST.json").write_text(json.dumps(doc))
        with pytest.raises(mm.ModelRefused, match="signature"):
            mm.verify_model(f)
        src = open(os.path.join(_BACKEND_DIR, "ml_ensemble.py")).read()
        assert "verify_model(f)" in src and src.index("verify_model(f)") < src.index("models = joblib.load(f)")
        assert "gbm_ensemble.candidate.joblib" in src
        assert json.load(open(os.path.join(ROOT, "release", "rc_lock.json")))["model_manifest_sha256"]

    def test_turnstile_malformed_provider_bodies_fail_closed(self, monkeypatch):
        import turnstile_gate as tg
        import httpx

        class _Resp:
            status_code = 200

            def __init__(self, payload, raw=False):
                self.p, self.raw = payload, raw

            def json(self):
                if self.raw:
                    raise ValueError("bad json")
                return self.p

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, data=None):
                return _Client.resp
        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "k")
        for payload, raw in ((None, True), ([1, 2], False), (None, False), ({"success": True, "error-codes": "x"}, False)):
            _Client.resp = _Resp(payload, raw)
            r = _run(tg.verify_token("tok", "1.1.1.1", action="login"))
            assert r["ok"] is False and r["state"] in ("provider_unavailable", "client_token_invalid"), (payload, r)
        assert tg._token_age_seconds("2026-01-01T00:00:00") is not None        # naive → UTC, no TypeError
        assert tg._token_age_seconds(12345) is None and tg._token_age_seconds(None) is None

    def test_frontend_never_unlocks_on_client_script_failure(self):
        src = open(os.path.join(ROOT, "frontend", "src", "components", "TurnstileWidget.jsx")).read()
        assert 'cfgState === "provider-degraded";' in src and 'state === "script-error" ||' not in src

    def test_break_glass_pending_visible_blocking_and_not_overwritable(self):
        import turnstile_break_glass as tbg
        db = _db()
        saved = _run(db.platform_state.find_one({"_id": "turnstile_break_glass"}))
        _run(db.platform_state.delete_one({"_id": "turnstile_break_glass"}))
        _run(db.platform_state.delete_one({"_id": tbg.PENDING_ID}))
        good = {"incident_id": "INC-241", "approver": "approver@stoic.test",
                "reason": "round 11 pending-request visibility and overwrite refusal drill", "scope": ["login"], "ttl_minutes": 5}
        try:
            _run(tbg.request_activation(db, good, "actor@stoic.test"))
            st = _run(tbg.status(db))
            assert st["record"] is None and st["pending_request"]["incident_id"] == "INC-241"
            assert st["promotion_blocked"] and not st["active"]
            assert _run(tbg.readiness_check(db))["ok"] is False
            with pytest.raises(HTTPException) as e:
                _run(tbg.request_activation(db, {**good, "incident_id": "INC-242"}, "other@stoic.test"))
            assert e.value.status_code == 409
            assert _run(db.platform_state.find_one({"_id": tbg.PENDING_ID}))["incident_id"] == "INC-241"
            _run(tbg.cancel_request(db, "actor@stoic.test", "drill over"))
            assert _run(tbg.status(db))["pending_request"] is None and _run(tbg.readiness_check(db))["ok"]
        finally:
            _run(db.platform_state.delete_one({"_id": tbg.PENDING_ID}))
            if saved:
                _run(db.platform_state.replace_one({"_id": "turnstile_break_glass"}, saved, upsert=True))

    def test_readiness_persists_the_same_snapshot_it_returns(self, monkeypatch):
        import trading_readiness as tr
        import canonical_decision as cd
        import uuid
        uid = f"iter241r_{uuid.uuid4().hex[:6]}"
        db = _db()

        async def _dec(db_, user_id, fresh=False):
            return {"decision_id": "dec_test", "state": "BLOCKED", "dominant_code": "IDENTITY_MISMATCH",
                    "blockers": [{"code": "IDENTITY_MISMATCH", "state": "BLOCKED", "reason": "m", "account_label": "A"}]}
        monkeypatch.setattr(cd, "decide_user", _dec)
        try:
            out = _run(tr.readiness(db, uid))
            stored = _run(db.trading_readiness.find_one({"_id": uid}))
            assert out["level"] == "BLOCKED" == stored["level"] and stored["decision_id"] == out["decision_id"] == "dec_test"
            assert out["reasons"][0]["code"] == "IDENTITY_MISMATCH" and stored["reason_codes"][0] == "IDENTITY_MISMATCH"
            fs = out["reasons"][0]["first_seen"]
            out2 = _run(tr.readiness(db, uid))
            assert out2["reasons"][0]["first_seen"] == fs
        finally:
            _run(db.trading_readiness.delete_one({"_id": uid}))
