"""M117-5 — clean_leftovers recovers the ded5552 jam: `docker rm -f` of a DEAD container fails with
"unlinkat …/merged: device or resource busy" because the overlay is still mounted in the host namespace; the
function prints the error + holders, lazy-unmounts the merged dir and retries once."""
import os

import pytest

from tests.unit.test_update_preflight_shell import ROOT, _run, _stub, world  # noqa: F401

pytestmark = pytest.mark.unit

# docker stub: rm fails with EBUSY while $STUB_STATE/mounted_<id> exists; inspect returns the merged dir
DOCKER = r'''#!/usr/bin/env bash
S="$STUB_STATE"; echo "$*" >> "$S/calls.log"
case "$1 $2" in
  "info ") exit 0 ;;
  "ps -aq") awk -F'|' '{print $1}' "$S/containers" 2>/dev/null; exit 0 ;;
  "inspect -f"|"inspect --format")
      fmt="$3"; id="$4"; line=$(grep "^$id|" "$S/containers" 2>/dev/null | head -1)
      case "$fmt" in *State.Status*) echo "$line" | cut -d'|' -f2 ;; *.Name*) echo "/$(echo "$line" | cut -d'|' -f3)" ;;
                     *MergedDir*) echo "$S/overlay/$id/merged" ;; *) echo "" ;; esac; exit 0 ;;
  "rm -f") shift 2
      for id in "$@"; do
        if [ -e "$S/mounted_$id" ]; then
          echo "Error response from daemon: container $id: driver \"overlay2\" failed to remove root filesystem: unlinkat $S/overlay/$id/merged: device or resource busy" >&2; exit 1
        fi
        grep -v "^$id|" "$S/containers" > "$S/containers.new" 2>/dev/null || true; mv "$S/containers.new" "$S/containers"
      done; exit 0 ;;
  "volume "*) echo "VOLUME COMMAND MUST NEVER RUN" >&2; exit 99 ;;
esac
exit 0
'''
MOUNTPOINT = '#!/usr/bin/env bash\n# mountpoint -q <dir> → 0 while the marker exists\nid=$(basename "$(dirname "$2")"); [ -e "$STUB_STATE/mounted_$id" ]\n'
UMOUNT = '#!/usr/bin/env bash\necho "umount $*" >> "$STUB_STATE/calls.log"\nid=$(basename "$(dirname "$2")"); rm -f "$STUB_STATE/mounted_$id"\n'
FUSER = '#!/usr/bin/env bash\necho "                     USER        PID ACCESS COMMAND"\necho "$2:           root     4242 f.... php-fpm"\n'


def _jam_world(world):   # noqa: F811
    b = world["tmp"] / "bin"
    for name, body in (("docker", DOCKER), ("mountpoint", MOUNTPOINT), ("umount", UMOUNT), ("fuser", FUSER)):
        _stub(b, name, body); os.chmod(b / name, 0o755)
    project = os.path.basename(ROOT)
    (world["state"] / "containers").write_text(f"dead1|dead|{project}-worker-analytics-1\ndead2|dead|{project}-worker-trading-1\nrun1|running|{project}-mongo-1\n")
    (world["state"] / "mounted_dead1").write_text(""); (world["state"] / "mounted_dead2").write_text("")
    _stub(b, "id", "#!/usr/bin/env bash\necho 0\n"); os.chmod(b / "id", 0o755)   # root — unmount path enabled


def test_clean_leftovers_lazy_unmounts_busy_dead_containers(world):   # noqa: F811
    _jam_world(world)
    r = _run(world, "clean_leftovers; echo RC=$?")
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "device or resource busy" in r.stdout and "holder: " in r.stdout and "php-fpm" in r.stdout
    assert "lazily unmounted" in r.stdout and "removed after unmount" in r.stdout and "leftovers removed" in r.stdout
    calls = (world["state"] / "calls.log").read_text()
    assert "umount -l" in calls and " -v" not in calls.replace("umount -l", "") and "volume" not in calls
    left = (world["state"] / "containers").read_text()
    assert "dead1" not in left and "dead2" not in left and "run1|running" in left


def test_clean_leftovers_still_fails_loudly_when_unmount_does_not_help(world):   # noqa: F811
    _jam_world(world)
    _stub(world["tmp"] / "bin", "umount", "#!/usr/bin/env bash\nexit 1\n"); os.chmod(world["tmp"] / "bin" / "umount", 0o755)
    r = _run(world, "clean_leftovers; echo RC=$?")
    assert "RC=1" in r.stdout and "still present after rm -f" in r.stdout and "imunify360" in r.stdout, r.stdout + r.stderr
