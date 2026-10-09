"""main117 review + audit A20: P1-03 (timer margin), M117-1 (timer is a prerequisite), P1-04 (writer fails loudly),
P2-01 (narrow crash-dump policy), M117-2 (byte-exact ledger read-back, pwsh test), P2-02/M117-3 (auth deadline),
P0-02 test #1 (reconciliation counts from the SIGNED policy)."""
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.fake_mongo import FakeDb
from tests.unit.test_update_preflight_shell import ROOT, _run, _stub, world  # noqa: F401

pytestmark = pytest.mark.unit
HP_SH = os.path.join(ROOT, "deploy", "host-prereqs.sh")


def _go(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- A20-P1-03 — timer interval vs max age
def test_timer_interval_leaves_a_retry_budget_under_the_max_age():
    import host_profile as hp
    src = open(HP_SH).read()
    m = re.search(r"OnUnitActiveSec=(\d+)h", src)
    assert m and int(m.group(1)) == hp.REFRESH_INTERVAL_HOURS == 6
    assert "RandomizedDelaySec=10min" in src and "Persistent=true" not in src.split("(d) signed host-profile")[1]
    # at least three missed runs (plus jitter + 1-min accuracy) before the profile reads as expired
    assert hp.REFRESH_INTERVAL_HOURS * 3 + 1 <= hp.MAX_AGE_HOURS == 24
    assert hp.WARN_AGE_HOURS == 12 and hp.REFRESH_INTERVAL_HOURS < hp.WARN_AGE_HOURS < hp.MAX_AGE_HOURS


def test_profile_refresh_overdue_warns_then_unverified_alerts(tmp_path):
    import host_profile as hp
    import host_profile_alerts as hpa
    from tests.unit.test_main116_a19 import _profile_file
    key = "anchor-key-for-test"
    fresh = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=_ts(2))})
    assert fresh["verified"] and not fresh["refresh_overdue"] and fresh["refresh_warning"] is None
    assert hpa.plan(fresh)["raise"] == []
    overdue = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=_ts(13))})
    assert overdue["verified"] is True and overdue["refresh_overdue"] is True and "refresh overdue" in overdue["refresh_warning"]
    p = hpa.plan(overdue)
    assert [r[0] for r in p["raise"]] == [hpa.KIND_OVERDUE] and p["raise"][0][1] == "warning" and hpa.KEY_OVERDUE in p["active"]
    expired = hp.host_profile({"LEDGER_ANCHOR_KEY": key, "STOIC_HOST_PROFILE_FILE": _profile_file(tmp_path, key, at=_ts(25))})
    assert expired["verified"] is False and "25.0 h old" in expired["unverified_reason"]
    p = hpa.plan(expired)
    assert [r[0] for r in p["raise"]] == [hpa.KIND_UNVERIFIED] and p["raise"][0][1] == "critical"
    # never recorded at all (dev box) → no alert noise
    assert hpa.plan(hp.host_profile({}))["raise"] == []
    import alerting
    assert hpa.KIND_OVERDUE in alerting.EVALUATOR_KINDS and hpa.KIND_UNVERIFIED in alerting.EVALUATOR_KINDS
    assert "host_profile_alerts" in open(os.path.join(ROOT, "backend", "alerting.py")).read()


def test_alert_evaluate_raises_through_raise_alert(tmp_path, monkeypatch):
    import host_profile_alerts as hpa
    from tests.unit.test_main116_a19 import _profile_file
    key = "anchor-key-for-test"
    monkeypatch.setenv("LEDGER_ANCHOR_KEY", key)
    monkeypatch.setenv("STOIC_HOST_PROFILE_FILE", _profile_file(tmp_path, key, at=_ts(14)))
    seen = []

    async def ra(db, kind, severity, message, dedup_key=None, meta=None):
        seen.append((kind, severity, dedup_key))
        return "id1"
    active, raised = _go(hpa.evaluate(FakeDb(), raise_alert=ra))
    assert raised == 1 and seen == [(hpa.KIND_OVERDUE, "warning", hpa.KEY_OVERDUE)] and active == {hpa.KEY_OVERDUE}


# ---------------------------------------------------------------- M117-1 — the timer is a host prerequisite
def _timer_world(world, active: bool):   # noqa: F811
    state = world["state"]
    (state / "timer_active").write_text("1" if active else "0")
    _stub(world["tmp"] / "bin", "systemctl",
          '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_STATE/calls.log"\n'
          'case "$1" in\n'
          '  is-active) [ "$(cat "$STUB_STATE/timer_active")" = 1 ] ;;\n'
          '  enable) echo 1 > "$STUB_STATE/timer_active" ;;\n'
          '  start) echo slave > "$STUB_STATE/prop" ;;\n'
          'esac\n')
    os.chmod(world["tmp"] / "bin" / "systemctl", 0o755)
    return {"STOIC_SKIP_HOST_TIMER": "0", "STOIC_SYSTEMD_DIR": str(world["tmp"] / "systemd")}


def test_host_prereqs_missing_reports_inactive_timer(world):   # noqa: F811
    env = _timer_world(world, active=False)
    # (a)+(b) satisfied: mdm=1 persisted, propagation slave
    (world["state"] / "mdm").write_text("1\n"); (world["state"] / "prop").write_text("slave")
    r = _run(world, "host_prereqs_missing; echo RC=$?", env)
    assert "RC=0" in r.stdout and "stoic-host-profile.timer not active" in r.stdout, r.stdout + r.stderr
    (world["state"] / "timer_active").write_text("1")
    r = _run(world, "host_prereqs_missing; echo RC=$?", env)
    assert "RC=1" in r.stdout and "timer" not in r.stdout


def test_host_prereqs_check_and_apply_install_the_timer(world, tmp_path):   # noqa: F811
    env = _timer_world(world, active=False)
    (tmp_path / "systemd").mkdir()
    (world["state"] / "mdm").write_text("1\n"); (world["state"] / "prop").write_text("slave")
    (world["etc"] / "99-stoic-docker.conf").write_text("fs.may_detach_mounts = 1\n")
    (world["etc"] / "docker.service.d").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "deploy", "docker-root-slave.sh"), world["etc"] / "stoic-docker-root-slave")
    (world["etc"] / "docker.service.d" / "10-stoic-private-root.conf").write_text("x")
    r = _run(world, "bash deploy/host-prereqs.sh --check; echo RC=$?", env)
    assert "RC=2" in r.stdout and "stoic-host-profile.timer missing/outdated" in r.stdout, r.stdout + r.stderr
    r = _run(world, "bash deploy/host-prereqs.sh --yes; echo RC=$?", env)
    assert "RC=0" in r.stdout and "refresh timer installed + active" in r.stdout, r.stdout + r.stderr
    tmr = (tmp_path / "systemd" / "stoic-host-profile.timer").read_text()
    assert "OnUnitActiveSec=6h" in tmr and "RandomizedDelaySec=10min" in tmr and "Persistent" not in tmr
    assert "systemctl enable --now stoic-host-profile.timer" in (world["state"] / "calls.log").read_text()
    r = _run(world, "bash deploy/host-prereqs.sh --check; echo RC=$?", env)
    assert "RC=0" in r.stdout and "installed and active" in r.stdout, r.stdout + r.stderr
    doctor = open(os.path.join(ROOT, "deploy", "doctor.sh")).read()
    assert "stoic-host-profile.timer not active" in doctor and "refresh overdue" in doctor


# ---------------------------------------------------------------- A20-P1-04 — writer fails loudly, keeps the previous file
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_write_host_profile_file_fails_on_unwritable_dir_and_keeps_previous(world, tmp_path):   # noqa: F811
    d = tmp_path / "state"; d.mkdir()
    (d / "host_profile.json").write_text('{"profile":"dedicated"}')
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = _run(world, "write_host_profile_file dedicated '' 2026-01-01T00:00:00Z abc; echo RC=$?", {"STOIC_HOST_PROFILE_DIR": str(d)})
    finally:
        d.chmod(0o755)
    assert "RC=1" in r.stdout and "host profile: cannot write" in r.stderr, r.stdout + r.stderr
    assert (d / "host_profile.json").read_text() == '{"profile":"dedicated"}' and not (d / "host_profile.json.tmp").exists()


def test_write_host_profile_file_mkdir_failure_is_nonzero(world, tmp_path):   # noqa: F811
    blocker = tmp_path / "file"; blocker.write_text("x")          # a FILE where the state dir should be → mkdir fails for everyone incl. root
    r = _run(world, "write_host_profile_file dedicated '' 2026-01-01T00:00:00Z abc; echo RC=$?", {"STOIC_HOST_PROFILE_DIR": str(blocker / "state")})
    assert "RC=1" in r.stdout and "cannot create" in r.stderr, r.stdout + r.stderr
    src = open(os.path.join(ROOT, "deploy", "preflight.sh")).read()
    assert "|| return 0" not in src.split("write_host_profile_file() {")[1].split("record_host_profile()")[0]
    refresh = open(os.path.join(ROOT, "deploy", "host-profile-refresh.sh")).read()
    assert "exit 1" in refresh and "previous file kept" in refresh


def test_write_host_profile_file_success_path(world, tmp_path):   # noqa: F811
    d = tmp_path / "state"
    r = _run(world, "write_host_profile_file shared-web-host cpanel 2026-01-01T00:00:00Z abc; echo RC=$?", {"STOIC_HOST_PROFILE_DIR": str(d)})
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    doc = json.loads((d / "host_profile.json").read_text())
    assert doc == {"profile": "shared-web-host", "markers": "cpanel", "detected_at": "2026-01-01T00:00:00Z", "sig": "abc", "schema": 1}
    assert not (d / "host_profile.json.tmp").exists()


# ---------------------------------------------------------------- M117-2 — pwsh behavioural test (5.1-compatible script)
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_agent_recovery_script_passes_under_pwsh():
    r = subprocess.run(["pwsh", "-NoProfile", "-File", os.path.join(ROOT, "scripts", "test_agent_recovery.ps1")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "agent recovery tests passed" in r.stdout, r.stdout + r.stderr


def test_ci_runs_agent_recovery_under_windows_powershell_51():
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    i = ci.index("scripts\\test_agent_recovery.ps1")
    assert "shell: powershell" in ci[i - 600:i] and "PSVersion.Major -ne 5" in ci[i - 600:i]
    ps1 = open(os.path.join(ROOT, "backend", "static", "STOIC-Agent.ps1"), encoding="utf-8-sig").read()
    assert '$script:AgentVersion = "1.4"' in ps1


# ---------------------------------------------------------------- A20-P2-02 / M117-3 — auth deadline + one-request anonymous redirect
def test_frontend_auth_check_has_deadline_and_anonymous_skip():
    ctx = open(os.path.join(ROOT, "frontend", "src", "context", "AuthContext.jsx")).read()
    assert "AUTH_CHECK_TIMEOUT_MS = 8000" in ctx and "Promise.race" in ctx and "AUTH_TIMEOUT" in ctx
    api = open(os.path.join(ROOT, "frontend", "src", "lib", "api.js")).read()
    assert 'readCookie("stoic_session")' in api and "anonymousMe" in api and "!anonymousMe" in api
    auth = open(os.path.join(ROOT, "backend", "auth.py")).read()
    assert 'key="stoic_session", value="1", httponly=False' in auth and 'delete_cookie("stoic_session"' in auth


# ---------------------------------------------------------------- A20-P0-02 test #1 — counts from the SIGNED policy
def test_reconcile_expect_approved_takes_counts_and_ids_from_the_signed_policy():
    sys.path.insert(0, os.path.join(ROOT, "backend", "ops"))
    import production_reconcile as pr
    db = FakeDb()
    assert _go(pr.approved_policy_expectation(db))["problems"]                     # nothing approved ⇒ REFUSED
    _go(db.platform_state.insert_one({"_id": "inventory_expectation", "accounts": 2, "enabled": 2, "bots": 2,
                                      "account_ids": ["b", "a"], "policy_version": "demo-2x2-v1", "demo_only": True,
                                      "approved_by": "admin@x", "policy_expires_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()}))
    p = _go(pr.approved_policy_expectation(db))
    assert p["problems"] == [] and (p["accounts"], p["enabled"], p["bots"]) == (2, 2, 2) and p["account_ids"] == ["a", "b"] and p["demo_only"]
    _go(db.platform_state.update_many({"_id": "inventory_expectation"}, {"$set": {"policy_expires_at": "2000-01-01T00:00:00+00:00"}}))
    assert any("expired" in x for x in _go(pr.approved_policy_expectation(db))["problems"])
    _go(db.platform_state.update_many({"_id": "inventory_expectation"}, {"$set": {"policy_expires_at": None}, "$unset": {"approved_by": ""}}))
    assert any("not approved" in x for x in _go(pr.approved_policy_expectation(db))["problems"])

    class A:
        expect, scope_user, strict = "approved", "u1", True
    assert pr.preflight(A, {"GIT_SHA": "abc", "LEDGER_ANCHOR_KEY": "k", "APP_ENV": "production"}) == []
    A.expect = "6/3/3-nope"
    assert any("malformed" in x for x in pr.preflight(A, {"GIT_SHA": "abc", "LEDGER_ANCHOR_KEY": "k"}))
    src = open(os.path.join(ROOT, "backend", "ops", "production_reconcile.py")).read()
    assert "enabled_environments_all_demo" in src and 'EXPECT_APPROVED = "approved"' in src
    # audit #17 P3 — the demo_only check classifies by the ATTESTED environment, and an id-less policy is REFUSED
    assert 'for e in totals["attested_environments_enabled"]' in src and '"attested_environment": attested_environment(acc)' in src
    assert "approved policy names no account ids" in src


def test_update_sh_defaults_to_the_signed_policy_not_633():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    seg = upd[upd.index("APP_ENV_VAL=$(app_env)"):upd.index("pruning dangling images")]
    assert 'APPROVED_POLICY="${APPROVED_POLICY:-approved}"' in seg and ':-6/3/3}' not in seg
    assert "|approved)$'" in seg and 'if [ "${RECONCILE_EXPECT}" != approved ]' in seg
    for f in ("docs/RELEASE_ACCEPTANCE_CHECKLIST.md", "docs/PRODUCTION_DEPLOY_CHECKLIST.md"):
        assert "--expect approved" in open(os.path.join(ROOT, f)).read() or "RECONCILE_EXPECT=approved" in open(os.path.join(ROOT, f)).read()
