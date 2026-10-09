"""main116 review + A18 fix list rev 1 (A19 items): M116-2, M116-3, A19-P1-03, A19-P1-04, A19-P2-01."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.fake_mongo import FakeDb
from tests.unit.test_update_preflight_shell import ROOT, _run, world  # noqa: F401

pytestmark = pytest.mark.unit
PS1 = os.path.join(ROOT, "backend", "static", "STOIC-Agent.ps1")


def _go(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- A19-P1-04 — file-based signed profile with expiry
def _profile_file(tmp_path, key, profile="dedicated", markers="", at=None, sig=None):
    import host_profile as hp
    at = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = sig if sig is not None else hmac.new(hp.derive_host_profile_key(key), hp.canonical_payload(profile, markers, at), hashlib.sha256).hexdigest()
    p = tmp_path / "host_profile.json"
    p.write_text(json.dumps({"profile": profile, "markers": markers, "detected_at": at, "sig": sig, "schema": 1}))
    return str(p)


def test_fresh_signed_file_verifies_and_file_wins_over_env(tmp_path):
    import host_profile as hp
    key = "anchor"
    env = {"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, "shared-web-host", "cPanel/WHM httpd"),
           "STOIC_HOST_PROFILE": "dedicated"}          # stale env snapshot must NOT win
    p = hp.host_profile(env)
    assert p["verified"] and p["shared_web_host"] and p["source"] == "file" and p["unverified_reason"] is None


def test_profile_older_than_24h_future_dated_or_unparseable_is_unverified(tmp_path):
    import host_profile as hp
    key = "anchor"
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=old)})
    assert p["verified"] is False and "h old" in p["unverified_reason"]
    fut = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=fut)})
    assert p["verified"] is False and "future" in p["unverified_reason"]
    ok4 = (datetime.now(timezone.utc) + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")   # within 5-min skew
    assert hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=ok4)})["verified"] is True
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    p = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": str(bad), "STOIC_HOST_PROFILE": "dedicated"})
    assert p["verified"] is False and p["source"] == "env"       # unparseable file → falls back to env, unsigned → unverified
    p = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at="not-a-date", sig="00")})
    assert p["verified"] is False


def test_refresh_script_writes_file_the_backend_verifies(world, tmp_path):   # noqa: F811
    proj = tmp_path / "proj"; (proj / "backend").mkdir(parents=True); (proj / "secrets").mkdir()
    (proj / "deploy").symlink_to(os.path.join(ROOT, "deploy"))
    (proj / "secrets" / "ledger_anchor_key").write_text("anchor-key-for-test\n")
    (proj / "backend" / ".env").write_text("APP_ENV=production\n")
    # deploy/ is a symlink → deploy/state resolves into the repo; point the writer at a temp copy instead
    state_dir = tmp_path / "hpstate"; state_dir.mkdir()
    r = _run(world, f"set -euo pipefail; cd {proj}; write_host_profile_file() {{ python3 -c 'import json,sys; json.dump({{\"profile\": sys.argv[1], \"markers\": sys.argv[2], \"detected_at\": sys.argv[3], \"sig\": sys.argv[4]}}, open(sys.argv[5], \"w\"))' \"$1\" \"$2\" \"$3\" \"$4\" {state_dir}/host_profile.json; }}; "
                     "markers=$(shared_web_host_markers); profile=dedicated; [ -n \"$markers\" ] && profile=shared-web-host; at=$(date -u +%Y-%m-%dT%H:%M:%SZ); "
                     "sig=$(host_profile_sig anchor-key-for-test \"$profile\" \"$markers\" \"$at\"); write_host_profile_file \"$profile\" \"$markers\" \"$at\" \"$sig\"; echo RC=$?", cwd=str(proj))
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    import host_profile as hp
    p = hp.host_profile({"LEDGER_ANCHOR_KEY": "anchor-key-for-test", "STOIC_HOST_PROFILE_FILE": str(state_dir / "host_profile.json")})
    assert p["verified"] is True and p["source"] == "file"
    assert os.path.exists(os.path.join(ROOT, "deploy", "host-profile-refresh.sh"))
    hp_sh = open(os.path.join(ROOT, "deploy", "host-prereqs.sh")).read()
    assert "stoic-host-profile.timer" in hp_sh and "OnUnitActiveSec=24h" in hp_sh
    compose = open(os.path.join(ROOT, "docker-compose.yml")).read()
    assert "./deploy/state:/app/state:ro" in compose and "STOIC_HOST_PROFILE_FILE: /app/state/host_profile.json" in compose


# ---------------------------------------------------------------- A19-P1-03 — degraded agent: CLOSE_ONLY for real money
def test_degraded_agent_close_only_for_real_reduced_for_demo():
    import trading_authority as ta
    base = {"_id": "a1", "mode": "live", "trading_enabled": False, "last_heartbeat": "2099-01-01T00:00:00+00:00",
            "vps_terminal": {"status": "agent_degraded", "detail": "restart ledger unsaved: disk full"}}
    real = _go(ta.infrastructure_domain(FakeDb(), {**base, "broker_environment": "LIVE"}))
    assert real["level"] == "CLOSE_ONLY" and real["code"] == "VPS_AGENT_DEGRADED"
    # audit #16 SEC-001: a self-DECLARED demo (no admin attestation) is still real money ⇒ CLOSE_ONLY
    declared = _go(ta.infrastructure_domain(FakeDb(), {**base, "broker_environment": "DEMO", "account_type": "demo",
                                                       "server": "ICMarkets-Demo"}))
    assert declared["level"] == "CLOSE_ONLY"
    from broker_env import attestation_identity
    att = {**base, "broker": "ICM", "account_number": 5012345, "server": "ICMarkets-Demo", "broker_server": "ICMarkets-Demo",
           "broker_environment": "DEMO", "account_type": "demo", "broker_account_id_reported": "5012345",
           "ea_identity": {"authoritative": True, "installation_id": "inst_1", "broker_server": "ICMarkets-Demo", "ea_version": "1.60"}}
    att["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin@x", "identity_hash": attestation_identity(att),
                                      "proof": {"verifier": "ea_heartbeat"}}
    demo = _go(ta.infrastructure_domain(FakeDb(), att))
    assert demo["level"] == "REDUCED"
    # broker-reported real trade mode voids the attestation ⇒ CLOSE_ONLY
    voided = _go(ta.infrastructure_domain(FakeDb(), {**att, "account_trade_mode": "real"}))
    assert voided["level"] == "CLOSE_ONLY"
    unattested = _go(ta.infrastructure_domain(FakeDb(), base))      # no attestation ⇒ treated as real
    assert unattested["level"] == "CLOSE_ONLY"
    import canonical_decision as cd
    dec = cd.from_snapshot({"domains": {"infrastructure": real}, "level": "CLOSE_ONLY"})
    assert dec["new_exposure_allowed"] is False and dec["close_allowed"] is True


# ---------------------------------------------------------------- M116-2 / A19-P1-03 agent recovery / A19-P2-01
def test_agent_reprobes_ledger_each_loop_and_recovers_through_stability_window():
    src = open(PS1, encoding="utf-8-sig").read()
    assert 'if ($script:Degraded) { $null = Save-StoicRestartLedger }' in src          # M116-2 — once per loop
    assert "$script:RecoveryWindowMinutes = 10" in src and "ledger read-back mismatch" in src
    assert "stability window elapsed - agent no longer degraded" in src
    assert src.count("$script:RecoverySince = $null") >= 2                               # cleared on failure + on recovery
    assert "Disable-StoicCrashDumps" in src and "LocalDumps" in src and "DumpCount" in src   # A19-P2-01
    wiz = open(os.path.join(ROOT, "frontend", "src", "components", "AddAccountWizard.jsx")).read()
    assert "wizard-real-manual-login" in wiz and "awaiting_login" in wiz


# ---------------------------------------------------------------- M116-3 — double-quoted snippets
def test_snippet_checker_parses_double_quoted_snippets(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib
    chk = importlib.import_module("check_deploy_python_snippets")
    sh = tmp_path / "x.sh"
    sh.write_text('a=$(python3 -c "import sys; print(\\"$X\\")")\nb=$(python3 -c \'print(1)\')\nc=$(python3 -c "print(f\\"{1!r:>{2}}\\")")\n')
    found = list(chk.snippets(str(sh)))
    assert len(found) == 3
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_deploy_python_snippets.py")], capture_output=True, text=True)
    assert r.returncode == 0 and re.search(r"checked (\d+) python3 -c snippet", r.stdout) and int(re.search(r"checked (\d+)", r.stdout).group(1)) >= 30
