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


def _secure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def _write_private(path: str, text: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


_REDACT = re.compile(r"((?:password|passwd|token|secret|api[_-]?key|authorization)[=:\s\"']+|\s-p\s+[\"']?)([^\s\"']{4,})", re.I)


def redact(line: str) -> str:
    return _REDACT.sub(lambda m: m.group(1) + "***", line)


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
        d = os.path.dirname(self.state_file)
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        tmp = self.state_file + ".tmp"
        _write_private(tmp, json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_file)

    def wipe_key_material(self, reason: str):
        """P1-5/P2-3 — destroy the SSH key + pinned host keys once the
        migration is over (decommission or abort); state stays for audit
        with logs truncated."""
        for f in (KEY_FILE, KEY_FILE + ".pub", KNOWN_HOSTS, KNOWN_HOSTS + ".scan"):
            try:
                if os.path.exists(f):
                    _write_private(f, "\0" * os.path.getsize(f))
                    os.remove(f)
            except OSError:
                pass
        with self.lock:
            self.state["key_material_destroyed_at"] = now_iso()
            self.state["log"] = self.state["log"][-300:]
            self._save()
        self.log("cleanup", f"ssh key material destroyed ({reason})")

    def snapshot(self) -> dict:
        with self.lock:
            s = json.loads(json.dumps(self.state))
        s["log"] = s["log"][-400:]
        s["simulated"] = self._run is simulated_run
        return s

    def log(self, step: str, line: str):
        with self.lock:
            self.state["log"].append({"at": now_iso(), "step": step, "line": redact(line)[:2000]})
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
        _secure_dir()
        if not os.path.exists(KEY_FILE):
            self._run(f"ssh-keygen -q -t ed25519 -N '' -C stoic-migrator -f {shlex.quote(KEY_FILE)}",
                      "keys", 60)
        try:
            return open(KEY_FILE + ".pub").read().strip()
        except FileNotFoundError:
            return ""

    def scan_host(self, host: str, port: int) -> dict:
        """Phase 1 — observe the host keys WITHOUT authenticating. Nothing is
        trusted: keys go to a scratch file, fingerprints are returned for
        out-of-band comparison (provider console) and journaled."""
        scratch = KNOWN_HOSTS + ".scan"
        rc, out = self._run(f"ssh-keyscan -p {int(port)} -T 10 -t ed25519,ecdsa,rsa {shlex.quote(host)} 2>/dev/null",
                            "scan", 30)
        keys = [l for l in out.splitlines() if l and not l.startswith("#")]
        if not keys:
            raise RuntimeError(f"ssh-keyscan: no host key from {host}:{port} — is SSH reachable?")
        _secure_dir()
        _write_private(scratch, "\n".join(keys) + "\n")
        rc, fp = self._run(f"ssh-keygen -lf {shlex.quote(scratch)}", "scan", 30)
        fps = [{"fingerprint": l.split()[1], "type": l.split()[-1].strip("()")} for l in fp.splitlines() if len(l.split()) > 1]
        scan = {"host": host, "port": int(port), "fingerprints": fps, "keys": len(keys), "scanned_at": now_iso()}
        with self.lock:
            self.state["host_key_scan"] = scan
            self._save()
        self.log("scan", f"observed host keys for {host}:{port}: " + ", ".join(f["fingerprint"] for f in fps))
        return scan

    def trust_host(self, host: str, port: int, accept_fingerprint: str) -> dict:
        """Phase 2 — the admin confirmed ONE exact fingerprint out-of-band.
        Only now is known_hosts written (pinned; StrictHostKeyChecking=yes)."""
        scan = self.scan_host(host, port)                     # fresh scan: defeats key swap between phases
        fps = [f["fingerprint"] for f in scan["fingerprints"]]
        if not accept_fingerprint or accept_fingerprint not in fps:
            raise RuntimeError(f"host key fingerprint not confirmed: accepted={accept_fingerprint!r} observed={fps}")
        os.replace(KNOWN_HOSTS + ".scan", KNOWN_HOSTS)
        os.chmod(KNOWN_HOSTS, 0o600)
        trust = {"host": host, "port": int(port), "observed": fps, "accepted": accept_fingerprint, "at": now_iso()}
        with self.lock:
            self.state["host_key_trust"] = trust
            self._save()
        self.log("scan", f"host key TRUSTED by admin: {accept_fingerprint}")
        return trust

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
        if not accept_fingerprint:
            raise RuntimeError("out-of-band host key confirmation is mandatory — scan first, "
                               "compare with the provider console, then confirm the exact fingerprint")
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("a migration step is running")
            if self.state["status"] in ("awaiting",) and self.state["current_step"] not in (None, "preflight"):
                raise RuntimeError("a migration is in progress — abort it before starting a new preflight")
            prev_scan, prev_log = self.state.get("host_key_scan"), self.state["log"][-200:]
            self.state = _fresh_state()
            self.state["log"] = prev_log
            self.state["host_key_scan"] = prev_scan
            self.state["id"] = f"mig_{uuid.uuid4().hex[:10]}"
            self.state["target"] = {"host": target["host"], "user": target["user"],
                                    "port": int(target.get("port", 22)), "path": target["path"]}
            self.state["status"] = "running"
            self._save()
        self._mark("preflight", "running")
        try:
            trust = self.trust_host(target["host"], int(target.get("port", 22)), accept_fingerprint)
            facts = {"host_key": {"fingerprints": trust["observed"], "accepted": trust["accepted"]}}
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

    def _mongo_eval(self, js: str, step: str, remote: bool = False) -> str:
        cmd = "docker compose exec -T mongo sh -c " + shlex.quote(
            f"mongosh {MONGO_AUTH} --quiet \"$DB_NAME\" --eval {shlex.quote(js)}")
        return self.remote_cd(cmd, step, 120, check=False) if remote else self.sh(cmd, step, 120, check=False)

    def snapshot_expected_accounts(self) -> list:
        """Before freeze: the EXACT set of enabled accounts (id, verified
        broker identity, bridge token hash, EA build/policy) that must all
        report to the new host before decommission."""
        js = ("JSON.stringify(db.accounts.find({trading_enabled:true,status:{$ne:'deleted'}},"
              "{label:1,verified_identity:1,ea_version:1,policy_version:1,bridge_token:1}).toArray()"
              ".map(a=>({id:String(a._id),label:a.label,account_number:(a.verified_identity||{}).account_number||null,"
              "broker_server:(a.verified_identity||{}).broker_server||null,ea_version:a.ea_version||null,"
              "policy_version:a.policy_version||null,bridge_token_tail:a.bridge_token?String(a.bridge_token).slice(-6):null})))")
        out = self._mongo_eval(js, "freeze")
        line = next((l for l in out.splitlines() if l.strip().startswith("[")), "[]")
        try:
            return json.loads(line)
        except ValueError:
            return []

    def freeze(self):
        self._mark("freeze", "running")
        expected = self.snapshot_expected_accounts()
        with self.lock:
            self.state["expected_accounts"] = expected
            self._save()
        self.log("freeze", f"snapshotted {len(expected)} enabled account identities that must all reconnect on the new host")
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
        expected = self.state.get("expected_accounts") or []
        js = ("JSON.stringify(db.accounts.find({last_heartbeat:{$gt:'" + since + "'}},"
              "{label:1,verified_identity:1,ea_version:1,policy_version:1,bridge_token:1,trading_enabled:1}).toArray()"
              ".map(a=>({id:String(a._id),account_number:(a.verified_identity||{}).account_number||null,"
              "broker_server:(a.verified_identity||{}).broker_server||null,ea_version:a.ea_version||null,"
              "policy_version:a.policy_version||null,bridge_token_tail:a.bridge_token?String(a.bridge_token).slice(-6):null,"
              "trading_enabled:a.trading_enabled===true})))")
        out = self._mongo_eval(js, sid, remote=True)
        line = next((l for l in out.splitlines() if l.strip().startswith("[")), "[]")
        try:
            arrived = json.loads(line)
        except ValueError:
            arrived = []
        by_id = {a["id"]: a for a in arrived}
        accounts = []
        for e in expected:
            got = by_id.get(e["id"])
            ident_ok = bool(got) and got.get("account_number") == e.get("account_number") \
                and got.get("broker_server") == e.get("broker_server") \
                and got.get("bridge_token_tail") == e.get("bridge_token_tail")
            version_ok = bool(got) and (not e.get("ea_version") or got.get("ea_version") == e.get("ea_version")) \
                and (not e.get("policy_version") or got.get("policy_version") == e.get("policy_version"))
            accounts.append({**e, "heartbeat_on_target": bool(got), "identity_match": ident_ok,
                             "version_match": version_ok, "ok": bool(got) and ident_ok and version_ok,
                             "target_ea_version": (got or {}).get("ea_version")})
        unexpected = [a for a in arrived if a["id"] not in {e["id"] for e in expected} and a.get("trading_enabled")]
        res["expected_accounts"] = len(expected)
        res["accounts"] = accounts
        res["arrived"] = sum(1 for a in accounts if a["heartbeat_on_target"])
        res["unexpected_identities"] = unexpected
        res["ea_heartbeats_on_target"] = len(arrived)
        res["connected_accounts_at_source"] = src.get("connected_accounts")
        res["missing_account_ids"] = [a["id"] for a in accounts if not a["ok"]]
        res["ok"] = bool(expected) and all(a["ok"] for a in accounts) and not unexpected \
            or (not expected and res.get("public_health", True))
        res["checked_at"] = now_iso()
        self._mark(sid, "done" if res["ok"] else "pending", **res)
        with self.lock:
            self.state["facts"]["cutover"] = res
            self._save()
        return res

    def disable_missing_on_target(self) -> list:
        """Audited exception path — sets every expected account that did not
        reconnect to trading_enabled=false on the NEW host, then re-checks."""
        cut = self.state["facts"].get("cutover") or {}
        missing = list(cut.get("missing_account_ids") or [])
        if missing:
            ids = ",".join(f"ObjectId('{i}')" for i in missing if re.fullmatch(r"[0-9a-f]{24}", i))
            js = ("db.accounts.updateMany({_id:{$in:[" + ids + "]}},{$set:{trading_enabled:false,"
                  "migration_disabled_at:new Date().toISOString(),migration_disabled_reason:'no heartbeat on new host at cutover'}}).modifiedCount")
            out = self._mongo_eval(js, "cutover_check", remote=True)
            self.log("cutover_check", f"EXCEPTION: disabled {len(missing)} account(s) on the new host: {missing} → {out.strip()[-40:]}")
            with self.lock:
                self.state["facts"]["cutover_exception"] = {"disabled_account_ids": missing, "at": now_iso()}
                self.state["expected_accounts"] = [e for e in self.state.get("expected_accounts", []) if e["id"] not in missing]
                self._save()
        res = self.cutover_check()
        with self.lock:
            if self.state["status"] != "running":
                self.state["status"] = "awaiting"
                self.state["awaiting"] = "decommission" if res.get("ok") else "cutover_check"
                self._save()
        return missing

    def decommission(self):
        sid = "decommission"
        self._mark(sid, "running")
        self.sh("docker compose stop", sid, 600)
        _write_private(os.path.join(STATE_DIR, "DECOMMISSIONED"),
                       json.dumps({"at": now_iso(), "target": self.state["target"], "id": self.state["id"]}))
        self._mark(sid, "done")
        self.wipe_key_material("decommissioned")

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
            raise RuntimeError("cutover not confirmed — not every expected account identity has reconnected on the new host")
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
        self.wipe_key_material("aborted")
        return {"restarted_source": restarted}


# ── Rehearsal mode (MIGRATOR_DRY_RUN=1): no command touches anything ────────
_SIM = [("ssh-keyscan", "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISIMULATEDKEY"),
        ("ssh-keygen -lf", "256 SHA256:Simulated0Fingerprint0Rehearsal0Mode0000000 203.0.113.10 (ED25519)"),
        ("trading_enabled:true,status", '[{"id":"6a0000000000000000000001","label":"SIM-1","account_number":"111","broker_server":"Sim","ea_version":"1.56","policy_version":"v56.4","bridge_token_tail":"aaaaaa"},'
                                        '{"id":"6a0000000000000000000002","label":"SIM-2","account_number":"222","broker_server":"Sim","ea_version":"1.56","policy_version":"v56.4","bridge_token_tail":"bbbbbb"}]'),
        ("last_heartbeat:{$gt", lambda: '[{"id":"6a0000000000000000000001","account_number":"111","broker_server":"Sim","ea_version":"1.56","policy_version":"v56.4","bridge_token_tail":"aaaaaa","trading_enabled":true}'
                                        + (',{"id":"6a0000000000000000000002","account_number":"222","broker_server":"Sim","ea_version":"1.56","policy_version":"v56.4","bridge_token_tail":"bbbbbb","trading_enabled":true}' if time.time() % 60 > 20 else "") + "]"),
        ("updateMany", "1"),
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
    """Dedicated one-time migration token (secrets/migrator_token, minted by
    `make migrator-on`). NEVER the metrics token."""
    f = os.environ.get("MIGRATOR_TOKEN_FILE", "/run/secrets/migrator_token")
    if f.endswith("metrics_token"):
        raise SystemExit("refusing to run with METRICS_TOKEN as migration authority")
    try:
        return open(f).read().strip()
    except FileNotFoundError:
        return os.environ.get("MIGRATOR_TOKEN", "")


def _expired() -> bool:
    until = os.environ.get("MIGRATOR_ENABLED_UNTIL", "")
    if not until:
        return False
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return True


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
            if _expired():
                self._send(410, {"error": "migration window expired — run `make migrator-on` again"})
                return False
            got = self.headers.get("X-Migrator-Token", "")
            return bool(token) and hmac.compare_digest(got, token)

        def log_message(self, *_):
            pass

        def do_GET(self):
            if not self._auth():
                return None if _expired() else self._send(401, {"error": "unauthorized"})
            if self.path == "/status":
                return self._send(200, mig.snapshot())
            if self.path == "/public-key":
                return self._send(200, {"public_key": mig.public_key()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return None if _expired() else self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                if self.path == "/scan":
                    return self._send(200, mig.scan_host(body["host"], int(body.get("port", 22))))
                if self.path == "/preflight":
                    return self._send(200, mig.preflight(body["target"], body.get("accept_fingerprint")))
                if self.path == "/disable-missing":
                    return self._send(200, {"disabled": mig.disable_missing_on_target(), "state": mig.snapshot()})
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
