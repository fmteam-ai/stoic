"""STOIC host migrator — ops sidecar that moves a running STOIC install to a
new host with a short freeze window. Runs NEXT TO the stack (own container,
docker socket + SSH), never inside the API container. The API proxies the
admin wizard to this loopback-only HTTP service (shared METRICS_TOKEN).

Steps: preflight → install (rsync checkout+secrets+env+backups+TLS certs,
install.sh on target, target workers stopped) → warm_sync (mongodump|restore
while source trades) → [gate] freeze (source API+workers stop) → final_sync →
verify_target (workers up, release-readiness) → [gate] cutover_check (DNS/TLS
/EA heartbeats on target) → [gate] decommission (source fully stopped).
`abort` restarts the source at any point before decommission. stdlib only.
"""
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("STOIC_ROOT", os.getcwd())
STATE_DIR = os.path.join(ROOT, "release", "migration")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
KEY_FILE = os.path.join(STATE_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(STATE_DIR, "known_hosts")
LOG_CAP = 3000
APP_SERVICES = ["backend", "worker-trading", "worker-protection", "worker-reconciliation",
                "worker-analytics", "worker-model", "worker-tuning"]
WORKERS = [s for s in APP_SERVICES if s.startswith("worker-")]
MONGO_AUTH = ('-u "$MONGO_INITDB_ROOT_USERNAME" -p "$(cat "$MONGO_INITDB_ROOT_PASSWORD_FILE")" '
              '--authenticationDatabase admin')
RSYNC_EXCLUDES = ["node_modules", "frontend/build", "frontend/dist", "__pycache__", ".pytest_cache",
                  "release/migration", "backend/models_store/*/tmp*"]
STEPS = [("preflight", "Preflight — SSH, docker, disk, ports on the new host"),
         ("install", "Install STOIC on the new host (checkout · secrets · env · backups · TLS certs)"),
         ("warm_sync", "Warm data sync — full MongoDB copy while the source keeps trading"),
         ("freeze", "Freeze source — stop API + workers (downtime starts)"),
         ("final_sync", "Final delta sync — copy everything written since the warm sync"),
         ("verify_target", "Verify new host — workers up, release-readiness green"),
         ("cutover_check", "Cutover — DNS → new host, TLS answers, EA heartbeats arrive"),
         ("decommission", "Decommission source — everything stopped, migration complete")]
GATES = {"warm_sync": "freeze", "verify_target": "cutover_check", "cutover_check": "decommission"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_state() -> dict:
    return {"id": None, "status": "idle", "target": {}, "source": {}, "facts": {},
            "current_step": None, "awaiting": None, "downtime_started_at": None,
            "steps": [{"id": s, "label": l, "status": "pending", "detail": {}} for s, l in STEPS],
            "log": [], "error": None, "updated_at": now_iso()}


class Migrator:
    def __init__(self, run=None, ssh_bin="ssh", state_file=STATE_FILE):
        self.state_file = state_file
        self._run = run or self._subprocess
        self.ssh_bin = ssh_bin
        self.lock = threading.RLock()
        self.state = self._load()
        self.thread = None

    # ── persistence ─────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            return json.load(open(self.state_file))
        except Exception:  # noqa: BLE001
            return _fresh_state()

    def _save(self):
        self.state["updated_at"] = now_iso()
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        json.dump(self.state, open(tmp, "w"), indent=1)
        os.replace(tmp, self.state_file)

    def snapshot(self) -> dict:
        with self.lock:
            s = json.loads(json.dumps(self.state))
        s["log"] = s["log"][-400:]
        s["simulated"] = self._run is simulated_run
        return s

    def log(self, step: str, line: str):
        with self.lock:
            self.state["log"].append({"at": now_iso(), "step": step, "line": line[:2000]})
            del self.state["log"][:-LOG_CAP]
            self._save()

    def _step(self, sid: str) -> dict:
        return next(s for s in self.state["steps"] if s["id"] == sid)

    def _mark(self, sid: str, status: str, **detail):
        with self.lock:
            st = self._step(sid)
            st["status"] = status
            st["detail"].update(detail)
            st[f"{'started' if status == 'running' else 'ended'}_at"] = now_iso()
            self.state["current_step"] = sid if status == "running" else self.state["current_step"]
            self._save()

    # ── command execution ───────────────────────────────────────────────
    def _subprocess(self, cmd: str, step: str, timeout: int) -> tuple:
        p = subprocess.Popen(cmd, shell=True, cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace")
        out, deadline = [], time.time() + timeout
        for line in p.stdout:
            line = line.rstrip("\n")
            out.append(line)
            self.log(step, line)
            if time.time() > deadline:
                p.kill()
                self.log(step, f"!! timeout after {timeout}s")
                return 124, "\n".join(out)
        p.wait()
        return p.returncode, "\n".join(out)

    def sh(self, cmd: str, step: str, timeout: int = 600, check: bool = True) -> str:
        self.log(step, f"$ {cmd if len(cmd) < 400 else cmd[:400] + ' …'}")
        rc, out = self._run(cmd, step, timeout)
        if check and rc != 0:
            raise RuntimeError(f"[{step}] command failed (rc={rc}): {cmd[:200]}\n{out[-1500:]}")
        return out

    def ssh_opts(self) -> str:
        t = self.state["target"]
        return (f"{self.ssh_bin} -i {shlex.quote(KEY_FILE)} -p {int(t.get('port', 22))} "
                f"-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={shlex.quote(KNOWN_HOSTS)}")

    def dest(self) -> str:
        t = self.state["target"]
        return f"{shlex.quote(t['user'])}@{shlex.quote(t['host'])}"

    def ssh_prefix(self) -> str:
        return f"{self.ssh_opts()} {self.dest()}"

    def remote(self, script: str, step: str, timeout: int = 600, check: bool = True) -> str:
        return self.sh(f"{self.ssh_prefix()} {shlex.quote('bash -s')} <<'STOIC_EOF'\nset -euo pipefail\n"
                       f"{script}\nSTOIC_EOF", step, timeout, check)

    def remote_cd(self, script: str, step: str, timeout: int = 600, check: bool = True) -> str:
        return self.remote(f"cd {shlex.quote(self.state['target']['path'])}\n{script}", step, timeout, check)

    # ── keys / hosts ────────────────────────────────────────────────────
    def public_key(self) -> str:
        os.makedirs(STATE_DIR, exist_ok=True)
        if not os.path.exists(KEY_FILE):
            self._run(f"ssh-keygen -q -t ed25519 -N '' -C stoic-migrator -f {shlex.quote(KEY_FILE)}",
                      "keys", 60)
        try:
            return open(KEY_FILE + ".pub").read().strip()
        except FileNotFoundError:
            return ""

    def scan_host(self, host: str, port: int) -> dict:
        rc, out = self._run(f"ssh-keyscan -p {int(port)} -T 10 -t ed25519,ecdsa,rsa {shlex.quote(host)} 2>/dev/null",
                            "preflight", 30)
        keys = [l for l in out.splitlines() if l and not l.startswith("#")]
        if not keys:
            raise RuntimeError(f"ssh-keyscan: no host key from {host}:{port} — is SSH reachable?")
        os.makedirs(STATE_DIR, exist_ok=True)
        open(KNOWN_HOSTS, "w").write("\n".join(keys) + "\n")
        rc, fp = self._run(f"ssh-keygen -lf {shlex.quote(KNOWN_HOSTS)}", "preflight", 30)
        return {"fingerprints": [l.split()[1] for l in fp.splitlines() if len(l.split()) > 1], "keys": len(keys)}

    # ── source facts ────────────────────────────────────────────────────
    def _env(self, path: str) -> dict:
        try:
            lines = open(os.path.join(ROOT, path)).read().splitlines()
        except FileNotFoundError:
            return {}
        return dict(l.split("=", 1) for l in lines if "=" in l and not l.startswith("#"))

    def source_facts(self) -> dict:
        env = self._env(".env")
        cf = env.get("COMPOSE_FILE", "docker-compose.yml")
        rc, sha = self._run("git rev-parse HEAD", "preflight", 30)
        rc, tag = self._run("git tag --points-at HEAD | grep -E '^v[0-9]' | head -1", "preflight", 30)
        rc, dbsize = self._run("docker compose exec -T mongo du -sb /data/db | cut -f1", "preflight", 120)
        rc, bsize = self._run("du -sb backups 2>/dev/null | cut -f1", "preflight", 60)
        rc, hb = self._run("docker compose exec -T mongo sh -c " + shlex.quote(
            f"mongosh {MONGO_AUTH} --quiet \"$DB_NAME\" --eval "
            "'db.accounts.countDocuments({status:\"connected\"})'"), "preflight", 120)
        return {"sha": sha.strip(), "tag": tag.strip() or None, "compose_file": cf,
                "tls": "docker-compose.tls.yml" in cf, "forecast": "docker-compose.forecast.yml" in cf,
                "registry": env.get("DEPLOY_MODE", "") == "registry", "domain": env.get("DOMAIN") or None,
                "project": (env.get("COMPOSE_PROJECT_NAME") or os.path.basename(ROOT)).lower(),
                "db_bytes": int(dbsize.strip() or 0) if dbsize.strip().isdigit() else None,
                "backups_bytes": int(bsize.strip() or 0) if bsize.strip().isdigit() else 0,
                "connected_accounts": int(hb.strip()) if hb.strip().isdigit() else None}

    # ── steps ───────────────────────────────────────────────────────────
    def preflight(self, target: dict, accept_fingerprint: str | None = None) -> dict:
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("a migration step is running")
            if self.state["status"] in ("awaiting",) and self.state["current_step"] not in (None, "preflight"):
                raise RuntimeError("a migration is in progress — abort it before starting a new preflight")
            self.state = _fresh_state()
            self.state["id"] = f"mig_{uuid.uuid4().hex[:10]}"
            self.state["target"] = {"host": target["host"], "user": target["user"],
                                    "port": int(target.get("port", 22)), "path": target["path"]}
            self.state["status"] = "running"
            self._save()
        self._mark("preflight", "running")
        try:
            scan = self.scan_host(target["host"], int(target.get("port", 22)))
            facts = {"host_key": scan}
            if accept_fingerprint and accept_fingerprint not in scan["fingerprints"]:
                raise RuntimeError(f"host key fingerprint mismatch: got {scan['fingerprints']}")
            facts["source"] = self.source_facts()
            self.state["source"] = facts["source"]
            path = shlex.quote(target["path"])
            out = self.remote(f"""
echo "OS=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "ARCH=$(uname -m)"
echo "DOCKER=$(command -v docker >/dev/null && docker --version || echo missing)"
echo "COMPOSE=$(docker compose version 2>/dev/null || echo missing)"
echo "RSYNC=$(command -v rsync >/dev/null && echo ok || echo missing)"
echo "SUDO=$(sudo -n true 2>/dev/null && echo ok || echo missing)"
echo "DOCKER_ACCESS=$(docker info >/dev/null 2>&1 && echo ok || echo denied)"
mkdir -p {path} 2>/dev/null || true
echo "PATH_ENTRIES=$(ls -A {path} 2>/dev/null | wc -l)"
echo "DISK_AVAIL=$(df -B1 --output=avail {path} 2>/dev/null | tail -1 | tr -d ' ')"
echo "MEM_TOTAL=$(awk '/MemTotal/{{print $2*1024}}' /proc/meminfo)"
echo "PORTS_BUSY=$( (ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null) | awk '{{print $4}}' | grep -oE ':(80|443|8001|3000|27017)$' | tr -d ':' | sort -u | tr '\\n' ',')"
echo "PUBLIC_IP=$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{{print $1}}')"
""", "preflight", 120)
            kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l and re.match(r"^[A-Z_]+=", l))
            need = (facts["source"].get("db_bytes") or 0) * 3 + facts["source"].get("backups_bytes", 0) + 6 * 1024 ** 3
            avail = int(kv.get("DISK_AVAIL") or 0)
            checks = {
                "ssh": True,
                "docker": not kv.get("DOCKER", "missing").startswith("missing"),
                "compose_v2": not kv.get("COMPOSE", "missing").startswith("missing"),
                "docker_access": kv.get("DOCKER_ACCESS") == "ok",
                "rsync": kv.get("RSYNC") == "ok",
                "path_empty": kv.get("PATH_ENTRIES", "0") == "0",
                "disk": avail >= need,
                "ports_free": not kv.get("PORTS_BUSY"),
                "memory": int(kv.get("MEM_TOTAL") or 0) >= (16 if facts["source"].get("forecast") else 4) * 1024 ** 3,
                "release_tag": bool(facts["source"].get("tag")) or not facts["source"].get("tls"),
            }
            facts["target"] = {**kv, "disk_needed": need, "checks": checks, "public_ip": kv.get("PUBLIC_IP")}
            facts["ok"] = all(checks.values())
            with self.lock:
                self.state["facts"] = facts
                self.state["target"]["public_ip"] = kv.get("PUBLIC_IP")
                self.state["status"] = "awaiting" if facts["ok"] else "failed"
                self.state["awaiting"] = "install" if facts["ok"] else None
                self.state["error"] = None if facts["ok"] else "preflight checks failed: " + ", ".join(
                    k for k, v in checks.items() if not v)
            self._mark("preflight", "done" if facts["ok"] else "failed", checks=checks)
            return facts
        except Exception as e:  # noqa: BLE001
            self._fail("preflight", e)
            raise

    def _fail(self, sid: str, e: Exception):
        self.log(sid, f"!! {e}")
        with self.lock:
            self.state["status"] = "failed"
            self.state["error"] = str(e)[:2000]
            self.state["awaiting"] = None
        self._mark(sid, "failed", error=str(e)[:500])

    def _install_cmd(self) -> str:
        src = self.state["source"]
        mode = f"--production {shlex.quote(src['domain'])}" if src.get("tls") else "--dev"
        flags = (" --with-forecast" if src.get("forecast") else "") + (" --registry" if src.get("registry") else "")
        return f"deploy/install.sh {mode}{flags}"

    def install(self):
        sid = "install"
        self._mark(sid, "running")
        t, src = self.state["target"], self.state["source"]
        path = shlex.quote(t["path"])
        ex = " ".join(f"--exclude={shlex.quote(x)}" for x in RSYNC_EXCLUDES)
        self.log(sid, "syncing checkout, secrets, env files, backups and release evidence")
        self.sh(f"rsync -az --delete {ex} -e {shlex.quote(self.ssh_opts())} ./ {self.dest()}:{path}/", sid, 3600)
        self.remote_cd("chmod 700 secrets && chmod 600 secrets/* && chmod +x deploy/*.sh", sid, 60)
        if src.get("tls"):
            vol = f"{src['project']}_caddy_data"
            self.log(sid, f"carrying TLS certificates ({vol}) so the new host answers HTTPS immediately")
            self.remote(f"docker volume create {shlex.quote(vol)} >/dev/null", sid, 60)
            untar = shlex.quote(f"docker run --rm -i -v {vol}:/data alpine tar xz -C /data")
            self.sh(f"docker run --rm -v {shlex.quote(vol)}:/data:ro alpine tar cz -C /data . | "
                    f"{self.ssh_prefix()} {untar}", sid, 600)
        self.log(sid, f"running {self._install_cmd()} on the new host (images build/pull — this takes a while)")
        self.remote_cd(self._install_cmd(), sid, 5400)
        self.log(sid, "stopping workers on the new host — it must NOT trade before cutover")
        self.remote_cd("docker compose stop " + " ".join(WORKERS), sid, 300)
        self._mark(sid, "done")

    def _dump_restore(self, sid: str):
        dump = f"docker compose exec -T mongo sh -c {shlex.quote(f'mongodump {MONGO_AUTH} --archive --gzip')}"
        restore = (f"cd {shlex.quote(self.state['target']['path'])} && docker compose exec -T mongo sh -c "
                   f"{shlex.quote(f'mongorestore {MONGO_AUTH} --archive --gzip --drop')}")
        self.sh(f"set -o pipefail; {dump} | {self.ssh_prefix()} {shlex.quote(restore)}", sid, 3600)

    def warm_sync(self):
        self._mark("warm_sync", "running")
        self._dump_restore("warm_sync")
        self._mark("warm_sync", "done")

    def freeze(self):
        self._mark("freeze", "running")
        self.sh("docker compose stop " + " ".join(APP_SERVICES), "freeze", 300)
        with self.lock:
            self.state["downtime_started_at"] = now_iso()
            self._save()
        self._mark("freeze", "done")

    def final_sync(self):
        self._mark("final_sync", "running")
        self._dump_restore("final_sync")
        self._mark("final_sync", "done")

    def verify_target(self):
        sid = "verify_target"
        self._mark(sid, "running")
        self.remote_cd("docker compose up -d", sid, 600)
        out = self.remote_cd("""
tok=$(cat secrets/metrics_token)
for i in $(seq 1 45); do
  if body=$(curl -fsS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness 2>/dev/null); then
    echo "READY=${body}"; exit 0; fi
  sleep 4
done
echo "READY="; curl -sS -H "X-Metrics-Token: ${tok}" http://127.0.0.1:8001/api/ops/release-readiness || true; exit 1
""", sid, 400, check=False)
        m = re.search(r"^READY=(\{.*)$", out, re.M)
        if not m:
            raise RuntimeError("new host never reported release-readiness READY")
        health = self.remote("curl -fsS http://127.0.0.1:8001/api/health", sid, 60)
        self._mark(sid, "done", readiness=json.loads(m.group(1)) if m.group(1).startswith("{") else m.group(1),
                   health=json.loads(health) if health.strip().startswith("{") else health.strip())

    def cutover_check(self) -> dict:
        sid = "cutover_check"
        self._mark(sid, "running")
        src, t = self.state["source"], self.state["target"]
        domain, ip = src.get("domain"), t.get("public_ip")
        res = {"domain": domain, "target_ip": ip}
        if domain:
            try:
                res["dns"] = sorted({a[4][0] for a in socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)})
            except OSError as e:
                res["dns"] = []
                res["dns_error"] = str(e)
            res["dns_points_to_target"] = bool(ip) and ip in res["dns"]
            res["dns_proxied"] = bool(res["dns"]) and not res["dns_points_to_target"]
            rc, out = self._run(f"curl -fsS -m 10 --resolve {shlex.quote(domain)}:443:{shlex.quote(ip or '0.0.0.0')} "
                                f"https://{shlex.quote(domain)}/api/health", sid, 30)
            res["target_serves_domain_tls"] = rc == 0
            rc, out = self._run(f"curl -fsS -m 10 https://{shlex.quote(domain)}/api/health", sid, 30)
            res["public_health"] = rc == 0
        since = self.state.get("downtime_started_at") or "1970"
        out = self.remote_cd("docker compose exec -T mongo sh -c " + shlex.quote(
            f"mongosh {MONGO_AUTH} --quiet \"$DB_NAME\" --eval "
            f"'db.accounts.countDocuments({{last_heartbeat:{{$gt:\"{since}\"}}}})'"), sid, 120, check=False)
        digits = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
        res["ea_heartbeats_on_target"] = int(digits[-1]) if digits else 0
        res["connected_accounts_at_source"] = src.get("connected_accounts")
        res["ok"] = res["ea_heartbeats_on_target"] > 0 or (src.get("connected_accounts") == 0
                                                          and res.get("public_health", True))
        res["checked_at"] = now_iso()
        self._mark(sid, "done" if res["ok"] else "pending", **res)
        with self.lock:
            self.state["facts"]["cutover"] = res
            self._save()
        return res

    def decommission(self):
        sid = "decommission"
        self._mark(sid, "running")
        self.sh("docker compose stop", sid, 600)
        open(os.path.join(STATE_DIR, "DECOMMISSIONED"), "w").write(
            json.dumps({"at": now_iso(), "target": self.state["target"], "id": self.state["id"]}))
        self._mark(sid, "done")

    # ── orchestration ───────────────────────────────────────────────────
    def _bg(self, fn, *steps):
        def runner():
            sid = None
            try:
                for sid in steps:
                    getattr(self, sid)()
                last = steps[-1]
                with self.lock:
                    if self.state["status"] == "running":
                        if last == "decommission":
                            self.state["status"], self.state["awaiting"] = "done", None
                        elif last == "cutover_check" and not self.state["facts"].get("cutover", {}).get("ok"):
                            self.state["status"], self.state["awaiting"] = "awaiting", "cutover_check"
                        else:
                            self.state["status"], self.state["awaiting"] = "awaiting", GATES.get(last)
                    self._save()
            except Exception as e:  # noqa: BLE001
                self._fail(sid or steps[0], e)
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("a migration step is already running")
            self.state["status"], self.state["awaiting"], self.state["error"] = "running", None, None
            self._save()
        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def start(self):
        if self.state["awaiting"] != "install":
            raise RuntimeError("run a successful preflight first")
        self._bg(None, "install", "warm_sync")

    def advance(self, step: str):
        allowed = {"freeze": ("freeze", "final_sync", "verify_target"),
                   "cutover_check": ("cutover_check",),
                   "decommission": ("decommission",)}
        if step not in allowed:
            raise RuntimeError(f"unknown step {step}")
        if step == "decommission" and not self.state["facts"].get("cutover", {}).get("ok"):
            raise RuntimeError("cutover not confirmed — EA heartbeats have not reached the new host")
        if self.state["awaiting"] != step:
            raise RuntimeError(f"migration is not awaiting '{step}' (awaiting: {self.state['awaiting']})")
        self._bg(None, *allowed[step])

    def retry(self):
        """Re-run the failed step chain from the step that failed."""
        failed = [s["id"] for s in self.state["steps"] if s["status"] == "failed"]
        if self.state["status"] != "failed" or not failed:
            raise RuntimeError("nothing to retry")
        sid = failed[0]
        chains = {"install": ("install", "warm_sync"), "warm_sync": ("warm_sync",),
                  "freeze": ("freeze", "final_sync", "verify_target"), "final_sync": ("final_sync", "verify_target"),
                  "verify_target": ("verify_target",), "cutover_check": ("cutover_check",),
                  "decommission": ("decommission",)}
        if sid not in chains:
            raise RuntimeError("re-run preflight instead")
        with self.lock:
            self.state["status"] = "awaiting"
            self._step(sid)["status"] = "pending"
        self._bg(None, *chains[sid])

    def abort(self) -> dict:
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("wait for the running step to finish (or fail) before aborting")
            if self.state["status"] == "done":
                raise RuntimeError("source already decommissioned — start the source manually if needed")
        restarted = False
        if self.state.get("downtime_started_at"):
            self.sh("docker compose up -d", "abort", 600, check=False)
            restarted = True
        if self._step("install")["status"] == "done":
            self.remote_cd("docker compose stop " + " ".join(WORKERS), "abort", 300, check=False)
        with self.lock:
            self.state["status"], self.state["awaiting"] = "aborted", None
            self.state["error"] = None
            self._save()
        self.log("abort", f"migration aborted — source {'restarted' if restarted else 'was never frozen'}")
        return {"restarted_source": restarted}


# ── Rehearsal mode (MIGRATOR_DRY_RUN=1): no command touches anything ────────
_SIM = [("ssh-keyscan", "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISIMULATEDKEY"),
        ("ssh-keygen -lf", "256 SHA256:SIMULATED-FINGERPRINT 203.0.113.10 (ED25519)"),
        ("git rev-parse", "f" * 40), ("git tag --points-at", "v0.0.0-rehearsal"),
        ("du -sb /data/db", "1500000000"), ("du -sb backups", "200000000"),
        ("countDocuments({status", "2"), ("countDocuments({last_heartbeat", lambda: "2" if time.time() % 60 > 20 else "0"),
        ("PATH_ENTRIES", "OS=Simulated Linux\nARCH=x86_64\nDOCKER=Docker version 27 (simulated)\nCOMPOSE=v2 (simulated)\n"
                         "RSYNC=ok\nSUDO=ok\nDOCKER_ACCESS=ok\nPATH_ENTRIES=0\nDISK_AVAIL=400000000000\n"
                         "MEM_TOTAL=34359738368\nPORTS_BUSY=\nPUBLIC_IP=203.0.113.10"),
        ("READY=", 'READY={"ready": true, "workers": 6, "simulated": true}'),
        ("api/health", '{"status":"ok","build_sha":"simulated"}')]


def simulated_run(cmd: str, step: str, timeout: int) -> tuple:
    if "ssh-keygen -q" in cmd:
        os.makedirs(STATE_DIR, exist_ok=True)
        open(KEY_FILE, "w").write("SIMULATED PRIVATE KEY\n")
        open(KEY_FILE + ".pub", "w").write("ssh-ed25519 AAAASIMULATED stoic-migrator-rehearsal\n")
        return 0, ""
    time.sleep(0.6 if step in ("install", "warm_sync", "final_sync") else 0.15)
    for key, out in _SIM:
        if key in cmd:
            return 0, out() if callable(out) else out
    return 0, "[simulated] ok"


# ── HTTP ────────────────────────────────────────────────────────────────
def _token() -> str:
    f = os.environ.get("MIGRATOR_TOKEN_FILE", "/run/secrets/metrics_token")
    try:
        return open(f).read().strip()
    except FileNotFoundError:
        return os.environ.get("MIGRATOR_TOKEN", "")


def make_handler(mig: Migrator, token: str):
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> bool:
            import hmac
            got = self.headers.get("X-Migrator-Token", "")
            return bool(token) and hmac.compare_digest(got, token)

        def log_message(self, *_):
            pass

        def do_GET(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/status":
                return self._send(200, mig.snapshot())
            if self.path == "/public-key":
                return self._send(200, {"public_key": mig.public_key()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                if self.path == "/preflight":
                    return self._send(200, mig.preflight(body["target"], body.get("accept_fingerprint")))
                if self.path == "/start":
                    mig.start()
                elif self.path == "/advance":
                    mig.advance(body.get("step", ""))
                elif self.path == "/retry":
                    mig.retry()
                elif self.path == "/abort":
                    return self._send(200, {**mig.abort(), "state": mig.snapshot()})
                else:
                    return self._send(404, {"error": "not found"})
                self._send(202, mig.snapshot())
            except (RuntimeError, KeyError) as e:
                self._send(409, {"error": str(e), "state": mig.snapshot()})
    return H


def main():
    dry = os.environ.get("MIGRATOR_DRY_RUN") == "1"
    mig = Migrator(run=simulated_run if dry else None)
    if dry:
        print("!! MIGRATOR_DRY_RUN=1 — rehearsal mode, no command is executed", flush=True)
    port = int(os.environ.get("MIGRATOR_PORT", "8790"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(mig, _token()))
    print(f"stoic-migrator listening on :{port} (root {ROOT})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
