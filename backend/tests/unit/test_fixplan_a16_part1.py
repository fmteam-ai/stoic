"""A16 Part 1: A16-1 registration fails closed; A16-2 status page loading/timeout state; A16-3 one validation
rule per policy field (workflow = script = server); A16-4 demo policy lifetime (30 default / 45 max, expiry →
close-only violation + readiness row + Telegram reminder/alert)."""
import asyncio
import base64
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8-sig").read()


# ── A16-1 ────────────────────────────────────────────────────────────────────────────────────────
def test_a16_1_registration_fails_closed_and_uses_one_wording():
    reg = _read("frontend/src/pages/Register.jsx")
    assert ".catch(() => setSignups({ unavailable: true }))" in reg and "setSignups({ closed: false })" not in reg
    assert 'data-testid="register-status-unavailable"' in reg and "Registration status unavailable — try again later." in reg
    assert 'data-testid="register-status-loading"' in reg                    # no form while the answer is pending
    assert reg.index('data-testid="register-status-loading"') < reg.index('data-testid="register-status-unavailable"') < reg.index('data-testid="register-closed"')
    assert "closed testing period" in reg.lower() and "closed beta" not in reg.lower()
    for rel in ("frontend/src/components/ClosedBetaBanner.jsx", "frontend/src/pages/Affiliate.jsx"):
        txt = _read(rel)
        assert "closed beta" not in txt.lower().replace("closedbetabanner", ""), rel
    aff = _read("frontend/src/pages/Affiliate.jsx")
    assert 'setSignupsClosed("unavailable")' in aff and 'signupsClosed === false && (' in aff and 'data-testid="affiliate-status-unavailable"' in aff
    assert "testing period" in _read("backend/signup_lock.py")


# ── A16-2 ────────────────────────────────────────────────────────────────────────────────────────
def test_a16_2_status_page_has_text_timeout_and_last_result_time():
    sp = _read("frontend/src/pages/StatusPage.jsx")
    assert "const STATUS_TIMEOUT_MS = 8000;" in sp and "{ timeout: STATUS_TIMEOUT_MS }" in sp
    assert 'data-testid="status-checking"' in sp and "CHECKING STATUS…" in sp
    assert "Status unavailable (checked ${utc(checkedAt)})" in sp and 'data-testid="status-last-result"' in sp
    assert "setTimedOut(true)" in sp and "(err || timedOut)" in sp


# ── A16-3 ────────────────────────────────────────────────────────────────────────────────────────
def test_a16_3_one_validation_rule_per_policy_field():
    import inventory_projection as ip
    wf = _read(".github/workflows/policy-migration.yml")
    assert f'[[ "$IN_VERSION" =~ ^{ip.POLICY_VERSION_RE}$ ]]' in wf
    assert f'[[ "$IN_PREVIOUS" =~ ^{ip.PREVIOUS_POLICY_VERSION_RE}$ ]]' in wf
    script = _read("scripts/sign_policy_migration.py")
    assert "re.fullmatch(POLICY_VERSION_RE, args.version)" in script and "re.fullmatch(PREVIOUS_POLICY_VERSION_RE, args.previous)" in script
    assert 'r"[A-Za-z0-9._-]{1,64}"' not in script                             # no second copy of the rule
    assert re.fullmatch(ip.POLICY_VERSION_RE, "demo-2x2-v1") and not re.fullmatch(ip.POLICY_VERSION_RE, "demo/2x2")
    assert re.fullmatch(ip.PREVIOUS_POLICY_VERSION_RE, "6/3/3-v1")
    # the bash test in the workflow rejects demo/2x2 BEFORE signing
    import subprocess
    r = subprocess.run(["bash", "-c", f'[[ "demo/2x2" =~ ^{ip.POLICY_VERSION_RE}$ ]] && echo ok || echo bad'], capture_output=True, text=True)
    assert r.stdout.strip() == "bad"
    r = subprocess.run(["bash", "-c", f'[[ "6/3/3-v1" =~ ^{ip.PREVIOUS_POLICY_VERSION_RE}$ ]] && echo ok || echo bad'], capture_output=True, text=True)
    assert r.stdout.strip() == "ok"
    assert wf.index("bad policy_version") < wf.index("sign_policy_migration.py")
    # server rejects the same shapes
    probs = ip.migration_problems({"policy_migration": {"signature_hex": "00", "policy_version": "demo/2x2", "previous_policy_version": "6/3/3-v1"}},
                                  current_policy_version="6/3/3-v1", now=NOW)
    assert any("policy_version must match" in p for p in probs)


# ── A16-4 ────────────────────────────────────────────────────────────────────────────────────────
def _signed_migration(ip, rs, *, demo_only=True, days=30, now=NOW):
    mig = {"schema": ip.MIGRATION_SCHEMA, "installation_id": "inst-x", "environment": ip.environment_label(),
           "previous_policy_version": "6/3/3-v1", "policy_version": "demo-2x2-v1", "accounts": 2, "enabled": 2, "bots": 2,
           "account_ids": ["a", "b"], "demo_only": demo_only, "reason": "four-week demo test", "issuer": "ci",
           "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=days)).isoformat(), "nonce": "n" * 16}
    mig["signature_hex"] = rs.sign_hex(ip.migration_body(mig), purpose="policy-migration")
    return mig


def _signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "RELEASE_SIGNER_DEFERRED": "false", "STOIC_INSTALLATION_ID": "inst-x",
            "ED25519_SIGNING_KEY_B64": base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode(),
            "RELEASE_PUBLIC_KEY_B64": base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()}


def test_a16_4_demo_policy_lifetime_rules():
    import inventory_projection as ip
    import release_signing as rs
    assert (ip.DEMO_POLICY_DEFAULT_DAYS, ip.DEMO_POLICY_MAX_DAYS, ip.POLICY_EXPIRY_REMINDER_DAYS) == (30, 45, 3)
    with patch.dict(os.environ, _signer_env()):
        payload = {"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ["a", "b"]}
        ok = {**payload, "policy_migration": _signed_migration(ip, rs, days=30)}
        assert ip.migration_problems(ok, current_policy_version="6/3/3-v1", now=NOW) == []
        too_long = {**payload, "policy_migration": _signed_migration(ip, rs, days=46)}
        assert any("at most 45 days" in p for p in ip.migration_problems(too_long, current_policy_version="6/3/3-v1", now=NOW))
        live_long = {**payload, "policy_migration": _signed_migration(ip, rs, demo_only=False, days=90)}
        assert not any("at most" in p for p in ip.migration_problems(live_long, current_policy_version="6/3/3-v1", now=NOW))
    script = _read("scripts/sign_policy_migration.py")
    assert "DEMO_POLICY_DEFAULT_DAYS if args.demo_only else 45" in script and 'default=None' in script
    wf = _read(".github/workflows/policy-migration.yml")
    assert 'default: "30"' in wf and '[ "$IN_EXPIRES_DAYS" -gt 45 ]' in wf


def test_a16_4_expired_policy_is_close_only_and_alerts():
    import inventory_projection as ip
    import policy_expiry_alerts as pea
    exp = {"policy_version": "demo-2x2-v1", "demo_only": True, "policy_expires_at": (NOW - timedelta(hours=1)).isoformat()}
    pe = ip.policy_expiry(exp, NOW)
    assert pe["expired"] and pe["days_left"] == 0
    soon = {**exp, "policy_expires_at": (NOW + timedelta(days=2)).isoformat()}
    pe2 = ip.policy_expiry(soon, NOW)
    assert not pe2["expired"] and pe2["reminder_due"] and pe2["days_left"] == 2
    far = ip.policy_expiry({**exp, "policy_expires_at": (NOW + timedelta(days=20)).isoformat()}, NOW)
    assert not far["reminder_due"] and far["days_left"] == 20
    assert ip.policy_expiry({"policy_version": "6/3/3-v1"}, NOW) == {"expires_at": None, "expired": False, "days_left": None, "reminder_due": False}
    # projection: the expired policy is a violation → trading_authority turns the inventory CLOSE_ONLY
    src = _read("backend/inventory_projection.py")
    assert "inventory close-only until a new signed policy is approved" in src[src.index("async def projection"):src.index("def _reservation_scope") if "def _reservation_scope" in src else len(src)]
    ta = _read("backend/trading_authority.py")
    assert '"level": "CLOSE_ONLY", "reason": "; ".join(proj["violations"][:3])' in ta
    # alerts: expired = critical + Telegram; reminder 3 days before = warning + Telegram
    p = pea.plan(exp, NOW)
    assert p["active"] == {pea.KEY_EXPIRED} and p["raise"][0][0] == pea.KIND_EXPIRED and p["raise"][0][1] == "critical" and "CLOSE-ONLY" in p["raise"][0][3]
    p = pea.plan(soon, NOW)
    assert p["active"] == {pea.KEY_EXPIRING} and p["raise"][0][0] == pea.KIND_EXPIRING and "2 DAY(S)" in p["raise"][0][3]
    assert pea.plan({"policy_version": "6/3/3-v1"}, NOW) == {"active": set(), "raise": []}

    sent, raised = [], []

    async def notify(text):
        sent.append(text)

    async def raise_alert(db, kind, sev, msg, dedup_key=None, meta=None):
        raised.append((kind, dedup_key))
        return "id"

    class _PS:
        async def find_one(self, *_a, **_k):
            return exp

    db = type("DB", (), {})()
    db.platform_state = _PS()
    active, n = asyncio.new_event_loop().run_until_complete(pea.evaluate(db, NOW, raise_alert=raise_alert, notify=notify))
    assert active == {pea.KEY_EXPIRED} and n == 1 and raised == [(pea.KIND_EXPIRED, pea.KEY_EXPIRED)] and sent and "EXPIRED" in sent[0]
    import alerting
    assert "policy_expiring" in alerting.EVALUATOR_KINDS and "policy_expired" in alerting.EVALUATOR_KINDS
    # approval persists the lifetime + single-admin flag; readiness shows it
    assert "policy_expires_at=" in src and "single_admin_approval=" in src
    dr = _read("backend/demo_readiness.py")
    assert '_check("policy_expiry"' in dr and "approved single-admin" in dr
