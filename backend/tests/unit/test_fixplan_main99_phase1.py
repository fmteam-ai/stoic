"""main99 review — Phase 1 (before day 1): SEC-001 step-up on trust-terminal, N99-4 secrets in
every backup, N99-5 one hash-key source."""
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_sec001_trust_terminal_requires_step_up():
    import step_up
    assert "terminal_trust" in step_up.STEP_UP_ACTIONS
    src = _src("backend", "routes", "account_routes.py")
    body = src.split("async def trust_terminal(")[1].split("\n@router")[0]
    assert 'require_step_up(db, user, request, "terminal_trust")' in body
    # the step-up happens BEFORE the installation / lease / verified_identity writes
    assert body.index("require_step_up") < body.index("verified_identity\": ") if "verified_identity\": " in body \
        else body.index("require_step_up") < body.index("installations")


def test_n995_key_source_conflict_detected_only_when_both_present_and_differ():
    import bridge_tokens as bt
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("k" * 40 + "\n")
    try:
        assert bt.key_source_conflict({"BRIDGE_TOKEN_HASH_KEY": "k" * 40, "BRIDGE_TOKEN_HASH_KEY_FILE": fh.name}) is None
        msg = bt.key_source_conflict({"BRIDGE_TOKEN_HASH_KEY": "x" * 40, "BRIDGE_TOKEN_HASH_KEY_FILE": fh.name})
        assert msg and "differs" in msg
        assert bt.key_source_conflict({"BRIDGE_TOKEN_HASH_KEY": "x" * 40}) is None
        assert bt.key_source_conflict({"BRIDGE_TOKEN_HASH_KEY_FILE": fh.name}) is None
        assert bt.key_source_conflict({"BRIDGE_TOKEN_HASH_KEY": "x" * 40, "BRIDGE_TOKEN_HASH_KEY_FILE": "/nonexistent"}) is None
    finally:
        os.unlink(fh.name)
    srv = _src("backend", "server.py")
    assert "production_key_violation() or key_source_conflict()" in srv


def test_n995_ensure_release_secrets_seeds_from_env_and_refuses_mismatch():
    lib = _src("deploy", "lib.sh")
    fn = lib.split("ensure_release_secrets() {")[1].split("\n}\n")[0]
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "secrets"))
        os.makedirs(os.path.join(d, "backend"))
        open(os.path.join(d, "backend", ".env"), "w").write("BRIDGE_TOKEN_HASH_KEY=" + "e" * 40 + "\n")
        script = "ensure_release_secrets() {" + fn + "\n}\nensure_release_secrets"
        r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert open(os.path.join(d, "secrets", "bridge_token_hash_key")).read().strip() == "e" * 40
        assert "seeded bridge_token_hash_key" in r.stdout
        open(os.path.join(d, "secrets", "bridge_token_hash_key"), "w").write("other\n")
        r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
        assert r.returncode == 1 and "differs between backend/.env" in r.stdout
    upd = _src("deploy", "update.sh")
    assert "_benvval BRIDGE_TOKEN_HASH_KEY" in upd and 'differs from secrets/bridge_token_hash_key' in upd


def test_n994_backup_encrypts_secrets_and_restore_puts_missing_files_back():
    bk = _src("deploy", "backup.sh")
    funcs = bk.split("_encrypt_args() {")[1].split("_write_manifest() {")[0]
    funcs = "_encrypt_args() {" + funcs
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "secrets"))
        os.makedirs(os.path.join(d, "backups"))
        open(os.path.join(d, "secrets", "bridge_token_hash_key"), "w").write("keyA\n")
        open(os.path.join(d, "secrets", "ledger_anchor_key"), "w").write("keyB\n")
        open(os.path.join(d, "pp"), "w").write("passphrase\n")
        env = {**os.environ, "BACKUP_DIR": "./backups", "BACKUP_PASSPHRASE_FILE": os.path.join(d, "pp")}
        # no passphrase ⇒ refuse (secrets would be missing from the backup)
        r = subprocess.run(["bash", "-c", funcs + "\n_backup_secrets x"], cwd=d, capture_output=True, text=True,
                           env={**os.environ, "BACKUP_DIR": "./backups"})
        assert r.returncode == 1 and "refusing" in r.stdout
        r = subprocess.run(["bash", "-c", funcs + "\n_backup_secrets 20260601-000000"], cwd=d, capture_output=True, text=True, env=env)
        assert r.returncode == 0 and "2 secret file(s) included" in r.stdout, r.stdout + r.stderr
        enc = os.path.join(d, "backups", "stoic-secrets-20260601-000000.tar.gz.enc")
        assert os.path.exists(enc) and b"keyA" not in open(enc, "rb").read()      # never plaintext
        os.unlink(os.path.join(d, "secrets", "ledger_anchor_key"))
        open(os.path.join(d, "secrets", "bridge_token_hash_key"), "w").write("current\n")
        open(os.path.join(d, "backups", "stoic-mongo-20260601-000000.archive.gz"), "w").write("")
        r = subprocess.run(["bash", "-c", funcs + "\n_restore_secrets backups/stoic-mongo-20260601-000000.archive.gz"],
                           cwd=d, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert open(os.path.join(d, "secrets", "ledger_anchor_key")).read().strip() == "keyB"      # missing → restored
        assert open(os.path.join(d, "secrets", "bridge_token_hash_key")).read().strip() == "current"  # existing → kept
        assert "differs from the backup copy" in r.stdout
    assert "_restore_secrets \"${FILE}\"" in bk and "stoic-secrets-*.tar.gz.enc" in bk
    assert "secrets/" in _src("docs", "DISASTER_RECOVERY.md") and "N99-4" in _src("docs", "DISASTER_RECOVERY.md")
