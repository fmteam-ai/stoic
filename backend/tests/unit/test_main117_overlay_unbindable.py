"""M117-7 — overlay2 must be UNBINDABLE: slave propagation does not stop a fresh cPanel VirtFS `rbind /var/lib` from
copying live overlay mounts (the ded5552 jam recurred on every recreate). Prerequisite reported by
host_prereqs_missing, fixed by host-prereqs.sh via the existing one-time dockerd restart, applied by
docker-root-slave.sh only while no overlay mount is live."""
import os
import shutil
import subprocess

import pytest

from tests.unit.test_update_preflight_shell import ROOT, _calls, _run, _stub, world  # noqa: F401

pytestmark = pytest.mark.unit

# findmnt stub: PROPAGATION of the overlay dir comes from $STUB_STATE/oprop, everything else from prop;
# `-t overlay` listing from $STUB_STATE/live_overlays (empty = dockerd stopped)
FINDMNT = r'''#!/usr/bin/env bash
case "$*" in
  *"-t overlay"*) cat "$STUB_STATE/live_overlays" 2>/dev/null; exit 0 ;;
  *overlay*) cat "$STUB_STATE/oprop" ;;
  *) cat "$STUB_STATE/prop" ;;
esac
'''
MOUNT = '#!/usr/bin/env bash\necho "mount $*" >> "$STUB_STATE/calls.log"\ncase "$*" in *--make-unbindable*) echo unbindable > "$STUB_STATE/oprop" ;; *--make-slave*) echo slave > "$STUB_STATE/prop" ;; esac\n'


def _overlay_world(world, oprop="slave"):   # noqa: F811
    b = world["tmp"] / "bin"
    _stub(b, "findmnt", FINDMNT); _stub(b, "mount", MOUNT)
    _stub(b, "mountpoint", "#!/usr/bin/env bash\nexit 1\n")
    for n in ("findmnt", "mount", "mountpoint"):
        os.chmod(b / n, 0o755)
    odir = world["tmp"] / "overlay2"; odir.mkdir(exist_ok=True)
    (world["state"] / "prop").write_text("slave"); (world["state"] / "oprop").write_text(oprop)
    (world["state"] / "mdm").write_text("1\n")
    return {"STOIC_OVERLAY_DIR": str(odir), "STOIC_WANT_OVERLAY_UNBINDABLE": "1"}   # M120-1 — these tests model a cPanel/VirtFS host


def test_dedicated_host_skips_overlay_unbindable(world):   # noqa: F811  (M120-1)
    env = _overlay_world(world); env.pop("STOIC_WANT_OVERLAY_UNBINDABLE")
    r = _run(world, "host_prereqs_missing; echo RC=$?", env)
    assert "RC=1" in r.stdout and "unbindable" not in r.stdout, r.stdout + r.stderr
    (world["etc"] / "99-stoic-docker.conf").write_text("fs.may_detach_mounts = 1\n")
    (world["etc"] / "docker.service.d").mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "deploy", "docker-root-slave.sh"), world["etc"] / "stoic-docker-root-slave")
    os.chmod(world["etc"] / "stoic-docker-root-slave", 0o755)
    (world["etc"] / "docker.service.d" / "10-stoic-private-root.conf").write_text("x")
    r = _run(world, "bash deploy/host-prereqs.sh --check; echo RC=$?", env)
    assert "RC=0" in r.stdout and "no VirtFS on this host" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(["sh", os.path.join(ROOT, "deploy", "docker-root-slave.sh"), "/var/lib/docker"],
                       env=world["env"] | {"STUB_STATE": str(world["state"]), "STOIC_OVERLAY_DIR": env["STOIC_OVERLAY_DIR"]}, capture_output=True, text=True)
    assert r.returncode == 0 and "make-unbindable" not in _calls(world)


def test_host_prereqs_missing_reports_bindable_overlay2(world):   # noqa: F811
    env = _overlay_world(world)
    r = _run(world, "host_prereqs_missing; echo RC=$?", env)
    assert "RC=0" in r.stdout and "want unbindable" in r.stdout, r.stdout + r.stderr
    (world["state"] / "oprop").write_text("private,unbindable")
    r = _run(world, "host_prereqs_missing; echo RC=$?", env)
    assert "RC=1" in r.stdout and "unbindable" not in r.stdout


def test_host_prereqs_fixes_overlay2_with_the_one_time_restart(world):   # noqa: F811
    env = _overlay_world(world)
    (world["etc"] / "99-stoic-docker.conf").write_text("fs.may_detach_mounts = 1\n")
    (world["etc"] / "docker.service.d").mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "deploy", "docker-root-slave.sh"), world["etc"] / "stoic-docker-root-slave")
    os.chmod(world["etc"] / "stoic-docker-root-slave", 0o755)
    (world["etc"] / "docker.service.d" / "10-stoic-private-root.conf").write_text("x")
    r = _run(world, "bash deploy/host-prereqs.sh --check; echo RC=$?", env)
    assert "RC=2" in r.stdout and "want unbindable" in r.stdout and "would fix (one dockerd restart" in r.stdout, r.stdout + r.stderr
    # apply: the real docker-root-slave.sh runs against the stubs (root already slave → overlay_unbindable path)
    r = _run(world, "bash deploy/host-prereqs.sh --yes; echo RC=$?", env | {"STOIC_SLAVE_BIN": str(world["etc"] / "stoic-docker-root-slave")})
    assert "RC=0" in r.stdout and "overlay2 unbindable (dockerd restarted once)" in r.stdout, r.stdout + r.stderr
    calls = _calls(world)
    assert "systemctl stop docker" in calls and "mount --make-unbindable" in calls and "systemctl start docker" in calls
    # idempotent: second run — nothing to do, no restart
    (world["state"] / "calls.log").write_text("")
    r = _run(world, "bash deploy/host-prereqs.sh --yes; echo RC=$?", env)
    assert "RC=0" in r.stdout and "overlay2 is unbindable" in r.stdout and "systemctl stop docker" not in _calls(world)


def test_docker_root_slave_defers_unbindable_while_overlays_are_live(world):   # noqa: F811
    env = _overlay_world(world)
    odir = env["STOIC_OVERLAY_DIR"]
    root = "/var/lib/docker"
    (world["state"] / "live_overlays").write_text(f"{odir}/abc/merged\n")
    r = subprocess.run(["sh", os.path.join(ROOT, "deploy", "docker-root-slave.sh"), root], env=world["env"] | {"STUB_STATE": str(world["state"]), "STOIC_OVERLAY_DIR": odir, "STOIC_WANT_OVERLAY_UNBINDABLE": "1"},
                       capture_output=True, text=True)
    assert r.returncode == 0 and "applied at the next dockerd restart" in r.stdout, r.stdout + r.stderr
    assert "make-unbindable" not in _calls(world)
    (world["state"] / "live_overlays").write_text("")
    r = subprocess.run(["sh", os.path.join(ROOT, "deploy", "docker-root-slave.sh"), root], env=world["env"] | {"STUB_STATE": str(world["state"]), "STOIC_OVERLAY_DIR": odir, "STOIC_WANT_OVERLAY_UNBINDABLE": "1"},
                       capture_output=True, text=True)
    assert r.returncode == 0 and "is now unbindable" in r.stdout, r.stdout + r.stderr
    assert "mount --make-unbindable" in _calls(world) and (world["state"] / "oprop").read_text().strip() == "unbindable"
