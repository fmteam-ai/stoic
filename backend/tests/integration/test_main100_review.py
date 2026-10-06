"""Main100 review backend integration tests (iter 238).

Covers STOIC main100 Review deltas on top of the preview-live backend:
- Regression smoke (status, acceptance/current contract, crypto exchanges only-binance-live).
- N100-8: /api/performance/verified provenance is 'derived' with STATEMENT_LEDGER_MISSING
  in ledger_gate_reasons and share_allowed=False when the admin has no enabled accounts.
- N100-6/A14-7: /api/ops/release-readiness returns structured JSON (never 500); the
  ea_release / release-gate check does not fail purely because EA_RELEASE_SHA256 env is set.
- SEC-002: /api/infra/agent/register with a bogus bootstrap_token returns
  {code,message,ref} with a 10-char ref, no raw str(e) / Traceback leak.
- No endpoint touched by this iteration returns HTTP 500.

Credentials are read at run time from TEST_ADMIN_EMAIL/TEST_ADMIN_PASSWORD (fallback
ADMIN_EMAIL/ADMIN_PASSWORD). Suite skips cleanly when no live target is configured.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))

import re
import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()


# ────────── helpers / fixtures ──────────
def _login(session: requests.Session, email: str, password: str) -> requests.Response:
    return session.post(f"{BASE_URL}/api/auth/login",
                        json={"email": email, "password": password}, timeout=15)


def _csrf_headers(session: requests.Session) -> dict:
    tok = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    s = requests.Session()
    r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    return s


@pytest.fixture(scope="module")
def anon_session() -> requests.Session:
    return requests.Session()


# ────────── Regression smoke ──────────
class TestRegressionSmoke:
    def test_status(self, anon_session):
        r = anon_session.get(f"{BASE_URL}/api/status", timeout=15)
        assert r.status_code == 200, r.text[:300]

    def test_admin_login_works(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10)
        assert r.status_code == 200, r.text[:300]
        assert r.json().get("role") == "admin"

    def test_acceptance_current_contract(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
        # Must be 200 or a structured 503 (never 500)
        assert r.status_code != 500, f"500 from acceptance/current: {r.text[:300]}"
        assert r.status_code in (200, 503), f"unexpected {r.status_code}: {r.text[:300]}"
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 503:
            # structured error envelope
            detail = body.get("detail", body)
            if isinstance(detail, dict):
                assert detail.get("code") == "bundle_unavailable", f"expected bundle_unavailable, got {detail}"
            assert "Traceback" not in r.text
        else:
            # 200 contract shape
            for k in ("required", "release", "latest", "valid_for_release", "reason", "accounts"):
                assert k in body, f"missing {k}"

    def test_crypto_exchanges_only_binance_live(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=20)
        assert r.status_code == 200, r.text[:300]
        exchanges = (r.json() or {}).get("exchanges") or []
        assert exchanges, "no exchanges returned"
        live_allowed_ids = []
        for e in exchanges:
            caps = e.get("capabilities")
            if caps is not None:
                assert isinstance(caps, dict), f"capabilities not a dict for {e.get('id')}"
                if caps.get("live_allowed") is True:
                    live_allowed_ids.append(e["id"])
        assert live_allowed_ids == ["binance"], \
            f"expected ONLY binance to have live_allowed=True, got {live_allowed_ids}"


# ────────── N100-8: verified-performance ledger_gate ──────────
class TestVerifiedProvenanceLedgerGate:
    def test_derived_with_statement_ledger_missing(self, admin_session):
        # Preview admin typically has zero enabled MT5 accounts, so the ledger gate
        # must surface STATEMENT_LEDGER_MISSING, provenance.source_kind='derived',
        # share_allowed=False.
        r = admin_session.get(f"{BASE_URL}/api/performance/verified", timeout=20)
        assert r.status_code == 200, r.text[:400]
        j = r.json()
        prov = j.get("provenance") or {}
        assert prov, f"provenance missing: keys={list(j.keys())}"
        sk = prov.get("source_kind")
        reasons = prov.get("ledger_gate_reasons")
        assert isinstance(reasons, list), f"ledger_gate_reasons not list: {reasons!r}"

        # First, check if admin has any enabled accounts via the accounts list.
        accs_r = admin_session.get(f"{BASE_URL}/api/accounts", timeout=15)
        acc_rows = []
        if accs_r.status_code == 200:
            data = accs_r.json()
            acc_rows = data if isinstance(data, list) else (data or {}).get("accounts") or []
        enabled = [a for a in acc_rows
                   if a.get("enabled") is True and a.get("mode") != "paper"]

        if not enabled:
            # N100-8 contract: ledger gate must report STATEMENT_LEDGER_MISSING
            assert "STATEMENT_LEDGER_MISSING" in reasons, \
                f"expected STATEMENT_LEDGER_MISSING in {reasons}"
            assert sk == "derived", f"source_kind expected 'derived' got {sk!r}"
            assert prov.get("share_allowed") is False, \
                f"share_allowed must be False when derived (got {prov.get('share_allowed')})"
            assert j.get("share_allowed") is False
        else:
            # Admin DOES have enabled accounts — N100-8 still mandates no bogus pass.
            # Either reconciled OR derived; if derived, share_allowed False.
            assert sk in ("derived", "broker_reconciled"), f"source_kind={sk!r}"
            if sk == "derived":
                assert prov.get("share_allowed") is False


# ────────── N100-6 / A14-7: release-readiness release-gate ──────────
class TestReleaseReadinessReleaseGate:
    def test_structured_json_never_500(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/release-readiness", timeout=30)
        # Must be structured JSON — 200 (all green) or 503 (not ready), never 500.
        assert r.status_code != 500, f"500 from release-readiness: {r.text[:400]}"
        assert r.status_code in (200, 503), f"unexpected {r.status_code}: {r.text[:300]}"
        j = r.json()
        assert isinstance(j, dict), f"non-dict body: {r.text[:200]}"
        assert "ready" in j and "checks" in j, f"shape: {list(j.keys())}"
        checks = j["checks"]
        assert isinstance(checks, dict)
        assert "Traceback" not in r.text

    def test_release_record_does_not_fail_purely_on_env_pin(self, admin_session):
        """N100-6: if an EX5 signed release record exists (docs/RELEASE_HASHES.json),
        the ea_release / release-gate check must count it even when
        EA_RELEASE_SHA256 env is set. We cannot flip env from the test, but we can
        assert that:
          (a) The ea_release check does NOT reference the specific anti-pattern
              'no signed EX5 release record' as a failure when the signed record is
              demonstrably present on disk.
          (b) The admin /release-gate endpoint responds structured JSON (no 500) and
              its reasons don't contain the regressed string either.
        """
        r = admin_session.get(f"{BASE_URL}/api/ops/release-readiness", timeout=30)
        assert r.status_code in (200, 503)
        j = r.json()
        checks = j.get("checks") or {}
        ea = checks.get("ea_release") or {}
        failures = ea.get("failures") or []
        # Normalize to list of strings
        if isinstance(failures, (list, tuple)):
            text = " ".join(str(x) for x in failures).lower()
        else:
            text = str(failures).lower()
        # Determine if a signed EX5 release record is present on this host.
        import json as _json
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))))
        hashes_path = _os.path.join(root, "..", "docs", "RELEASE_HASHES.json")
        hashes_path = _os.path.normpath(hashes_path)
        signed_record_present = False
        if _os.path.exists(hashes_path):
            try:
                data = _json.load(open(hashes_path))
                signed_record_present = bool((data.get("ea") or {}).get("signature"))
            except Exception:
                signed_record_present = False
        if signed_record_present:
            # The exact regression wording from the review PDF
            assert "no signed ex5 release record" not in text, (
                f"N100-6 regression: ea_release reports 'no signed EX5 release record' "
                f"even though a signed record is present on disk. failures={failures}")

        # Admin /release-gate endpoint — also structured, no 500
        rg = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=20)
        assert rg.status_code != 500, f"500 from /admin/release-gate: {rg.text[:300]}"
        if rg.status_code == 200:
            body = rg.json()
            # Collect any flat text we can scan for the regressed phrase.
            flat = _json.dumps(body).lower()
            if signed_record_present:
                assert "no signed ex5 release record" not in flat, (
                    f"N100-6 regression in /admin/release-gate: {flat[:400]}")


# ────────── SEC-002: error envelope on infra/agent/register ──────────
class TestErrorEnvelopeAgentRegister:
    def test_bogus_token_returns_structured_envelope(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/infra/agent/register",
                   json={"bootstrap_token": "DEFINITELY_BOGUS_TOKEN_XYZ"},
                   timeout=15)
        assert r.status_code != 500, f"500 from infra/agent/register: {r.text[:300]}"
        # Expected: 401 agent_registration_refused per infra_routes.py
        assert r.status_code in (400, 401, 403), f"unexpected {r.status_code}: {r.text[:300]}"
        assert r.headers.get("content-type", "").startswith("application/json"), \
            f"non-json body: {r.text[:200]}"
        body = r.json()
        detail = body.get("detail", body)
        assert isinstance(detail, dict), f"detail not a dict: {detail!r}"
        for k in ("code", "message", "ref"):
            assert k in detail, f"missing {k} in error envelope: {detail}"
        assert isinstance(detail["ref"], str) and len(detail["ref"]) == 10 \
            and re.fullmatch(r"[0-9a-f]{10}", detail["ref"]), \
            f"ref must be a 10-char hex string, got {detail.get('ref')!r}"
        # No internal exception text leaks
        assert "Traceback" not in r.text
        assert "<class '" not in r.text
        # Message should be the generic/static one, not raw str(e). The code
        # must be the stable machine-readable identifier.
        assert detail["code"], "code missing/empty"
        # Shouldn't echo the bogus token value back
        assert "DEFINITELY_BOGUS_TOKEN_XYZ" not in r.text


# ────────── No-500 sweep across the endpoints this iteration touches ──────────
class TestNo500Sweep:
    @pytest.mark.parametrize("path", [
        "/api/status",
        "/api/admin/acceptance/current",
        "/api/authority/inventory",
        "/api/authority/inventory/pending",
        "/api/crypto/exchanges",
        "/api/performance/verified",
        "/api/ops/release-readiness",
        "/api/admin/release-gate",
        "/api/accounts",
    ])
    def test_no_500(self, admin_session, path):
        r = admin_session.get(f"{BASE_URL}{path}", timeout=25)
        assert r.status_code != 500, f"500 from {path}: {r.text[:400]}"
        assert "Traceback" not in r.text, f"traceback leak on {path}: {r.text[:200]}"
