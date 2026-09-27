from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-239 — audit round 10 corrections.

P0-02 credential scanner · P1-01 inventory fails closed in production + two-admin
expectation · P1-03 user decision dominant reason · P1-02 /state/readiness dominated
by the canonical decision · P1-04 mandatory challenge_ts / production hostname
allowlist · P1-05 server-side degraded routing for tokenless login · P1-06
break-glass step-up + second-admin approval · P1-08 container keepalive ·
P2-02 risk-reducing policy · P2-03 immutable id.
"""
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
