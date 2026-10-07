"""main105 review:
N105-1 the previous-record chain survives a third release (verify_ea_release keeps previous.previous in the copy);
N105-2 the Fly signer script is init-only (refuses an existing key) and never puts the release token on the API host;
N105-3 accounts-count explanation + live counts in the Inventory panel; N105-4 installation id stored in secrets/
(backed up) and reconciled with backend/.env; N105-5 installer: stale .ex5 removed, compile trusted only on a
0-error log, UTF-8 BOM, -TerminalId hint; N105-6 progress panel polish (spare token, no flash, hidden tabs, indexes)."""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8-sig").read()


def _local_signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "RELEASE_SIGNER_DEFERRED": "false",
            "ED25519_SIGNING_KEY_B64": priv, "RELEASE_PUBLIC_KEY_B64": pub, "GITHUB_ACTIONS": "true", "GITHUB_SHA": "c" * 40}


# ── N105-1 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_1_three_release_chain_keeps_previous_verifiable():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import verify_ea_release as ver
    import ea_capabilities as ec
    import release_signing as rs
    d = tempfile.mkdtemp()
    log = os.path.join(d, "c.log")
    open(log, "w", encoding="utf-16").write("MetaEditor 5.00 build 4500\nResult: 0 errors, 0 warnings\n")
    with patch.dict(os.environ, _local_signer_env()), \
            patch.object(ver, "HASHES", os.path.join(d, "RELEASE_HASHES.json")), \
            patch.object(ver, "EA_RELEASE", os.path.join(d, "release", "ea_release.json")), \
            patch.object(ver, "RC_LOCK", os.path.join(d, "rc_lock.json")):
        json.dump({"ea": {}}, open(ver.HASHES, "w"))
        records = []
        for i, v in enumerate(("1.58", "1.59", "1.60")):
            ex5 = os.path.join(d, f"{v}.ex5")
            open(ex5, "wb").write(f"EX5-{v}".encode())
            with patch.object(ver, "mq5_property_version", lambda path=None, v=v: v):
                ver.record(types.SimpleNamespace(compile_log=log, ex5=ex5, metaeditor_version=None, windows_build="w",
                                                 mt5_build=None, source_commit=None, compiled_by=None, sign=True))
            records.append(json.load(open(ver.HASHES))["ea"])
        r3 = records[2]
        assert r3["version"] == "1.60" and r3["previous"]["version"] == "1.59"
        assert r3["previous"]["previous"] == {"version": "1.58", "ex5_sha256": records[0]["ex5_sha256"]}   # pointer kept
        assert "signature" in r3["previous"] and "signature" not in r3["previous"]["previous"]           # one hop only
        # the current record verifies, AND the copied previous verifies on its OWN signature (N104-6 payload)
        assert rs.verify_hex(ec._canonical_payload(r3), r3["signature"]["sig_hex"], purpose="ea-release")
        assert rs.verify_hex(ec._canonical_payload(r3["previous"]), r3["previous"]["signature"]["sig_hex"], purpose="ea-release")
        assert ver._canonical_payload(r3["previous"]) == ec._canonical_payload(r3["previous"])
        # the second release's previous (1.58, no previous of its own) verifies too
        r2 = records[1]
        assert rs.verify_hex(ec._canonical_payload(r2["previous"]), r2["previous"]["signature"]["sig_hex"], purpose="ea-release")


# ── N105-2 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_2_fly_signer_script_is_init_only_and_keeps_release_token_off_the_api():
    assert not os.path.exists(os.path.join(ROOT, "deploy", "signer", "deploy_fly.sh"))
    s = _read("deploy/signer/init_fly_signer.sh")
    assert "grep -q 'ED25519_SIGNING_KEY_B64'" in s and "exit 3" in s and "refusing to mint a new key" in s
    assert s.index("ED25519_SIGNING_KEY_B64'; then") < s.index("Ed25519PrivateKey.generate()")       # refuse BEFORE minting
    api_block = s[s.index("API host backend/.env"):s.index("DELETE (or leave empty) on the API host")]
    assert "RELEASE_SIGNER_TOKEN" not in api_block                                                 # N101-5
    assert "RELEASE_SIGNER_TOKEN" in s[s.index("DELETE (or leave empty) on the API host"):]
    assert "flyctl deploy -a $APP --ha=false" in s
    for rel in ("docs/RELEASE_SIGNER.md", "docs/PRODUCTION_DEPLOY_CHECKLIST.md", "deploy/signer/fly.toml", "backend/deploy_preflight.py"):
        assert "deploy_fly.sh" not in _read(rel), rel
    assert "flyctl deploy -a stoic-signer" in _read("docs/RELEASE_SIGNER.md")


# ── N105-3 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_3_accounts_count_explained_and_live_counts_in_panel():
    doc = _read("docs/DEPLOYMENT.md")
    assert "**every** account row in the database" in doc and "disabled and paper accounts included" in doc
    assert "`accounts`/`enabled` = 2/2" not in doc
    panel = _read("frontend/src/components/admin/InventoryGoLivePanel.jsx")
    assert 'data-testid="expectation-live-counts"' in panel and "{c.configured ?? " in panel and "{c.bots_enabled ?? " in panel


# ── N105-4 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_4_installation_id_lives_in_secrets_and_is_reconciled(tmp_path):
    lib = os.path.join(ROOT, "deploy", "lib.sh")
    (tmp_path / "backend").mkdir()
    (tmp_path / "secrets").mkdir()

    def run():
        return subprocess.run(["bash", "-c", f". {lib}; ensure_installation_id"], cwd=tmp_path, capture_output=True, text=True)

    (tmp_path / "backend" / ".env").write_text("APP_ENV=production\n")
    r = run()
    assert r.returncode == 0 and "secrets/installation_id" in r.stdout
    sec = (tmp_path / "secrets" / "installation_id").read_text().strip()
    assert re.fullmatch(r"stoic-[0-9a-f]{16}", sec) and f"STOIC_INSTALLATION_ID={sec}\n" in (tmp_path / "backend" / ".env").read_text()
    assert oct(os.stat(tmp_path / "secrets" / "installation_id").st_mode & 0o777) == "0o600"
    # rebuilt host: secrets/ restored from the encrypted backup, fresh backend/.env → the SAME id comes back
    (tmp_path / "backend" / ".env").write_text("APP_ENV=production\n")
    assert run().returncode == 0 and f"STOIC_INSTALLATION_ID={sec}" in (tmp_path / "backend" / ".env").read_text()
    # legacy host (id only in backend/.env, main104) → copied into secrets/, value kept
    (tmp_path / "secrets" / "installation_id").unlink()
    (tmp_path / "backend" / ".env").write_text("STOIC_INSTALLATION_ID=stoic-legacy\n")
    assert run().returncode == 0 and (tmp_path / "secrets" / "installation_id").read_text().strip() == "stoic-legacy"
    # two different ids → hard stop (the signed policies name exactly one)
    (tmp_path / "backend" / ".env").write_text("STOIC_INSTALLATION_ID=stoic-other\n")
    r = run()
    assert r.returncode == 1 and "differs" in r.stdout
    assert "ensure_installation_id || gate_refused" in _read("deploy/update.sh") and "ensure_installation_id || exit 1" in _read("deploy/install.sh")
    assert "tar -czf - secrets" in _read("deploy/backup.sh")      # secrets/ (now incl. installation_id) is in the encrypted backup


# ── N105-5 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_5_installer_compile_verdict_bom_and_terminalid_hint():
    raw = open(os.path.join(ROOT, "backend", "static", "STOIC-Installer.ps1"), "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")                                             # UTF-8 BOM for Windows PowerShell 5.1
    ps = raw.decode("utf-8-sig")
    assert "function Test-StoicCompileLog" in ps and "-Encoding Unicode -Raw" in ps
    body = ps[ps.index("function Install-Stoic"):]
    assert body.index("Remove-Item -Force $ex5 -ErrorAction SilentlyContinue") < body.index('& $editor /compile:"$destMq5"')
    assert "$compiled = Test-StoicCompileLog -LogPath $log" in body and "if ($compiled -and (Test-Path $ex5))" in body
    assert "Test-StoicCompileLog -LogPath $ok" in _read("scripts/test_installer.ps1")
    assert 'data-testid="quick-install-terminalid-hint"' in _read("frontend/src/components/QuickInstallPanel.jsx")


# ── N105-6 ───────────────────────────────────────────────────────────────────────────────────────
def test_n105_6_progress_panel_polish():
    import install_progress as ip
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    acc = {"_id": "x", "installer_paired_at": (now - timedelta(hours=2)).isoformat(), "installer_paired_hostname": "VPS-1", "installer_version": "1.4"}
    spare = {"issued_at": (now - timedelta(hours=1)).isoformat(), "expires_at": (now - timedelta(minutes=45)).isoformat()}   # minted AFTER pairing, unused
    res = ip.derive(acc, spare, {"installation_id": "i", "host_fingerprint": "VPS-1"}, attested=("unmeasured", None), accepted_hashes=[], now=now)
    tok = next(s for s in res["steps"] if s["id"] == "token")
    assert tok["status"] == "done" and "later token was never used" in tok["detail"]
    panel = _read("frontend/src/components/InstallProgressPanel.jsx")
    assert 'document.visibilityState === "hidden"' in panel and "setData(null);" in panel and '"visibilitychange"' in panel
    da = _read("backend/device_attestation.py")
    assert 'db.pairing_tokens.create_index([("account_id", 1), ("issued_at", -1)])' in da
    assert 'db.installations.create_index([("account_id", 1), ("revoked", 1), ("created_at", -1)])' in da
