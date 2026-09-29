"""Signed install report + health watcher — exercised with a stubbed docker/curl."""
import base64
import json
import os
import shutil
import stat
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _stub(bin_dir, name, body):
    p = os.path.join(bin_dir, name)
    open(p, "w").write("#!/usr/bin/env bash\n" + body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "deploy").mkdir(parents=True)
    (proj / "backend").mkdir()
    for f in ("install_report.sh", "healthwatch.sh", "doctor.sh"):
        shutil.copy(os.path.join(ROOT, "deploy", f), proj / "deploy" / f)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "x").write_text("x")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"], cwd=proj, check=True)
    return proj


def test_install_report_is_signed_in_sidecar_and_verifies(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    priv_b64 = base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                  serialization.NoEncryption())).decode()
    pub_b64 = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)).decode()
    proj = _project(tmp_path)
    (proj / ".env").write_text("DOMAIN=trade.example.com\nSTOIC_IMAGE_DIGEST=sha256:abc\n")
    (proj / "backend" / ".env").write_text(f"APP_ENV=production\nRELEASE_PUBLIC_KEY_B64={pub_b64}\nADMIN_EMAIL=ops@example.com\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `docker compose exec -T signer python -c <code>` → run the code with the test key as the secret file
    (tmp_path / "signer_ed25519_key").write_text(priv_b64)
    _stub(bin_dir, "docker", f'''
code="${{@: -1}}"
code="${{code//\\/run\\/secrets\\/signer_ed25519_key/{tmp_path}/signer_ed25519_key}}"
exec python3 -c "$code"
''')
    _stub(bin_dir, "curl", 'echo "$@" >> "%s/curl.log"; exit 0\n' % tmp_path)     # e-mail/telegram calls are recorded
    # doctor would FAIL in the sandbox — stub it to a clean result
    (proj / "deploy" / "doctor.sh").write_text("#!/usr/bin/env bash\necho '  WARN  one warning'\nexit 0\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RESEND_API_KEY": "re_test"}
    r = subprocess.run(["bash", "deploy/install_report.sh"], cwd=proj, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "signed by the sidecar" in r.stdout and "e-mailed to ops@example.com" in r.stdout
    files = sorted((proj / "deploy" / "releases").glob("install-report-*.json"))
    assert len(files) == 1
    doc = json.load(open(files[0]))
    assert doc["report"]["domain"] == "trade.example.com" and doc["report"]["image_digest"] == "sha256:abc"
    assert doc["report"]["doctor"] == {"fail": 0, "warn": 1, "ok": True}
    key.public_key().verify(bytes.fromhex(doc["signature"]["sig_hex"]), doc["report_canonical"].encode())
    log = open(tmp_path / "curl.log").read()
    assert "api.resend.com/emails" in log and "attachments" in log
    assert priv_b64 not in log and priv_b64 not in open(files[0]).read()       # private key never leaves the sidecar


def test_healthwatch_alerts_on_transition_and_recovery_only(tmp_path):
    proj = _project(tmp_path)
    (proj / ".env").write_text("HEALTHWATCH_TELEGRAM_BOT_TOKEN=tok\nHEALTHWATCH_TELEGRAM_CHAT_ID=42\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", 'echo "$@" >> "%s/curl.log"; exit 0\n' % tmp_path)
    doctor = proj / "deploy" / "doctor.sh"
    state = tmp_path / "state"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HEALTHWATCH_STATE_DIR": str(state)}

    def run(result_ok):
        doctor.write_text("#!/usr/bin/env bash\n" + ("echo '  WARN  w'\nexit 0\n" if result_ok else "echo '  FAIL  api down'\nexit 1\n"))
        return subprocess.run(["bash", "deploy/healthwatch.sh", "run"], cwd=proj, env=env, capture_output=True, text=True).stdout

    def alerts():
        return open(tmp_path / "curl.log").read().count("sendMessage") if (tmp_path / "curl.log").exists() else 0
    assert "healthwatch: ok" in run(True) and alerts() == 0          # healthy: silent
    assert "alert sent" in run(False) and alerts() == 1              # ok → fail: one alert
    run(False)
    assert alerts() == 1                                             # still failing within remind window: silent
    assert "recovery alert sent" in run(True) and alerts() == 2      # fail → ok: recovery alert
    run(True)
    assert alerts() == 2
    assert (state / "healthwatch.state").read_text().startswith("ok ")


def test_bootstrap_wires_timer_and_report():
    b = open(os.path.join(ROOT, "deploy", "bootstrap.sh")).read()
    for needle in ("deploy/healthwatch.sh install", "deploy/install_report.sh", "--report-email", "--telegram",
                   "HEALTHWATCH_TELEGRAM_BOT_TOKEN", "INSTALL_REPORT_EMAIL"):
        assert needle in b, needle
    hw = open(os.path.join(ROOT, "deploy", "healthwatch.sh")).read()
    assert "OnCalendar=${every}" in hw and "systemctl enable --now stoic-healthwatch.timer" in hw
    for f in ("healthwatch.sh", "install_report.sh"):
        subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", f)], check=True)
