"""main114 review follow-ups (M114-1 … M114-7) — shell branches with stubbed docker/findmnt/mount/umount
and the signed host profile / deploy-jam readiness pieces."""
import hashlib
import hmac
import os
import subprocess
import textwrap

import pytest

from tests.unit.test_update_preflight_shell import ROOT, _calls, _run, _stub, world  # noqa: F401 — fixture reuse

pytestmark = pytest.mark.unit


def _slave(tmp_path, source, prop="shared", is_mountpoint=True):
    bin_dir = tmp_path / "sbin"; bin_dir.mkdir()
    state = tmp_path / "sstate"; state.mkdir()
    (state / "prop").write_text(prop)
    (state / "mp").write_text("1" if is_mountpoint else "0")
    _stub(bin_dir, "findmnt", '#!/usr/bin/env bash\ncase "$*" in *PROPAGATION*) cat "' + str(state) + '/prop" ;; *SOURCE*) echo "' + source + '" ;; esac\n')
    _stub(bin_dir, "mountpoint", f'#!/usr/bin/env bash\n[ "$(cat {state}/mp)" = 1 ]\n')
    _stub(bin_dir, "umount", f'#!/usr/bin/env bash\necho "umount $*" >> {state}/calls; echo 0 > {state}/mp\n')
    _stub(bin_dir, "mount", f'#!/usr/bin/env bash\necho "mount $*" >> {state}/calls; case "$*" in *--bind*) echo 1 > {state}/mp ;; *make-slave*) echo slave > {state}/prop ;; esac\n')
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    r = subprocess.run(["sh", os.path.join(ROOT, "deploy", "docker-root-slave.sh"), "/var/lib/docker"], env=env, capture_output=True, text=True, timeout=30)
    calls = (state / "calls").read_text() if (state / "calls").exists() else ""
    return r, calls, (state / "prop").read_text().strip()


def test_m114_1_real_partition_is_never_unmounted(tmp_path):
    r, calls, prop = _slave(tmp_path, source="/dev/sdb1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "umount" not in calls                                    # the data disk stays mounted
    assert "mount --bind /var/lib/docker /var/lib/docker" in calls and "mount --make-slave /var/lib/docker" in calls
    assert prop == "slave" and "real filesystem" in r.stdout


def test_m114_1_self_bind_is_detached_then_rebound(tmp_path):
    r, calls, prop = _slave(tmp_path, source="/dev/sda3[/var/lib/docker]")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "umount -l /var/lib/docker" in calls and "mount --bind" in calls and "make-slave" in calls and prop == "slave"


def test_m114_1_already_slave_is_a_noop(tmp_path):
    r, calls, _ = _slave(tmp_path, source="/dev/sdb1", prop="shared,slave")
    assert r.returncode == 0 and calls == ""


def test_m114_4_preflight_without_yes_refuses_dockerd_restart_non_interactive(world):   # noqa: F811
    r = _run(world, "preflight_host; echo RC=$?", {"PREFLIGHT_YES": "0"})
    assert "RC=1" in r.stdout, r.stdout + r.stderr
    assert "systemctl stop docker" not in _calls(world)                       # no restart without --yes / a terminal
    assert "--yes" in r.stdout
    assert (world["state"] / "mdm").read_text().strip() == "1"              # the harmless sysctl part IS applied


def test_m114_6_second_attempt_non_ebusy_failure_returns_1_not_reboot(world):   # noqa: F811
    docker = world["tmp"] / "bin" / "docker"
    src = docker.read_text().replace(
        'if [ "$n" -le "$(cat "$S/up_fail_times" 2>/dev/null || echo 0)" ]; then',
        'if [ "$n" = 2 ]; then echo "Error: image stoic-backend not found" >&2; exit 1; fi\n      if [ "$n" -le "$(cat "$S/up_fail_times" 2>/dev/null || echo 0)" ]; then')
    docker.write_text(src)
    (world["state"] / "up_fail_times").write_text("1")
    r = _run(world, "compose_up_guarded; echo RC=$?")
    assert "RC=1" in r.stdout and "reboot required" not in r.stdout and "other than overlay EBUSY" in r.stdout


def test_m114_3_fetched_key_must_match_committed_fingerprint(world, tmp_path):   # noqa: F811
    fp_file = tmp_path / "release_key.fingerprint"
    fp_file.write_text("stoic-release-ed25519-v1 SHA256:" + "0" * 64 + "\n")
    _stub(world["tmp"] / "bin", "curl", '#!/usr/bin/env bash\necho \'{"key_id":"stoic-release-ed25519-v1","public_key_b64":"1NgD7Rq2/8Fa31kwU2N18krBt3d5zPkwmg60MUW0Gkc="}\'\n')
    envf = tmp_path / "be"; envf.mkdir(); (envf / "backend").mkdir(); (envf / "backend" / ".env").write_text("RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v1\n")
    (envf / "deploy").symlink_to(os.path.join(ROOT, "deploy"))
    r = _run(world, "ensure_release_public_key_pin; echo RC=$?", {"RELEASE_KEY_FINGERPRINT_FILE": str(fp_file)}, cwd=str(envf))
    assert "NOT pinning" in r.stdout and "RELEASE_PUBLIC_KEY_B64=" not in (envf / "backend" / ".env").read_text()
    # matching fingerprint → pinned
    fp_file.write_text("stoic-release-ed25519-v1 SHA256:4c214d393287aac3983178b671e76a35a29d6f0b5e6c815729cc84afaa404d3d\n")
    r = _run(world, "ensure_release_public_key_pin; echo RC=$?", {"RELEASE_KEY_FINGERPRINT_FILE": str(fp_file)}, cwd=str(envf))
    assert "matches the committed fingerprint" in r.stdout and "RELEASE_PUBLIC_KEY_B64=1NgD7Rq2" in (envf / "backend" / ".env").read_text()
    # existing pin that differs from the committed fingerprint → warned, never rewritten
    (envf / "backend" / ".env").write_text("RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v1\nRELEASE_PUBLIC_KEY_B64=" + "QUFB" * 10 + "QQ==\n")
    r = _run(world, "ensure_release_public_key_pin; echo RC=$?", {"RELEASE_KEY_FINGERPRINT_FILE": str(fp_file)}, cwd=str(envf))
    assert "differs from the committed CI key fingerprint" in r.stdout and ("QUFB" * 10) in (envf / "backend" / ".env").read_text()


def test_committed_fingerprint_matches_live_format():
    lines = [l.split() for l in open(os.path.join(ROOT, "release", "release_key.fingerprint")) if l.strip() and not l.startswith("#")]
    assert lines and lines[0][0] == "stoic-release-ed25519-v1" and lines[0][1].startswith("SHA256:") and len(lines[0][1]) == 7 + 64


def test_m114_7_host_profile_signature(monkeypatch):
    import host_profile as hp
    key = "k" * 32; at = "2026-10-08T20:00:00Z"
    sig = hmac.new(hp.derive_host_profile_key(key), f"shared-web-host|cPanel/WHM httpd|{at}".encode(), hashlib.sha256).hexdigest()   # M115-3 derived key
    for k, v in {"STOIC_HOST_PROFILE": "shared-web-host", "STOIC_HOST_MARKERS": '"cPanel/WHM httpd"', "STOIC_HOST_DETECTED_AT": at,
                 "STOIC_HOST_PROFILE_SIG": sig, "LEDGER_ANCHOR_KEY": key}.items():
        monkeypatch.setenv(k, v)
    p = hp.host_profile()
    assert p["verified"] is True and p["shared_web_host"] is True and p["markers"] == "cPanel/WHM httpd" and p["age_hours"] is not None
    monkeypatch.setenv("STOIC_HOST_PROFILE", "dedicated")          # root hand-edits the profile → signature no longer matches
    p = hp.host_profile()
    assert p["verified"] is False and p["profile"] == "dedicated"


def test_m114_2_wiring_and_marker_script():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert upd.index('pause_trading_after_jam "${REF}"') < upd.index("reboot-required") and "clear_deploy_jam_marker" in upd
    pre = open(os.path.join(ROOT, "deploy", "preflight.sh")).read()
    assert "docker compose stop -t 30 worker-trading" in pre and "ops/deploy_jam.py set" in pre and "ops/deploy_jam.py clear" in pre
    ops = open(os.path.join(ROOT, "backend", "routes", "ops_routes.py")).read()
    assert 'checks["deploy_jam"]' in ops and "TRADING PAUSED" in ops and 'checks["host_suitability"]' in ops
    card = open(os.path.join(ROOT, "frontend", "src", "components", "ReadinessCard.jsx")).read()
    assert 'host_suitability: "HOST SUITABILITY"' in card and "readiness-trading-paused" in card
    tu = open(os.path.join(ROOT, "scripts", "test_unit.sh")).read()
    assert "generate_test_manifest.py --check" in tu and '"$COLLECTED" != "$EXPECTED"' not in tu     # M114-5
