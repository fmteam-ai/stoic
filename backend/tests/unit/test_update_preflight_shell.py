"""v1.60.7 — deploy/update.sh preflight (deploy/preflight.sh + deploy/host-prereqs.sh) exercised with
stubbed docker / findmnt / sysctl / systemctl / id on PATH: prerequisites missing → applied; leftovers
cleaned; EBUSY → one cleanup + one retry, then a clear stop; volumes are never touched."""
import os
import stat
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DOCKER_STUB = r"""#!/usr/bin/env bash
# fake docker: state in $STUB_STATE — calls.log (every argv), prop (findmnt), containers (id|status|name),
# up_fail_times (how many `compose up` calls fail with EBUSY)
S="$STUB_STATE"; echo "$*" >> "$S/calls.log"
case "$1 $2" in
  "info -f") echo /var/lib/docker; exit 0 ;;
  "info ") exit 0 ;;
  "compose up")
      n=$(cat "$S/up_calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$S/up_calls"
      if [ "$n" -le "$(cat "$S/up_fail_times" 2>/dev/null || echo 0)" ]; then
        echo 'Error response from daemon: driver "overlay2" failed to remove root filesystem: unlinkat /var/lib/docker/overlay2/abc/merged: device or resource busy' >&2; exit 1
      fi; echo "stack up"; exit 0 ;;
  "compose ps") exit 0 ;;
  "compose logs") exit 0 ;;
  "ps -aq") awk -F'|' '{print $1}' "$S/containers" 2>/dev/null; exit 0 ;;
  "inspect -f")
      fmt="$3"; id="$4"; line=$(grep "^$id|" "$S/containers" 2>/dev/null | head -1)
      case "$fmt" in *State.Status*) echo "$line" | cut -d'|' -f2 ;; *.Name*) echo "/$(echo "$line" | cut -d'|' -f3)" ;; *) echo "" ;; esac; exit 0 ;;
  "rm -f") shift 2; for id in "$@"; do grep -v "^$id|" "$S/containers" > "$S/containers.new" 2>/dev/null || true; mv "$S/containers.new" "$S/containers"; done; exit 0 ;;
  "volume "*) echo "VOLUME COMMAND MUST NEVER RUN" >&2; exit 99 ;;
esac
exit 0
"""


def _stub(bin_dir, name, body):
    p = os.path.join(bin_dir, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


@pytest.fixture
def world(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    state = tmp_path / "state"; state.mkdir()
    (state / "prop").write_text("shared")
    (state / "mdm").write_text("0")
    (state / "containers").write_text("")
    _stub(bin_dir, "docker", DOCKER_STUB)
    _stub(bin_dir, "findmnt", '#!/usr/bin/env bash\ncat "$STUB_STATE/prop"\n')
    _stub(bin_dir, "sysctl", '#!/usr/bin/env bash\necho "$*" >> "$STUB_STATE/calls.log"; case "$*" in *may_detach_mounts=1*) echo 1 > "$STUB_STATE/mdm" ;; esac\n')
    _stub(bin_dir, "systemctl", '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_STATE/calls.log"\n'
                                'case "$1" in start) echo slave > "$STUB_STATE/prop" ;; is-active) exit 1 ;; esac\n')
    _stub(bin_dir, "id", '#!/usr/bin/env bash\necho 0\n')
    _stub(bin_dir, "mountpoint", '#!/usr/bin/env bash\nexit 1\n')
    _stub(bin_dir, "mount", '#!/usr/bin/env bash\necho "mount $*" >> "$STUB_STATE/calls.log"\n')
    _stub(bin_dir, "umount", '#!/usr/bin/env bash\nexit 0\n')
    _stub(bin_dir, "nsenter", '#!/usr/bin/env bash\nexit 0\n')
    etc = tmp_path / "etc"; etc.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STUB_STATE=str(state),
               STOIC_SYSCTL_CONF=str(etc / "99-stoic-docker.conf"), STOIC_SLAVE_BIN=str(etc / "stoic-docker-root-slave"),
               STOIC_DOCKER_DROPIN=str(etc / "docker.service.d" / "10-stoic-private-root.conf"),
               STOIC_MAY_DETACH_MOUNTS=str(state / "mdm"), STOIC_REPAIR_JOURNAL=str(tmp_path / "journal.jsonl"),
               STOIC_SKIP_MOUNT_FIX="1", PREFLIGHT_YES="1")
    return {"env": env, "state": state, "etc": etc, "tmp": tmp_path}


def _run(world, script, extra_env=None, cwd=None):
    env = dict(world["env"]); env.update(extra_env or {})
    full = textwrap.dedent(f"""
        set -u; cd {cwd or ROOT}
        . deploy/lib.sh; . deploy/preflight.sh
        {script}
    """)
    return subprocess.run(["bash", "-c", full], env=env, capture_output=True, text=True, timeout=120)


def _calls(world):
    p = world["state"] / "calls.log"
    return p.read_text() if p.exists() else ""


def test_prereqs_missing_are_applied_and_idempotent(world):
    r = _run(world, "preflight_host; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "applying host prerequisites" in r.stdout
    assert (world["state"] / "mdm").read_text().strip() == "1"
    assert "fs.may_detach_mounts = 1" in (world["etc"] / "99-stoic-docker.conf").read_text()
    assert (world["etc"] / "docker.service.d" / "10-stoic-private-root.conf").exists()
    assert "systemctl stop docker" in _calls(world) and "systemctl start docker" in _calls(world)
    assert (world["state"] / "prop").read_text().strip() == "slave"
    # second run: nothing changes, no dockerd restart
    (world["state"] / "calls.log").write_text("")
    r2 = _run(world, "preflight_host; echo RC=$?")
    assert "RC=0" in r2.stdout and "host prerequisites present" in r2.stdout
    assert "systemctl stop docker" not in _calls(world)


def test_no_host_changes_refuses_with_exact_command(world):
    r = _run(world, "preflight_host; echo RC=$?", {"PREFLIGHT_NO_HOST_CHANGES": "1"})
    assert "RC=1" in r.stdout
    assert "sudo bash deploy/host-prereqs.sh --yes" in r.stdout
    assert (world["state"] / "mdm").read_text().strip() == "0"          # nothing applied
    assert "systemctl stop docker" not in _calls(world)


def test_leftovers_cleaned_without_touching_volumes(world):
    project = os.path.basename(ROOT)
    (world["state"] / "containers").write_text("\n".join([
        f"aaaa|dead|{project}-backend-1",
        f"bbbb|removing|{project}-worker-trading-1",
        f"cccc|created|{project}-worker-model-1",
        f"dddd|exited|0123456789ab_{project}-backend-1",
        f"eeee|running|{project}-mongo-1",
        f"ffff|exited|{project}-frontend-1",
    ]) + "\n")
    r = _run(world, "clean_leftovers; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    left = (world["state"] / "containers").read_text()
    assert "aaaa" not in left and "bbbb" not in left and "cccc" not in left and "dddd" not in left
    assert "eeee|running" in left and "ffff|exited" in left                  # running + plain exited are kept
    calls = _calls(world)
    assert "rm -f" in calls and " -v" not in calls and "volume" not in calls


def test_ebusy_retries_once_then_succeeds(world):
    (world["state"] / "up_fail_times").write_text("1")
    r = _run(world, "compose_up_guarded; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "cleaning leftovers once more and retrying compose up ONCE" in r.stdout
    assert (world["state"] / "up_calls").read_text().strip() == "2"
    assert "volume" not in _calls(world)


def test_ebusy_twice_stops_with_reboot_recipe(world):
    (world["state"] / "up_fail_times").write_text("5")
    r = _run(world, "compose_up_guarded; echo RC=$?")
    assert "RC=2" in r.stdout, r.stdout + r.stderr
    assert (world["state"] / "up_calls").read_text().strip() == "2"          # exactly ONE retry
    assert "reboot required" in r.stdout and "docker update --restart=no $(docker ps -aq)" in r.stdout
    assert "docker ps -aq | xargs -r docker rm -f" in r.stdout and "docker compose up -d" in r.stdout


def test_scripts_never_remove_volumes():
    for rel in ("deploy/preflight.sh", "deploy/host-prereqs.sh", "deploy/restart.sh", "deploy/update.sh"):
        src = open(os.path.join(ROOT, rel)).read()
        assert "volume rm" not in src and "docker volume" not in src and "rm -f -v" not in src and "rm -fv" not in src, rel
        assert "down -v" not in src and "--volumes" not in src, rel


def test_update_sh_wiring():
    upd = open(os.path.join(ROOT, "deploy", "update.sh")).read()
    assert upd.index("preflight_host || gate_refused") < upd.index("clean_leftovers || gate_refused") < upd.index("provision_images || rollback")
    assert "compose_up_guarded" in upd and 'UP_RC}" = 2' in upd and "--no-host-changes" in upd and "PREFLIGHT_YES" in upd
    rs = open(os.path.join(ROOT, "deploy", "restart.sh")).read()
    assert "--env-changed" in rs and "preflight_host || exit 1" in rs and "compose_up_guarded --force-recreate" in rs
    mk = open(os.path.join(ROOT, "Makefile")).read()
    assert "apply-env:" in mk and "deploy/restart.sh --env-changed --yes" in mk
