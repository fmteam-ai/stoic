"""main115 review + A18 audit fix list — M115-1/2/3, P1-03, P1-04, P2-01, P2-02, P0-01 (offline --check), P2-03."""
import asyncio
import hashlib
import hmac
import os
import re
import subprocess
import sys

import pytest

from tests.unit.fake_mongo import FakeDb
from tests.unit.test_update_preflight_shell import ROOT, _run, world  # noqa: F401

pytestmark = pytest.mark.unit
PS1 = os.path.join(ROOT, "backend", "static", "STOIC-Agent.ps1")


def _go(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- M115-2 / P1-03 — execution gate
def test_deploy_jam_marker_is_close_only_in_authority_and_canonical_decision():
    import trading_authority as ta
    import canonical_decision as cd
    db = FakeDb()
    assert _go(ta.deploy_posture_domain(db))["level"] == "FULL"
    _go(db.platform_state.insert_one({"_id": "deploy_jam", "trading_paused": True, "at": "2026-10-08T21:00:00Z", "reason": "ebusy"}))
    d = _go(ta.deploy_posture_domain(db))
    assert d["level"] == "CLOSE_ONLY" and d["code"] == "TRADING_PAUSED_DEPLOY_JAM"
    snap = {"domains": {"deploy_posture": d, "platform": {"level": "FULL", "reason": "ok"}}, "level": "CLOSE_ONLY"}
    dec = cd.from_snapshot(snap)
    assert dec["state"] == "CLOSE_ONLY" and dec["new_exposure_allowed"] is False and dec["close_allowed"] is True
    assert "TRADING_PAUSED_DEPLOY_JAM" in dec["reason_codes"]
    assert "deploy_posture" in ta._DOMAINS and ta.DOMAIN_SCOPE["deploy_posture"] == "platform_global"


def test_deploy_jam_unreadable_fails_closed():
    import trading_authority as ta

    class Boom:
        async def find_one(self, *_a, **_k):
            raise RuntimeError("mongo down")

    class DB:
        platform_state = Boom()

    d = _go(ta.deploy_posture_domain(DB()))
    assert d["level"] == "CLOSE_ONLY" and d["code"] == "DEPLOY_JAM_UNKNOWN"
    ops = open(os.path.join(ROOT, "backend", "routes", "ops_routes.py")).read()
    assert '"unknown": True' in ops and '"ok": not is_production(), "unknown": True' in ops
    card = open(os.path.join(ROOT, "frontend", "src", "components", "ReadinessCard.jsx")).read()
    assert 'checks[k]?.unknown ? "UNKNOWN"' in card


# ---------------------------------------------------------------- M115-1 / M115-3 — host profile signing
def test_record_host_profile_runs_under_strict_shell_and_backend_verifies_it(world, tmp_path):   # noqa: F811
    proj = tmp_path / "proj"; (proj / "backend").mkdir(parents=True); (proj / "secrets").mkdir()
    (proj / "deploy").symlink_to(os.path.join(ROOT, "deploy"))
    (proj / "backend" / ".env").write_text("APP_ENV=production\n")
    (proj / "secrets" / "ledger_anchor_key").write_text("anchor-key-for-test\n")
    r = _run(world, "set -euo pipefail; record_host_profile; echo RC=$?", cwd=str(proj))
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    env = dict(l.split("=", 1) for l in (proj / "backend" / ".env").read_text().splitlines() if "=" in l)
    assert env["STOIC_HOST_PROFILE"] in ("dedicated", "shared-web-host") and len(env["STOIC_HOST_PROFILE_SIG"]) == 64
    import host_profile as hp
    hp_env = {k: v for k, v in env.items()}; hp_env["LEDGER_ANCHOR_KEY"] = "anchor-key-for-test"
    p = hp.host_profile(hp_env)
    assert p["verified"] is True and p["detected_at"]
    # M115-3 — derived key, never the raw anchor key
    raw = hmac.new(b"anchor-key-for-test", hp.canonical_payload(env['STOIC_HOST_PROFILE'], env['STOIC_HOST_MARKERS'].strip(chr(34)), env['STOIC_HOST_DETECTED_AT']), hashlib.sha256).hexdigest()
    assert raw != env["STOIC_HOST_PROFILE_SIG"]
    # audit #15 P3 — field shifting via "|" is impossible with length-prefixed canonicalisation
    assert hp.canonical_payload("a|b", "c", "d") != hp.canonical_payload("a", "b|c", "d")
    assert hp.derive_host_profile_key("k") == hmac.new(b"k", b"stoic-host-profile-v1", hashlib.sha256).digest()


def test_record_host_profile_without_key_is_non_fatal(world, tmp_path):   # noqa: F811
    proj = tmp_path / "proj2"; (proj / "backend").mkdir(parents=True)
    (proj / "deploy").symlink_to(os.path.join(ROOT, "deploy"))
    (proj / "backend" / ".env").write_text("APP_ENV=production\n")
    r = _run(world, "set -euo pipefail; record_host_profile; echo RC=$?", cwd=str(proj))
    assert "RC=0" in r.stdout and "UNSIGNED" in r.stdout


def test_deploy_python_snippets_compile_under_py36():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_deploy_python_snippets.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    assert "f-string" not in r.stdout
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    assert "check_deploy_python_snippets.py" in ci


# ---------------------------------------------------------------- P1-04 / P2-01 — agent
def test_ps1_every_unmanaged_secret_pointer_is_freed_in_finally():
    src = open(PS1, encoding="utf-8-sig").read()
    allocs = len(re.findall(r"SecureStringToGlobalAllocUnicode", src))
    frees = len(re.findall(r"ZeroFreeGlobalAllocUnicode", src))
    assert allocs == 2 and frees == allocs
    for m in re.finditer(r"SecureStringToGlobalAllocUnicode", src):
        window = src[m.start():m.start() + 600]
        assert "finally" in window and "ZeroFreeGlobalAllocUnicode" in window
    assert "$plain = " not in src                                   # plaintext never held in a named variable
    assert src.count("Remove-Item $firstIni -Force") >= 1 and "Remove-StoicPasswordLeftovers" in src


def test_ps1_degraded_agent_refuses_automatic_restarts():
    src = open(PS1, encoding="utf-8-sig").read()
    assert "$script:Degraded = $null" in src and "Test-StoicRestartAllowed" in src
    assert src.count("if (-not (Test-StoicRestartAllowed $cfg $base)) { return }") == 2
    assert 'status = "agent_degraded"' in src and "degraded = $(if ($script:Degraded)" in src
    assert "$null = Save-StoicRestartLedger" in src                  # probe at start
    import vps_terminals
    assert "agent_degraded" in vps_terminals.TERMINAL_STATUSES
    import alerting
    assert "vps_agent_degraded" in alerting.ALERT_KINDS if hasattr(alerting, "ALERT_KINDS") else "vps_agent_degraded" in open(os.path.join(ROOT, "backend", "alerting.py")).read()


def test_infrastructure_domain_flags_degraded_agent():
    import trading_authority as ta
    acc = {"_id": "a1", "mode": "live", "trading_enabled": False, "last_heartbeat": "2099-01-01T00:00:00+00:00",
           "vps_terminal": {"status": "agent_degraded", "detail": "restart ledger unsaved: disk full"}}
    d = _go(ta.infrastructure_domain(FakeDb(), acc))
    assert d["level"] == "CLOSE_ONLY" and d["code"] == "VPS_AGENT_DEGRADED"      # A19-P1-03: unattested ⇒ real ⇒ close-only
    assert _go(ta.infrastructure_domain(FakeDb(), {**acc, "broker_environment": "DEMO"}))["level"] == "REDUCED"


# ---------------------------------------------------------------- P2-02 — wizard policy states
def test_wizard_policy_loading_known_unknown():
    wiz = open(os.path.join(ROOT, "frontend", "src", "components", "AddAccountWizard.jsx")).read()
    assert 'setPolicy({ state: "loading", demoOnly: false })' in wiz and "setF(freshForm())" in wiz
    assert 'setPolicy({ state: "unknown", demoOnly: false })' in wiz
    assert 'disabled={id === "real" && policyUnknown}' in wiz and "wizard-policy-unavailable" in wiz
    assert "(demoPolicy || policyUnknown)" in wiz


# ---------------------------------------------------------------- P0-01 / P2-03 — offline --check, public release id
def test_verify_ea_release_check_is_offline_and_fingerprint_bound(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib
    ver = importlib.import_module("verify_ea_release")
    for k in ("RELEASE_PUBLIC_KEY_B64", "RELEASE_SIGNER_KEY_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RELEASE_SIGNER", "external"); monkeypatch.setenv("RELEASE_SIGNER_URL", "https://signer:9443")

    class A:
        public_key = None
    assert ver.resolve_public_key(A()) is None
    with pytest.raises(SystemExit) as ei:
        ver.check(A())
    assert "--public-key" in str(ei.value) and "release/release_key.fingerprint" in str(ei.value)
    A.public_key = __import__("base64").b64encode(b"A" * 32).decode()      # valid 32-byte shape, wrong key
    with pytest.raises(SystemExit) as ei:
        ver.resolve_public_key(A())
    assert "does not match the committed" in str(ei.value)
    A.public_key = "1NgD7Rq2/8Fa31kwU2N18krBt3d5zPkwmg60MUW0Gkc="
    assert ver.resolve_public_key(A()) == A.public_key and os.environ["RELEASE_PUBLIC_KEY_B64"] == A.public_key


def test_public_status_release_identity():
    import release_truth as rt
    out = rt.public_release_identity()
    assert set(out) >= {"short_commit", "release_id", "signed", "note"}
    assert out["signed"] is False or out["release_id"]
    page = open(os.path.join(ROOT, "frontend", "src", "pages", "StatusPage.jsx")).read()
    assert "status-release-identity" in page


def test_audit15_no_inline_untrusted_context_in_workflow_run_steps():
    """SEC-001 — github.event.* / inputs.* must never be expanded inline inside a `run:` block."""
    import glob
    offenders = []
    for wf in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        in_run = False
        for i, line in enumerate(open(wf), 1):
            if re.match(r"^\s*run:\s*\|?\s*$", line) or re.match(r"^\s*run:\s*\S", line):
                in_run = True
                if re.match(r"^\s*run:\s*\S", line) and not line.rstrip().endswith("|"):
                    in_run = False
                    if "${{ github.event" in line or "${{ inputs" in line:
                        offenders.append(f"{os.path.basename(wf)}:{i}")
                continue
            if in_run and re.match(r"^\s*(-\s+name:|-\s+uses:|env:|with:|if:|id:|shell:|working-directory:)", line):
                in_run = False
            if in_run and ("${{ github.event" in line or "${{ inputs" in line):
                offenders.append(f"{os.path.basename(wf)}:{i}")
    assert offenders == [], offenders


def test_audit15_release_key_pin_refuses_tofu(world, tmp_path):   # noqa: F811
    from tests.unit.test_update_preflight_shell import _stub
    fp_file = tmp_path / "empty.fingerprint"; fp_file.write_text("# none\n")
    _stub(world["tmp"] / "bin", "curl", '#!/usr/bin/env bash\necho \'{"key_id":"stoic-release-ed25519-v1","public_key_b64":"1NgD7Rq2/8Fa31kwU2N18krBt3d5zPkwmg60MUW0Gkc="}\'\n')
    proj = tmp_path / "tofu"; (proj / "backend").mkdir(parents=True); (proj / "deploy").symlink_to(os.path.join(ROOT, "deploy"))
    (proj / "backend" / ".env").write_text("RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v1\n")
    r = _run(world, "ensure_release_public_key_pin; echo RC=$?", {"RELEASE_KEY_FINGERPRINT_FILE": str(fp_file)}, cwd=str(proj))
    assert "NOT pinning" in r.stdout and "RELEASE_PUBLIC_KEY_B64=" not in (proj / "backend" / ".env").read_text()
