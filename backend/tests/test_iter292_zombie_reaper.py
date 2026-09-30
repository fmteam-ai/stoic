"""r292 — compose_up must survive containers that die WHILE compose recreates them
(RHEL 8 overlay EBUSY: compose renames old container to <12hex>_stoic-*, removal
fails, name conflict on every later `up`). Runs deploy/lib.sh against a fake
`docker` shim that replays the exact failure seen on the AlmaLinux host."""
import os
import stat
import subprocess
import textwrap

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

FAKE_DOCKER = textwrap.dedent(r'''
#!/usr/bin/env bash
# state file: list of "<id> <name> <status>" lines; UP_FAILS counts remaining `compose up` failures
S="$FAKE_STATE"
log() { echo "$*" >> "$S.calls"; }
log "$@"
case "$1 $2" in
  "compose ps")   # --status removing --status dead
      awk '($3=="dead"||$3=="removing") && index($2,"stoic-"){print $1}' "$S"; exit 0 ;;
  "compose up")
      left=$(( $(cat "$S.upfails") ))
      if [ "$left" -gt 0 ]; then
        echo $((left-1)) > "$S.upfails"
        # simulate: running worker renamed to <hash>_stoic-worker-tuning-1 and stuck dead
        sed -i 's/^\(b71a5514e1ac\) stoic-worker-tuning-1 running$/\1 6dd45105ceb4_stoic-worker-tuning-1 dead/' "$S"
        echo 'Error response from daemon: Conflict. The container name "/6dd45105ceb4_stoic-worker-tuning-1" is already in use' >&2
        exit 1
      fi
      exit 0 ;;
  "ps -a")
      case "$*" in
        *--format*) awk '{print $1, $2}' "$S" ;;
        *status=dead*) awk -v N="$(echo "$*" | sed -n 's/.*name=\([^ ]*\).*/\1/p')" '($3=="dead"||$3=="removing") && index($2,N){print $1}' "$S" ;;
      esac; exit 0 ;;
  "rm -f")
      shift 2; for id in "$@"; do sed -i "/^$id /d" "$S"; done; exit 0 ;;
  "inspect -f") echo ""; exit 0 ;;
  "info ") exit 0 ;;
esac
exit 0
''').lstrip()


@pytest.fixture
def sandbox(tmp_path):
    binpath = tmp_path / "bin"; binpath.mkdir()
    dk = binpath / "docker"; dk.write_text(FAKE_DOCKER); dk.chmod(dk.stat().st_mode | stat.S_IEXEC)
    for tool in ("systemctl", "umount", "sysctl"):
        t = binpath / tool; t.write_text("#!/usr/bin/env bash\nexit 0\n"); t.chmod(t.stat().st_mode | stat.S_IEXEC)
    proj = tmp_path / "stoic"; proj.mkdir()
    state = tmp_path / "state"
    state.write_text("aaaaaaaaaaaa stoic-mongo-1 running\n"
                     "bbbbbbbbbbbb stoic-signer-1 running\n"
                     "b71a5514e1ac stoic-worker-tuning-1 running\n")
    (tmp_path / "state.upfails").write_text("1")
    (tmp_path / "state.calls").write_text("")
    return {"bin": binpath, "proj": proj, "state": state, "tmp": tmp_path}


def _run(sandbox, script):
    env = dict(os.environ, PATH=f"{sandbox['bin']}:{os.environ['PATH']}", FAKE_STATE=str(sandbox["state"]))
    return subprocess.run(["bash", "-c", f"set -euo pipefail; cd {sandbox['proj']}; . {_REPO}/deploy/lib.sh; {script}"],
                          env=env, capture_output=True, text=True)


class TestReaper:
    def test_zombie_ids_matches_compose_renamed_leftovers(self, sandbox):
        sandbox["state"].write_text("aaaaaaaaaaaa stoic-mongo-1 running\n"
                                    "b71a5514e1ac 6dd45105ceb4_stoic-worker-tuning-1 dead\n"
                                    "cccccccccccc e2dad2e5e33c_stoic-worker-reconciliation-1 running\n"
                                    "dddddddddddd other-project-1 dead\n")
        r = _run(sandbox, "zombie_ids")
        assert r.returncode == 0, r.stderr
        ids = set(r.stdout.split())
        assert ids == {"b71a5514e1ac", "cccccccccccc"}, ids   # renamed leftovers even if still "running"; foreign dead ignored

    def test_compose_up_reaps_and_retries_once(self, sandbox):
        r = _run(sandbox, "compose_up")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "compose up failed — reaping" in r.stdout
        assert "removing stale containers left from a previous run: b71a5514e1ac" in r.stdout
        calls = (sandbox["tmp"] / "state.calls").read_text()
        assert calls.count("compose up -d --remove-orphans") == 2
        assert "rm -f b71a5514e1ac" in calls
        remaining = sandbox["state"].read_text()
        assert "stoic-mongo-1 running" in remaining and "stoic-signer-1 running" in remaining   # healthy services untouched
        assert "6dd45105ceb4_" not in remaining

    def test_compose_up_gives_up_after_one_retry(self, sandbox):
        (sandbox["tmp"] / "state.upfails").write_text("2")
        r = _run(sandbox, "compose_up")
        assert r.returncode != 0
        assert (sandbox["tmp"] / "state.calls").read_text().count("compose up") == 2

    def test_no_zombies_is_a_noop(self, sandbox):
        (sandbox["tmp"] / "state.upfails").write_text("0")
        r = _run(sandbox, "compose_up")
        assert r.returncode == 0, r.stderr
        assert "stale containers" not in r.stdout
        assert "rm -f" not in (sandbox["tmp"] / "state.calls").read_text()


class TestBootstrapStatic:
    def test_bootstrap_sets_may_detach_mounts_persistently(self):
        with open(f"{_REPO}/deploy/bootstrap.sh") as f:
            body = f.read()
        assert 'BOOTSTRAP_VERSION="r293"' in body
        assert "/etc/sysctl.d/99-stoic-docker.conf" in body
        assert "fs.may_detach_mounts = 1" in body
        assert "may_detach_mounts=0" in body   # system-check WARN
        assert body.count("may_detach_mounts") >= 5
        assert subprocess.run(["bash", "-n", f"{_REPO}/deploy/bootstrap.sh"]).returncode == 0
        assert subprocess.run(["bash", "-n", f"{_REPO}/deploy/lib.sh"]).returncode == 0
