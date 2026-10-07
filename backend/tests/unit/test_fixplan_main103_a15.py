"""main103 review + A15 audit fix list (Part 1 + small carry-overs):
A15-1 signed DEMO-only inventory policy (2 demo accounts on a production host → FULL; LIVE account → close-only;
DB row drift → close-only), A15-2 installer targets one terminal / keeps the device key, A15-3 closed-beta banner +
toggle step-up/audit/403 message, A15-4 broker in the fleet projection, N103-5 install.sh adopts the lock,
N103-6 previous EA record stays valid on the legacy payload."""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _local_signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    # local signing is forbidden in production (by design) — sign in preview and patch production_mode/environment_label
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "RELEASE_SIGNER_DEFERRED": "false",
            "ED25519_SIGNING_KEY_B64": priv, "RELEASE_PUBLIC_KEY_B64": pub, "STOIC_INSTALLATION_ID": "inst-demo-1"}


def _demo_policy(ids, demo_only=True, counts=(2, 2, 2)):
    now = datetime.now(timezone.utc)
    return {"schema": "stoic.policy-migration/v3", "installation_id": "inst-demo-1", "environment": "production",
            "previous_policy_version": "6/3/3-v1", "policy_version": "demo-2x2-v1",
            "accounts": counts[0], "enabled": counts[1], "bots": counts[2], "account_ids": list(ids), "demo_only": demo_only,
            "reason": "4-week MT5 demo on two attested demo accounts", "issuer": "ops@test", "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(), "nonce": "n" * 24}


def _signed(mig):
    import inventory_projection as ip
    import release_signing as rs
    mig = dict(mig)
    mig["signature_hex"] = rs.sign_hex(ip.migration_body(mig), purpose="policy-migration")
    return mig


# ── A15-1 ────────────────────────────────────────────────────────────────────────────────────────
def test_a15_1_validate_expectation_accepts_signed_demo_policy_and_refuses_unsigned_or_wrong_counts():
    import inventory_projection as ip
    from fastapi import HTTPException
    ids = ["a1", "a2"]
    with patch.dict(os.environ, _local_signer_env()), patch("inventory_projection.production_mode", lambda: True), \
            patch("inventory_projection.environment_label", lambda: "production"):
        mig = _signed(_demo_policy(ids))
        v = ip.validate_expectation({"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ids, "policy_migration": mig},
                                    require_policy=True, current_policy_version="6/3/3-v1")
        assert v["demo_only"] is True and v["policy_version"] == "demo-2x2-v1" and v["account_ids"] == ids
        # unsigned demo_only flag is refused
        with pytest.raises(HTTPException) as e:
            ip.validate_expectation({"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ids,
                                     "policy_migration": {**_demo_policy(ids)}}, require_policy=True, current_policy_version="6/3/3-v1")
        assert "SIGNED" in json.dumps(e.value.detail)
        # a 2/2/2 without any policy is still refused in production
        with pytest.raises(HTTPException):
            ip.validate_expectation({"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ids}, require_policy=True, current_policy_version="6/3/3-v1")
        # demo_only is part of the signed statement: flipping it after signing breaks the signature
        tampered = {**mig, "demo_only": False}
        assert ip.migration_problems({"accounts": 2, "enabled": 2, "bots": 2, "account_ids": ids, "policy_migration": tampered},
                                     current_policy_version="6/3/3-v1")


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        self._it = iter(self._docs); return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    def sort(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    def __init__(self, docs=None, one=None):
        self.docs = docs or []; self.one = one

    def find(self, q=None, proj=None):
        return _Cursor(self.docs)

    async def find_one(self, q=None, proj=None):
        return self.one

    async def count_documents(self, q=None):
        return 0

    def aggregate(self, *a, **k):
        return _Cursor([])


class _Db:
    def __init__(self, accounts, exp):
        self.accounts = _Coll(accounts)
        self.platform_state = _Coll(one=exp)
        self.bot_configs = _Coll([{"account_id": a["_id"], "enabled": True} for a in accounts])
        self.trades = _Coll([]); self.positions = _Coll([]); self.broker_deals = _Coll([])

    def __getattr__(self, name):
        return _Coll([])


def _acct(aid, env="DEMO"):
    return {"_id": aid, "label": aid, "mode": "demo", "trading_enabled": True, "broker": "ICM",
            "environment_attestation": {"environment": env, "status": "attested"}}


def test_a15_1_inventory_domain_two_demo_accounts_full_live_account_close_only():
    import trading_authority as ta
    exp = {"_id": "inventory_expectation", "accounts": 2, "enabled": 2, "bots": 2, "account_ids": ["a1", "a2"],
           "demo_only": True, "policy_version": "demo-2x2-v1"}
    accounts = [_acct("a1"), _acct("a2")]
    good = {"blocking": False, "violations": [], "counts": {}}

    async def proj_ok(db, scope=None):
        return good
    with patch("inventory_projection.production_mode", lambda: True), patch("inventory_projection.projection", proj_ok), \
            patch("broker_env.attested_environment", lambda a: (a.get("environment_attestation") or {}).get("environment", "LIVE")):
        d = _run(ta.inventory_domain(_Db(accounts, exp), accounts[0]))
        assert d["level"] == "FULL" and "demo-only" in d["reason"]
        live = {"_id": "l1", "mode": "live", "trading_enabled": True, "environment_attestation": {"environment": "LIVE"}}
        d = _run(ta.inventory_domain(_Db(accounts, exp), live))
        assert d["level"] == "CLOSE_ONLY" and "DEMO-only" in d["reason"]
    # projection: a listed account that is not attested DEMO is a blocking violation
    import inventory_projection as ip
    src = _read("backend/inventory_projection.py")
    assert 'exp.get("demo_only")' in src and "not attested DEMO" in src and 'counts["enabled"] != exp["enabled"]' in src


def test_a15_1_workflow_script_and_panel_wiring():
    wf = _read(".github/workflows/policy-migration.yml")
    assert "environment: policy-approval" in wf and "scripts/sign_policy_migration.py" in wf and "release/policy_migrations/" in wf
    assert "RELEASE_SIGNER_BUNDLE_TOKEN" not in wf                    # release key signs policies
    panel = _read("frontend/src/components/admin/InventoryGoLivePanel.jsx")
    assert 'data-testid="expectation-account-ids"' in panel and "policy_migration: policy.migration" in panel
    assert "/authority/inventory/policies" in panel and 'data-testid="expectation-policy"' in panel
    assert '@router.get("/inventory/policies")' in _read("backend/routes/authority_routes.py")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import sign_policy_migration as spm
    import inventory_projection as ip
    import release_signing as rs
    with patch.dict(os.environ, _local_signer_env()):
        ns = type("A", (), dict(installation_id="inst-demo-1", environment="production", previous="6/3/3-v1", version="demo-2x2-v1",
                                accounts=2, enabled=2, bots=2, account_ids="a2,a1", demo_only=True,
                                reason="4-week MT5 demo on two attested demo accounts", issuer="ops@test", expires_days=45))()
        mig = spm.sign(spm.build(ns))
        assert mig["account_ids"] == ["a1", "a2"] and mig["demo_only"] is True and mig["schema"] == ip.MIGRATION_SCHEMA
        assert rs.verify_hex(ip.migration_body(mig), mig["signature_hex"], purpose="policy-migration")
        assert not rs.verify_hex(ip.migration_body(mig), mig["signature_hex"], purpose="ea-release")   # domain-separated


# ── A15-2 ────────────────────────────────────────────────────────────────────────────────────────
def test_a15_2_installer_targets_one_terminal_keeps_device_key_and_finds_branded_editors():
    ps = _read("backend/static/STOIC-Installer.ps1")
    assert "[string]$TerminalPath" in ps and "[string]$TerminalId" in ps and "[switch]$RotateDeviceKey" in ps
    assert "Several terminals found" in ps and "Read-Host" in ps                      # interactive choice, never all terminals
    assert "terminal64.exe" in ps                                                      # portable-mode discovery
    assert 'Get-ChildItem $root -Filter "metaeditor64.exe"' in ps and "origin.txt" in ps  # broker-branded MetaEditor
    keep = ps[ps.index("function Get-StoicDevicePublicKey"):ps.index("function Get-StoicDevicePublicKey") + 600]
    assert "Test-Path (Get-StoicDeviceKeyPath)" in keep and "rotate the key on every pairing" not in keep
    assert ps.count("foreach ($t in $terminals)") >= 2 and '$terminals = @($found[[int]$pick - 1])' in ps


# ── A15-3 / N103-7 ───────────────────────────────────────────────────────────────────────────────
def test_a15_3_closed_beta_banner_and_toggle_hardening():
    assert os.path.exists(os.path.join(ROOT, "frontend", "src", "components", "ClosedBetaBanner.jsx"))
    for f in ("frontend/src/components/AppLayout.jsx", "frontend/src/pages/StatusPage.jsx",
              "frontend/src/pages/WelcomeTrailer.jsx"):
        assert "<ClosedBetaBanner" in _read(f), f
    reg = _read("frontend/src/pages/Register.jsx")
    assert "Join the discipline" not in reg and "Closed beta" in reg
    adm = _read("backend/routes/admin_routes.py")
    body = adm[adm.index('@router.post("/admin/settings/signups")'):adm.index('@router.get("/admin/settings/login-otp")')]
    assert 'require_step_up(db, user, request, "authority_relax")' in body and '"previous_closed": before["closed"]' in body
    import signup_lock as sl
    exc = sl.closed_http_exception("member", "Closed for the demo")
    assert exc.detail["message"] == "Closed for the demo"
    assert "refuse_if_closed" in _read("backend/routes/auth_routes.py") and "refuse_if_closed" in _read("backend/routes/affiliate_routes.py")


# ── A15-4 ────────────────────────────────────────────────────────────────────────────────────────
def test_a15_4_fleet_projection_carries_broker_so_attested_demo_reads_demo():
    src = _read("backend/demo_readiness.py")
    body = src[src.index("async def fleet"):src.index("async def fleet") + 2500]
    assert '"broker": 1' in body
    import broker_env as be
    ident_src = _read("backend/broker_env.py")
    fn = ident_src[ident_src.index("def attestation_identity"):ident_src.index("def attestation_identity") + 1200]
    for field in re.findall(r'account\.get\("([a-z_]+)"', fn):
        if field in ("broker", "creds_version", "account_trade_mode"):
            assert f'"{field}": 1' in body, field


# ── N103-5 / N103-6 ──────────────────────────────────────────────────────────────────────────────
def test_n103_5_install_adopts_release_lock_after_attestation():
    inst = _read("deploy/install.sh")
    assert inst.count("adopt_release_lock ||") == 2
    assert inst.index("verify_attestation || { echo \"ERROR: release attestation gate failed — refusing production install\"") \
        < inst.index("adopt_release_lock ||") < inst.index("verify_release_provenance || exit 1")


def test_n103_6_previous_ea_record_verifies_on_legacy_payload_but_current_never_does():
    import ea_capabilities as ec
    import release_signing as rs
    rec = {"version": "1.59", "mq5_sha256": "a" * 64, "ex5_sha256": "b" * 64, "metaeditor_version": None,
           "windows_build": None, "mt5_build": None, "source_commit": "c" * 40, "compiled_by": "github-actions"}
    with patch.dict(os.environ, {**_local_signer_env(), "APP_ENV": "preview"}):
        legacy_sig = rs.sign_hex(ec._canonical_payload(rec, legacy=True), purpose="ea-release")
        prev = {**rec, "signature": {"key_id": rs.key_id(), "sig_hex": legacy_sig}}
        assert not rs.verify_hex(ec._canonical_payload(prev), legacy_sig, purpose="ea-release")          # new payload: no
        assert rs.verify_hex(ec._canonical_payload(prev, legacy=True), legacy_sig, purpose="ea-release")  # legacy: yes
    src = _read("backend/ea_capabilities.py")
    body = src[src.index('prev = rec.get("previous")'):src.index('result = {**rec, "previous": prev}')]
    assert "legacy=True" in body
    cur = src[src.index("def _signed_release_record"):src.index('prev = rec.get("previous")')]
    assert "legacy=True" not in cur                                                                   # current record: strict


# ── audit #5 (SEC-001 / SEC-002 / P3) ─────────────────────────────────────────────────────────────
def test_sec001_policy_workflow_never_interpolates_inputs_into_shell():
    wf = _read(".github/workflows/policy-migration.yml")
    run_blocks = re.findall(r"run: \|\n((?:\s{10,}.*\n)+)", wf)
    assert run_blocks
    for blk in run_blocks:
        assert "${{" not in blk, blk          # inputs/actor reach bash only via env vars
    assert 'IN_REASON: ${{ inputs.reason }}' in wf and '--reason "$IN_REASON"' in wf
    assert '[[ "$IN_VERSION" =~' in wf and '[[ "$IN_ACCOUNT_IDS" =~' in wf
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import sign_policy_migration as spm
    bad = type("A", (), dict(installation_id="i", environment="production", previous="6/3/3-v1", version="../evil",
                             accounts=2, enabled=2, bots=2, account_ids="a,b", demo_only=True,
                             reason="4-week MT5 demo on two attested demo accounts", issuer="x", expires_days=1))()
    with pytest.raises(SystemExit):
        spm.build(bad)


def test_sec002_approval_persists_demo_only_so_the_guard_is_live():
    import inventory_projection as ip

    class _PS:
        def __init__(self, pending):
            self.pending = pending; self.saved = None

        async def find_one(self, q, *a, **k):
            return self.pending if q.get("_id") == "inventory_expectation_pending" else None

        async def replace_one(self, q, doc, upsert=False):
            self.saved = doc

        async def delete_one(self, q):
            return None

        async def update_one(self, *a, **k):
            return None

        async def find_one_and_update(self, *a, **k):
            return {"_id": "nonce", "version": 1}

        def __getattr__(self, name):            # any other collection op (insert/delete/update/count…) is a no-op
            async def _noop(*a, **k):
                return None
            return _noop

        def find(self, *a, **k):
            return _Cursor([])

    class _Db:
        def __init__(self, pending):
            self.platform_state = _PS(pending)

        def __getattr__(self, name):
            return _PS(None)

    ids = ["a1", "a2"]
    with patch.dict(os.environ, _local_signer_env()), patch("inventory_projection.production_mode", lambda: True), \
            patch("inventory_projection.environment_label", lambda: "production"):
        mig = _signed(_demo_policy(ids))
        pending = {"_id": "inventory_expectation_pending", "accounts": 2, "enabled": 2, "bots": 2, "account_ids": ids,
                   "demo_only": True, "policy_migration": mig, "proposed_by": "a@x", "proposed_at": "t"}
        db = _Db(pending)

        async def cpv(_db):
            return "6/3/3-v1"

        async def noop(*a, **k):
            return None
        with patch("inventory_projection.current_policy_version", cpv), patch("inventory_projection.consume_migration_nonce", noop), \
                patch("audit_chain.append_chained", noop), patch("inventory_projection.approval_mode", lambda: "single_admin"):
            _run(ip.approve_expectation(db, "b@x"))
    assert db.platform_state.saved["demo_only"] is True and db.platform_state.saved["policy_version"] == "demo-2x2-v1"


def test_p3_adopt_release_lock_validates_every_asset_before_writing():
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^adopt_release_lock\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert fn.index('adopt+=("${f}")') < fn.index('cp "${dest}/rc_lock.json" release/rc_lock.json') < fn.index('for f in "${adopt[@]}"')
