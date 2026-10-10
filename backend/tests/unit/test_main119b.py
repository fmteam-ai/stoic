"""main119 phase B — S-1 release key rotation (transition/revoked key ids, pin scripts), M119-4 restore into a
brand-new auth-enabled mongod (admin.* excluded), M119-5 account deletion hygiene (pairing tokens + alerts, orphan
sweep), M119-6 migrate-offline.sh contract, OpenVZ host checks."""
import asyncio
import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402
import release_signing as rs  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
V1, V2 = "stoic-release-ed25519-v1", "stoic-release-ed25519-v2"


def _keypair():
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return k, pub


def _fp(pub_b64):
    import hashlib
    return "SHA256:" + hashlib.sha256(base64.b64decode(pub_b64)).hexdigest()


def _sign(k, data: bytes, purpose="ea-release"):
    return k.sign(rs.domain_bytes(purpose, data)).hex()


def _stub(bin_dir, name, body):
    p = os.path.join(bin_dir, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------- S-1 · release_signing
def test_transition_and_revoked_release_key_ids(monkeypatch):
    k1, pub1 = _keypair(); k2, pub2 = _keypair()
    env = {"RELEASE_SIGNER": "external", "RELEASE_SIGNER_KEY_ID": V2, "RELEASE_PUBLIC_KEY_B64": pub2,
           "RELEASE_ACCEPTED_KEY_IDS": f"{V1}", "RELEASE_TRANSITION_PUBLIC_KEYS": f"{V1}={pub1}"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RELEASE_REVOKED_KEY_IDS", raising=False)
    assert rs.accepted_release_key_ids() == {V1, V2}
    assert rs.key_id_accepted(V1) and rs.key_id_accepted(V2) and not rs.key_id_accepted("stoic-release-ed25519-v0")
    assert rs.key_id_accepted(V1, purpose="acceptance-bundle") is False      # runtime purposes: current key only
    data = b"ea-record"
    assert rs.verify_hex(data, _sign(k1, data), rs.release_public_key_for(V1), purpose="ea-release")
    assert rs.verify_hex(data, _sign(k2, data), rs.release_public_key_for(V2), purpose="ea-release")
    assert not rs.verify_hex(data, _sign(k1, data), rs.release_public_key_for(V2), purpose="ea-release")   # wrong key for the id
    st = rs.release_key_rotation_status()
    assert st["in_transition"] and st["ok"] and V1 in st["warning"] and st["transition_key_ids"] == [V1]
    # finish the rotation: v1 revoked → never accepted, no warning, status clean
    monkeypatch.setenv("RELEASE_REVOKED_KEY_IDS", V1)
    assert not rs.key_id_accepted(V1) and rs.accepted_release_key_ids() == {V2}
    assert not rs.verify_hex(data, _sign(k1, data), rs.release_public_key_for(V1), purpose="ea-release")
    st = rs.release_key_rotation_status()
    assert not st["in_transition"] and st["ok"] and st["warning"] is None and st["revoked_key_ids"] == [V1]
    # inconsistent sets block: transition id without a public key / current id revoked
    monkeypatch.setenv("RELEASE_REVOKED_KEY_IDS", "")
    monkeypatch.setenv("RELEASE_TRANSITION_PUBLIC_KEYS", "")
    assert not rs.release_key_rotation_status()["ok"]
    monkeypatch.setenv("RELEASE_REVOKED_KEY_IDS", V2)
    assert not rs.release_key_rotation_status()["ok"] and not rs.key_id_accepted(V2)


def test_verify_ea_release_check_entry_honours_transition_and_revocation(monkeypatch, tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import verify_ea_release as vr
    monkeypatch.setattr(vr, "MQ5", str(tmp_path / "missing.mq5"))
    k1, pub1 = _keypair(); _k2, pub2 = _keypair()
    monkeypatch.setenv("RELEASE_SIGNER", "external"); monkeypatch.setenv("RELEASE_SIGNER_KEY_ID", V2)
    monkeypatch.setenv("RELEASE_PUBLIC_KEY_B64", pub2); monkeypatch.setenv("RELEASE_ACCEPTED_KEY_IDS", V1)
    monkeypatch.setenv("RELEASE_TRANSITION_PUBLIC_KEYS", f"{V1}={pub1}"); monkeypatch.delenv("RELEASE_REVOKED_KEY_IDS", raising=False)
    ea = {"version": "1.62", "ex5_sha256": "a" * 64, "compile_log": {"errors": 0}, "compiled_by": "github-actions",
          "signature": {"key_id": V1}}
    ea["signature"]["sig_hex"] = _sign(k1, vr._canonical_payload(ea, V1))
    assert vr.check_entry(ea) == []                                      # v1-signed record still verifies in transition
    monkeypatch.setenv("RELEASE_REVOKED_KEY_IDS", V1)
    fails = vr.check_entry(ea)
    assert fails and "not accepted" in fails[0] and "RELEASE_KEY_ROTATION" in fails[0]


def test_readiness_release_key_rotation_check_wired():
    src = open(os.path.join(ROOT, "backend", "routes", "ops_routes.py")).read()
    assert 'checks["release_key_rotation"]' in src and "release_key_rotation_status" in src
    tpl = open(os.path.join(ROOT, "deploy", "env", "backend.env.example")).read()
    for k in ("RELEASE_ACCEPTED_KEY_IDS", "RELEASE_TRANSITION_PUBLIC_KEYS", "RELEASE_REVOKED_KEY_IDS"):
        assert f"\n{k}=" in tpl


# ---------------------------------------------------------------- S-1 · preflight pin + rotate-release-pin.sh
def _tmp_root(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(os.path.join(ROOT, "deploy"), root / "deploy", ignore=shutil.ignore_patterns("state", "releases", "*.log"))
    (root / "release").mkdir(); (root / "backend").mkdir(); (root / "secrets").mkdir()
    (root / "deploy" / "state").mkdir()
    (root / ".env").write_text("")
    return root


def _pin_world(tmp_path, serve_kid, serve_pub):
    root = _tmp_root(tmp_path)
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _stub(bin_dir, "curl", "#!/usr/bin/env bash\n" + f"printf '%s' '{json.dumps({'key_id': serve_kid, 'public_key_b64': serve_pub})}'\n")
    _stub(bin_dir, "id", "#!/usr/bin/env bash\necho 0\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PREFLIGHT_YES="1", STOIC_REPAIR_JOURNAL=str(tmp_path / "j.jsonl"))
    return root, env


def _run_in(root, env, script):
    return subprocess.run(["bash", "-c", f"set -u; cd '{root}'; . deploy/lib.sh; . deploy/preflight.sh\n{script}"],
                          capture_output=True, text=True, env=env, timeout=120)


def test_preflight_refuses_revoked_key_and_warns_on_transition_pin(tmp_path):
    _k1, pub1 = _keypair(); _k2, pub2 = _keypair()
    root, env = _pin_world(tmp_path, V1, pub1)
    (root / "release" / "release_key.fingerprint").write_text(f"# hdr\n{V2} {_fp(pub2)} current\n{V1} {_fp(pub1)} revoked\n")
    (root / "backend" / ".env").write_text(f"RELEASE_SIGNER_KEY_ID={V1}\nRELEASE_PUBLIC_KEY_B64=\n")
    r = _run_in(root, env, "ensure_release_public_key_pin; echo RC=$?")
    assert "RC=0" in r.stdout and "REVOKED" in r.stdout and "NOT pinning" in r.stdout, r.stdout + r.stderr
    assert "RELEASE_PUBLIC_KEY_B64=\n" in (root / "backend" / ".env").read_text()                  # nothing pinned
    r = _run_in(root, env, f"release_key_status {V1}; release_key_status {V2}; current_release_key_id")
    assert r.stdout.split() == ["revoked", "current", V2]
    # a host still pinned to a transition key is told to re-pin (pin itself stays — never silently rewritten)
    (root / "release" / "release_key.fingerprint").write_text(f"{V2} {_fp(pub2)} current\n{V1} {_fp(pub1)} transition\n")
    (root / "backend" / ".env").write_text(f"RELEASE_SIGNER_KEY_ID={V1}\nRELEASE_PUBLIC_KEY_B64={pub1}\n")
    r = _run_in(root, env, "ensure_release_public_key_pin; echo RC=$?")
    assert "TRANSITION key" in r.stdout and f"rotate-release-pin.sh {V2}" in r.stdout and "matches the committed fingerprint" in r.stdout
    assert f"RELEASE_PUBLIC_KEY_B64={pub1}" in (root / "backend" / ".env").read_text()


def test_rotate_release_pin_script_pins_new_key_keeps_transition_then_revokes(tmp_path):
    _k1, pub1 = _keypair(); _k2, pub2 = _keypair()
    root, env = _pin_world(tmp_path, V2, pub2)
    (root / "release" / "release_key.fingerprint").write_text(f"{V2} {_fp(pub2)} current\n{V1} {_fp(pub1)} transition\n")
    (root / "backend" / ".env").write_text(f"APP_ENV=production\nRELEASE_SIGNER_KEY_ID={V1}\nRELEASE_PUBLIC_KEY_B64={pub1}\nBUNDLE_PUBLIC_KEY_B64=BUNDLE==\n")
    r = subprocess.run(["bash", "deploy/rotate-release-pin.sh", V2, "--no-restart"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    envf = (root / "backend" / ".env").read_text()
    assert f"RELEASE_SIGNER_KEY_ID={V2}\n" in envf and f"RELEASE_PUBLIC_KEY_B64={pub2}\n" in envf
    assert f"RELEASE_ACCEPTED_KEY_IDS={V1}\n" in envf and f"RELEASE_TRANSITION_PUBLIC_KEYS={V1}={pub1}\n" in envf
    assert "BUNDLE_PUBLIC_KEY_B64=BUNDLE==" in envf                      # runtime key untouched
    assert "kept as a TRANSITION key" in r.stdout and "release-key-repinned" in (root / "deploy" / "releases.log").read_text()
    # idempotent
    r = subprocess.run(["bash", "deploy/rotate-release-pin.sh", V2, "--no-restart"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "already the pinned key id" in r.stdout
    # refuses to revoke the current key; revokes the old one
    r = subprocess.run(["bash", "deploy/rotate-release-pin.sh", "--revoke", V2, "--no-restart"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "CURRENT key id" in r.stdout
    r = subprocess.run(["bash", "deploy/rotate-release-pin.sh", "--revoke", V1, "--no-restart"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    envf = (root / "backend" / ".env").read_text()
    assert f"RELEASE_REVOKED_KEY_IDS={V1}\n" in envf and "RELEASE_ACCEPTED_KEY_IDS=\n" in envf and "RELEASE_TRANSITION_PUBLIC_KEYS=\n" in envf
    # repo marks a different key current → refuse
    (root / "release" / "release_key.fingerprint").write_text(f"stoic-release-ed25519-v3 {_fp(pub1)} current\n")
    r = subprocess.run(["bash", "deploy/rotate-release-pin.sh", V2, "--no-restart"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "repo marks" in r.stdout


def test_rotate_fly_signer_key_script_contract():
    p = os.path.join(ROOT, "deploy", "signer", "rotate_fly_signer_key.sh")
    assert subprocess.run(["bash", "-n", p]).returncode == 0
    s = open(p).read()
    assert "SIGNER_KEY_ID=" in s and "a rotation needs a NEW key id" in s and "release_key.fingerprint" in s
    assert "rotate-release-pin.sh" in s and "--revoke" in s
    app = open(os.path.join(ROOT, "deploy", "signer", "app.py")).read()
    assert 'os.environ.get("SIGNER_KEY_ID")' in app                      # the signer honours the rotated key id
    assert os.path.exists(os.path.join(ROOT, "docs", "RELEASE_KEY_ROTATION.md"))


# ---------------------------------------------------------------- M119-5 · account deletion hygiene
def _sync(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_cleanup_deleted_account_removes_tokens_and_closes_alerts():
    from account_cleanup import cleanup_deleted_account, purge_orphan_pairing_tokens
    db = FakeDb()
    gone, alive = str(ObjectId()), ObjectId()
    db.accounts.rows.append({"_id": alive, "label": "live"})
    db.pairing_tokens.rows += [{"token": "t1", "account_id": gone}, {"token": "t2", "account_id": gone, "consumed_at": "x"},
                               {"token": "t3", "account_id": str(alive)}]
    db.ops_alerts.rows += [{"_id": 1, "kind": "pairing_no_heartbeat", "dedup_key": f"pairing_heartbeat:{gone}", "acked_at": None, "occurrences": 1564},
                           {"_id": 2, "kind": "ea_heartbeat_stale", "dedup_key": "hb:x", "meta": {"account_id": gone}, "acked_at": None},
                           {"_id": 3, "kind": "pairing_no_heartbeat", "dedup_key": f"pairing_heartbeat:{alive}", "acked_at": None}]
    out = _sync(cleanup_deleted_account(db, gone))
    assert out["pairing_tokens_removed"] == 2 and out["alerts_closed"] == 2
    assert [t["token"] for t in db.pairing_tokens.rows] == ["t3"]
    closed = [a for a in db.ops_alerts.rows if a.get("acked_at")]
    assert {a["_id"] for a in closed} == {1, 2} and all(a["acked_by"] == "system:account-deleted" and a["auto_resolved"] for a in closed)
    assert db.ops_alerts.rows[2]["acked_at"] is None                                   # the live account's alert is untouched
    assert _sync(cleanup_deleted_account(db, gone)) == {"account_id": gone, "pairing_tokens_removed": 0, "alerts_closed": 0}   # idempotent
    # orphan sweep: a token whose account vanished (deleted before this release) is purged with its alert
    db.pairing_tokens.rows.append({"token": "t4", "account_id": gone})
    db.ops_alerts.rows.append({"_id": 4, "kind": "pairing_no_heartbeat", "dedup_key": f"pairing_heartbeat:{gone}", "acked_at": None})
    swept = _sync(purge_orphan_pairing_tokens(db))
    assert [s["account_id"] for s in swept] == [gone] and swept[0]["pairing_tokens_removed"] == 1 and swept[0]["alerts_closed"] == 1
    assert [t["token"] for t in db.pairing_tokens.rows] == ["t3"]
    assert db.ops_alerts.rows[3]["acked_by"] == "system:orphan-pairing-token"


def test_pairing_evaluator_ignores_orphan_tokens(monkeypatch):
    import pairing_alerts as pa
    db = FakeDb()
    gone = str(ObjectId())
    db.pairing_tokens.rows.append({"token": "t", "account_id": gone})
    db.ops_alerts.rows.append({"kind": pa.KIND, "dedup_key": pa.dedup_key(gone), "acked_at": None})
    raised, notified = [], []

    async def raise_alert(db_, *a, **kw):
        raised.append(a); return "id"

    async def notify(text):
        notified.append(text)

    monkeypatch.setattr("install_progress.webrequest_url", lambda: "https://x", raising=False)
    active, n = _sync(pa.evaluate(db, datetime.now(timezone.utc), raise_alert=raise_alert, notify=notify))
    assert active == set() and n == 0 and raised == [] and notified == []        # no alert, no recovery push
    assert db.pairing_tokens.rows == [] and db.ops_alerts.rows[0]["acked_by"] == "system:orphan-pairing-token"


def test_delete_routes_call_cleanup():
    for f in ("account_routes.py", "crypto_routes.py"):
        src = open(os.path.join(ROOT, "backend", "routes", f)).read()
        assert "cleanup_deleted_account" in src


# ---------------------------------------------------------------- M119-4 · restore into a brand-new mongo (real mongod)
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _mongosh(port, ev, user=None, pwd=None, authdb="admin", db="admin"):
    cmd = ["mongosh", "--quiet", "--port", str(port)]
    if user:
        cmd += ["-u", user, "-p", pwd, "--authenticationDatabase", authdb]
    cmd += [db, "--eval", ev]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


class _Mongod:
    def __init__(self, tmp, name):
        self.port = _free_port(); self.path = tmp / name; self.path.mkdir()
        self.proc = subprocess.Popen(["mongod", "--auth", "--dbpath", str(self.path), "--port", str(self.port), "--bind_ip", "127.0.0.1",
                                      "--logpath", str(tmp / f"{name}.log")])
        for _ in range(60):
            if _mongosh(self.port, "db.runCommand({ping:1}).ok").returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("mongod did not start: " + (tmp / f"{name}.log").read_text()[-2000:])

    def init_users(self):   # what the compose mongo-init does on a fresh volume: root (localhost exception) + app user
        assert _mongosh(self.port, "db.createUser({user:'root',pwd:'rootpw',roles:['root']})").returncode == 0
        r = _mongosh(self.port, "db.createUser({user:'stoic_app',pwd:'apppw',roles:[{role:'readWrite',db:'stoicdb'}]})",
                     "root", "rootpw", db="stoicdb")
        assert r.returncode == 0, r.stdout + r.stderr

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.mark.skipif(not (shutil.which("mongod") and shutil.which("mongorestore") and shutil.which("mongosh")), reason="mongod/mongorestore/mongosh not installed")
def test_restore_into_brand_new_mongo_volume_keeps_indexes_and_app_user(tmp_path):
    src = _Mongod(tmp_path, "src")
    try:
        src.init_users()
        r = _mongosh(src.port, "db.trades.insertMany([{a:1},{a:2},{a:3}]); db.trades.createIndex({a:1},{name:'a_idx'}); "
                               "db.accounts.insertOne({label:'x'}); db.accounts.createIndex({label:1},{unique:true,name:'label_u'}); print('seeded')",
                     "root", "rootpw", db="stoicdb")
        assert "seeded" in r.stdout, r.stdout + r.stderr
        archive = tmp_path / "stoic-mongo-20260101-000000.archive.gz"
        with open(archive, "wb") as fh:
            d = subprocess.run(["mongodump", "--port", str(src.port), "-u", "root", "-p", "rootpw", "--authenticationDatabase", "admin",
                                "--archive", "--gzip"], stdout=fh, stderr=subprocess.PIPE, timeout=120)
        assert d.returncode == 0, d.stderr.decode()
    finally:
        src.stop()
    # the archive carries admin.system.users of the SOURCE; the target is a brand-new volume with its own users
    dst = _Mongod(tmp_path, "dst")
    try:
        dst.init_users()
        root = _tmp_root(tmp_path)
        (root / "backend" / ".env").write_text("APP_ENV=production\n")
        sec = tmp_path / "secrets"; sec.mkdir(); (sec / "mongo_app_password").write_text("apppw\n")
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        # `docker compose exec -T mongo sh -c "<cmd>"` → run <cmd> HERE against the fresh mongod, with the container's env
        _stub(bin_dir, "docker", "#!/usr/bin/env bash\n" + f"""
echo "docker $*" >> "{tmp_path}/docker.log"
case "$1 $2 $3" in
  "compose exec -T") shift 4; cmd="$3"
     cmd="${{cmd//mongorestore /mongorestore --port {dst.port} }}"; cmd="${{cmd//mongosh /mongosh --port {dst.port} }}"
     cmd="${{cmd//\\/run\\/secrets\\//{sec}/}}"
     MONGO_INITDB_ROOT_USERNAME=root MONGO_INITDB_ROOT_PASSWORD=rootpw MONGO_APP_USER=stoic_app DB_NAME=stoicdb sh -c "$cmd"; exit $? ;;
  *) exit 0 ;;
esac
""")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RESTORE_NO_START="1")
        r = subprocess.run(["bash", "deploy/backup.sh", "restore", str(archive)], cwd=root, env=env, capture_output=True, text=True, timeout=240)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "--nsExclude admin.*" in (tmp_path / "docker.log").read_text()
        assert "app user (mongo-init.js + secrets/mongo_app_password) authenticates" in r.stdout
        assert "4/5 skipped (RESTORE_NO_START=1)" in r.stdout
        # data + indexes restored, the TARGET's own users survived (admin.* excluded), app user works
        r = _mongosh(dst.port, "print(db.trades.countDocuments({}) + ' ' + db.trades.getIndexes().map(i=>i.name).sort().join(',') + ' ' + db.accounts.getIndexes().map(i=>i.name).sort().join(','))",
                     "stoic_app", "apppw", authdb="stoicdb", db="stoicdb")
        assert r.stdout.strip().endswith("3 _id_,a_idx _id_,label_u"), r.stdout + r.stderr
        r = _mongosh(dst.port, "print(db.getSiblingDB('admin').system.users.find().toArray().map(u=>u.user).sort().join(','))", "root", "rootpw")
        assert r.stdout.strip() == "root,stoic_app"
    finally:
        dst.stop()


# ---------------------------------------------------------------- M119-6 · migrate-offline.sh · OpenVZ
def test_migrate_offline_script_contract_and_dry_run(tmp_path):
    p = os.path.join(ROOT, "deploy", "migrate-offline.sh")
    assert subprocess.run(["bash", "-n", p]).returncode == 0
    s = open(p).read()
    for needle in ("rsync -aHAX --delete", "RESTORE_NO_START=1 bash deploy/backup.sh restore", "compose_up_guarded", "wait_api_health",
                   "already runs a compose stack", "BACKUP_PASSPHRASE_FILE"):
        assert needle in s, needle
    assert "docker volume" not in s                                                   # volumes never copied/touched
    root = _tmp_root(tmp_path)
    r = subprocess.run(["bash", "deploy/migrate-offline.sh"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 64 and "usage:" in r.stdout
    r = subprocess.run(["bash", "deploy/migrate-offline.sh", "restore", "--dry-run"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 1 and "secrets/ missing" in r.stdout                       # refuses a dir that was not pushed
    (root / "backend" / ".env").write_text("APP_ENV=production\n"); (root / "backups").mkdir()
    for s_ in ("mongo_root_password", "mongo_app_password"):
        (root / "secrets" / s_).write_text("x\n")
    r = subprocess.run(["bash", "deploy/migrate-offline.sh", "restore", "--dry-run"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 1 and "no backup archive" in r.stdout
    (root / "backups" / "stoic-mongo-20260101-000000.archive.gz").write_text("")
    r = subprocess.run(["bash", "deploy/migrate-offline.sh", "restore", "--dry-run", "--yes"], cwd=root, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    for step in ("[dry-run] preflight_host", "[dry-run] provision_images", "[dry-run] docker compose up -d --no-deps mongo",
                 "[dry-run] env RESTORE_NO_START=1 bash deploy/backup.sh restore", "[dry-run] compose_up_guarded", "Before cutover"):
        assert step in r.stdout, step
    assert "Offline path" in open(os.path.join(ROOT, "docs", "HOST_MIGRATION.md")).read()


def test_openvz_host_checks_are_pass():
    doc = open(os.path.join(ROOT, "deploy", "doctor.sh")).read()
    assert "host-managed (OpenVZ" in doc and "overlayfs (containerd snapshotter" in doc and 'hdr "host' in doc
    boot = open(os.path.join(ROOT, "deploy", "bootstrap.sh")).read()
    assert "OpenVZ container" in boot and "systemd-detect-virt" in boot
    for f in ("doctor.sh", "bootstrap.sh", "backup.sh", "rotate-release-pin.sh", "preflight.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", f)]).returncode == 0, f
