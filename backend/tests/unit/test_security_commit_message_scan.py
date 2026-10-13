"""SECURITY 2026-10-13 — commit MESSAGES are scanned for credentials (commit e17db269 leaked a live Telegram bot
token in its message; the bot was hijacked). scripts/commit_message_secret_scan.py must catch env-style lines and
known token shapes, accept placeholders, and know the rotated fingerprints; CI runs it + gitleaks stdin."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import commit_message_secret_scan as cms  # noqa: E402

pytestmark = pytest.mark.unit

# synthetic shapes assembled at runtime so no scanner ever sees a token-shaped literal in this file
FAKE_TG = ":".join(["1234567890", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"[:35]])
FAKE_EMERGENT = "sk-emergent-" + "aB3dE6fG9hJ2kL5m"
FAKE_HIGH_ENTROPY = "Qz7#Lm2pX9vK4rT8wN1"


def _kinds(msg):
    return sorted((f["kind"], f["name"]) for f in cms.scan_message("deadbeefcafe", msg))


def test_env_style_credential_line_is_blocking_and_value_is_never_printed(capsys):
    msg = f"Telegram alerts are live.\n\nSECURITY_AGENT_TELEGRAM_BOT_TOKEN={FAKE_TG}\nSECURITY_AGENT_TELEGRAM_CHAT_ID=12345\n"
    found = cms.scan_message("deadbeefcafe", msg)
    assert any(f["kind"] == "telegram-bot-token" for f in found)
    assert all(FAKE_TG not in str(v) for f in found for v in f.values())


def test_known_token_shapes_are_caught_anywhere_in_prose():
    assert ("emergent-universal-key", "emergent-universal-key") in _kinds(f"The new key {FAKE_EMERGENT} is in .env now")
    assert any(k == "private-key-block" for k, _ in _kinds("-----BEGIN RSA PRIVATE KEY-----\nMIIE..."))
    assert any(k == "aws-access-key" for k, _ in _kinds("creds: " + "AKIA" + "ABCDEFGHIJKLMNOP"))


def test_credential_assignment_with_secret_name_and_real_looking_value():
    assert _kinds(f"- Temporary Password: {FAKE_HIGH_ENTROPY}") == [("credential-assignment", "Temporary Password")]
    assert _kinds(f"ADMIN_PASSWORD={FAKE_HIGH_ENTROPY}") == [("credential-assignment", "ADMIN_PASSWORD")]
    assert _kinds(f"  api_key: '{FAKE_HIGH_ENTROPY}'") == [("credential-assignment", "api_key")]


def test_placeholders_and_non_secret_prose_pass():
    clean = "\n".join([
        "SECURITY_AGENT_TELEGRAM_BOT_TOKEN=<set>",
        "ADMIN_PASSWORD=<rotated>",
        "STRIPE_API_KEY=${STRIPE_API_KEY}",
        "JWT_SECRET=********",
        "password: REDACTED",
        "Token: see backend/.env",
        "STOIC_READINESS_POLICY=demo_only",
        "- Added PRODUCTION_RETIRED_SECRETS listing STEP_UP_BYPASS_TOKEN, RATE_LIMIT_BYPASS_TOKEN",
        "release key pin: RELEASE_SIGNER_KEY_ID=rel-2026-10",
        "Emergent-Commit-Key: " + "0" * 64,
        "ea_binary_sha256=" + "c" * 64,
        "Fixed token refresh in api.js; the password reset flow now rate-limits",
    ])
    assert _kinds(clean) == []


def test_rotated_fingerprints_are_informational_and_history_is_clean():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "commit_message_secret_scan.py"), "--all"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "0 blocking finding(s)" in out.stdout
    assert "historical finding(s) match ROTATED credentials" in out.stdout      # e17db269 + 6086a7b0, hashes only


def test_message_file_mode_for_the_commit_msg_hook(tmp_path):
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(f"ship it\n\nRESEND_API_KEY={FAKE_HIGH_ENTROPY}\n", encoding="utf-8")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "commit_message_secret_scan.py"), "--message-file", str(p)],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 1 and "RESEND_API_KEY" in out.stdout and FAKE_HIGH_ENTROPY not in out.stdout
    p.write_text("ship it\n\nRESEND_API_KEY=<set>\n", encoding="utf-8")
    assert subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "commit_message_secret_scan.py"), "--message-file", str(p)],
                          capture_output=True, text=True, cwd=ROOT).returncode == 0


def test_ci_runs_gitleaks_and_the_scanner_on_commit_messages():
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    assert "Commit-MESSAGE secret scan" in ci
    assert "./gitleaks stdin --no-banner --redact --exit-code 1 -c .gitleaks-commit-messages.toml" in ci
    assert 'python3 scripts/commit_message_secret_scan.py --range "${RANGE}"' in ci
    assert "python3 scripts/commit_message_secret_scan.py --all" in ci
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  /tmp/gitleaks.tgz" in ci      # pinned binary
    assert os.path.exists(os.path.join(ROOT, ".gitleaks-commit-messages.toml"))
    assert os.access(os.path.join(ROOT, "scripts", "git-hooks", "commit-msg"), os.X_OK)


def test_prose_with_credential_words_but_code_or_nested_assignments_passes():
    assert _kinds("- Remaining operator Secrets for production: APP_ENV=production, ADMIN_MFA_ENFORCED=true") == []
    assert _kinds("- P3 auth: `auth.is_master_admin(email)` / `master_admin_email()` — exemption") == []
    assert _kinds("token refresh: api.refreshToken(session)") == []
    # …while an identifier-shaped password with Upper+lower+digit is still caught
    assert _kinds("Temporary password: Summer2026x") == [("credential-assignment", "Temporary password")]
