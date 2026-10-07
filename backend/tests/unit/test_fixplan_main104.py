"""main104 review:
N104-1 installer download URL (`"${eaScriptUrl}?v=…"`) + terminal chosen BEFORE the pairing token is spent +
pwsh behavioural test on the Windows runner; N104-2 signed policies shipped in the backend image; N104-3
STOIC_INSTALLATION_ID minted once by deploy/lib.sh and shown on Demo Readiness / the go-live panel; N104-4
no terminal.ini WebRequest write, origin.txt / portable MetaEditor / usage fixes; N104-5 honest approval
wording; N104-6 the previous EA record is signed inside the current record."""
import base64
import os
import re
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _local_signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "RELEASE_SIGNER_DEFERRED": "false",
            "ED25519_SIGNING_KEY_B64": priv, "RELEASE_PUBLIC_KEY_B64": pub}


# ── N104-1 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_1_installer_builds_the_download_url_and_picks_the_terminal_before_spending_the_token():
    ps = _read("backend/static/STOIC-Installer.ps1")
    assert '"$eaScriptUrl?v=' not in ps                                     # `$eaScriptUrl?v` was parsed as ONE (empty) variable
    assert 'return "${BaseUrl}${sep}v=${Version}"' in ps
    assert "Invoke-WebRequest -Uri (Get-StoicEaDownloadUrl -BaseUrl $eaScriptUrl -Version $eaLatestVer)" in ps
    # no other `$name?` / `$name:` pitfalls inside double-quoted strings
    assert not re.search(r'"[^"\n]*\$(?!env:)[A-Za-z_][A-Za-z0-9_]*[?:][A-Za-z][^"\n]*"', ps.replace("${", "")), "variable followed by ?/: inside a string"
    body = ps[ps.index("function Install-Stoic"):]
    assert body.index("Resolve-StoicTerminal -TerminalPath $TerminalPath -TerminalId $TerminalId") < body.index("/api/setup/claim-pairing")
    assert "if (-not $terminal) { return }" in body                        # bad path / no terminal / bad choice → token untouched
    assert "pairing token NOT used" in ps
    assert '$InstallerVersion = "1.3"' in ps
    assert ". .\\STOIC-Installer.ps1" in ps and ".\\STOIC-Installer.ps1 -Token" not in ps   # usage line: the file only defines the function
    # the behavioural pwsh test exists, dot-sources the installer and runs on the Windows CI runner
    t = _read("scripts/test_installer.ps1")
    assert ". $installer" in t and "Get-StoicEaDownloadUrl" in t and "Resolve-StoicTerminal" in t and "claim-pairing" in t
    ci = _read(".github/workflows/ci.yml")
    win = ci[ci.index("runs-on: windows-latest"):]
    assert win.index("scripts/test_installer.ps1") < win.index("Install MetaTrader 5 (silent)")
    assert "shell: pwsh" in win[:win.index("scripts/test_installer.ps1")]


# ── N104-2 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_2_backend_image_ships_the_signed_policies():
    df = _read("Dockerfile.backend")
    assert "COPY release/policy_migrations/ /app/release/policy_migrations/" in df
    assert os.path.exists(os.path.join(ROOT, "release", "policy_migrations", ".gitkeep"))   # empty folder still builds
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "release/policy_migrations/.gitkeep"],
                             cwd=ROOT, capture_output=True, text=True)
    ignored = subprocess.run(["git", "check-ignore", "-q", "release/policy_migrations/.gitkeep"], cwd=ROOT)
    assert tracked.returncode == 0 or ignored.returncode != 0
    # the API reads exactly that folder
    assert '"release", "policy_migrations"' in _read("backend/routes/authority_routes.py")


# ── N104-3 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_3_installation_id_minted_once_by_lib_sh(tmp_path):
    lib = os.path.join(ROOT, "deploy", "lib.sh")
    (tmp_path / "backend").mkdir()
    (tmp_path / "secrets").mkdir()

    def run():
        return subprocess.run(["bash", "-c", f". {lib}; ensure_installation_id"], cwd=tmp_path,
                              capture_output=True, text=True, check=True).stdout

    assert run() == ""                                                    # no backend/.env yet → no-op
    (tmp_path / "backend" / ".env").write_text("APP_ENV=production\n")
    out = run()
    env = (tmp_path / "backend" / ".env").read_text()
    m = re.search(r"^STOIC_INSTALLATION_ID=(stoic-[0-9a-f]{16})$", env, re.M)
    assert m and m.group(1) in out and env.startswith("APP_ENV=production\n")
    assert run() == "" and (tmp_path / "backend" / ".env").read_text() == env   # idempotent: NEVER regenerated
    (tmp_path / "backend" / ".env").write_text("STOIC_INSTALLATION_ID=keep-me\n")
    run()
    assert (tmp_path / "backend" / ".env").read_text() == "STOIC_INSTALLATION_ID=keep-me\n"
    upd = _read("deploy/update.sh")
    assert upd.index("ensure_release_secrets || gate_refused") < upd.index("\nensure_installation_id")   # update.sh path
    inst = _read("deploy/install.sh")
    assert inst.index('echo "-- backend/.env exists — leaving untouched"') < inst.index("\nensure_installation_id")   # install.sh: after backend/.env exists


def test_n104_3_demo_readiness_shows_the_installation_id():
    import demo_readiness as dr
    rows = {c["id"]: c for c in dr.env_checks({"APP_ENV": "production", "STOIC_INSTALLATION_ID": "stoic-abc"})}
    assert rows["installation_id"]["status"] == "pass" and rows["installation_id"]["detail"] == "stoic-abc"
    rows = {c["id"]: c for c in dr.env_checks({"APP_ENV": "production"})}
    assert rows["installation_id"]["status"] == "fail"                   # production without an id refuses every signed policy
    rows = {c["id"]: c for c in dr.env_checks({"APP_ENV": "preview"})}
    assert rows["installation_id"]["status"] == "info"
    src = _read("backend/demo_readiness.py")
    assert '"installation_id": installation_id()' in src[src.index("async def build"):]
    with patch.dict(os.environ, {"STOIC_INSTALLATION_ID": "stoic-xyz"}):
        import inventory_projection as ip
        assert ip.installation_id() == "stoic-xyz"
    assert 'data-testid="demo-readiness-installation-id"' in _read("frontend/src/pages/DemoReadiness.jsx")
    panel = _read("frontend/src/components/admin/InventoryGoLivePanel.jsx")
    assert 'data-testid="expectation-host-id"' in panel and "setHostId(c.data?.installation_id" in panel
    assert '"installation_id": installation_id()' in _read("backend/routes/authority_routes.py")


# ── N104-4 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_4_installer_no_terminal_ini_write_origin_guard_portable_editor():
    ps = _read("backend/static/STOIC-Installer.ps1")
    body = ps[ps.index("function Install-Stoic"):]
    assert 'config\\terminal.ini' not in ps and "Set-Content -Path $iniPath" not in ps and "$iniPath" not in body   # ineffective + overwritten by MT5
    assert "Allow WebRequest for listed URL" in body and "$heartbeatHost" in body  # manual step printed with the URL
    assert "function Get-StoicTerminalOrigin" in ps and 'Where-Object { "$_".Trim() }' in ps   # empty origin.txt tolerated
    assert "(Get-Content $origin | Select-Object -First 1).Trim()" not in ps
    assert '$candidates = @((Join-Path $t.FullName "metaeditor64.exe")) + $candidates' in ps   # portable: own folder


# ── N104-5 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_5_approval_wording_is_honest():
    for rel in (".github/workflows/policy-migration.yml", "scripts/sign_policy_migration.py"):
        txt = _read(rel).lower()
        assert "dual approval" not in txt and "2 required reviewers" not in txt, rel
    wf = _read(".github/workflows/policy-migration.yml")
    assert "ONE required reviewer" in wf and "SECOND, independent approval happens on the server" in wf
    doc = _read("docs/DEPLOYMENT.md")
    sec = doc[doc.index("### Signed DEMO-only inventory policy"):doc.index("### Windows installer")]
    assert "dual approval" not in sec.lower() and "**one** required reviewer" in sec and "second, independent" in sec.lower()
    assert "Prevent self-review" in sec


# ── N104-6 ───────────────────────────────────────────────────────────────────────────────────────
def test_n104_6_previous_release_is_signed_inside_the_current_record():
    import ea_capabilities as ec
    import release_signing as rs
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import verify_ea_release as ver
    base = {"mq5_sha256": "a" * 64, "metaeditor_version": None, "windows_build": None, "mt5_build": None,
            "source_commit": "c" * 40, "compiled_by": "github-actions"}
    with patch.dict(os.environ, _local_signer_env()):
        kid = rs.key_id(purpose="ea-release")
        prev = {**base, "version": "1.59", "ex5_sha256": "b" * 64}
        prev["signature"] = {"key_id": kid, "sig_hex": rs.sign_hex(ec._canonical_payload(prev), purpose="ea-release")}
        cur = {**base, "version": "1.60", "ex5_sha256": "d" * 64, "previous": prev, "signature": {"key_id": kid}}
        cur["signature"]["sig_hex"] = rs.sign_hex(ec._canonical_payload(cur), purpose="ea-release")
        assert rs.verify_hex(ec._canonical_payload(cur), cur["signature"]["sig_hex"], purpose="ea-release")
        # the signer script and the backend verifier agree on the statement
        assert ver._canonical_payload(cur) == ec._canonical_payload(cur)
        # graft an OLDER self-signed release in as `previous` → the current signature breaks
        rogue = {**base, "version": "1.55", "ex5_sha256": "e" * 64}
        rogue["signature"] = {"key_id": kid, "sig_hex": rs.sign_hex(ec._canonical_payload(rogue), purpose="ea-release")}
        grafted = {**cur, "previous": rogue}
        assert not rs.verify_hex(ec._canonical_payload(grafted), cur["signature"]["sig_hex"], purpose="ea-release")
        # swapping only the previous EX5 hash breaks it too; dropping `previous` breaks it as well
        assert not rs.verify_hex(ec._canonical_payload({**cur, "previous": {**prev, "ex5_sha256": "f" * 64}}), cur["signature"]["sig_hex"], purpose="ea-release")
        assert not rs.verify_hex(ec._canonical_payload({**cur, "previous": None}), cur["signature"]["sig_hex"], purpose="ea-release")
        # a record signed WITHOUT previous stays valid, and cannot have one added afterwards
        solo = {**base, "version": "1.60", "ex5_sha256": "d" * 64, "signature": {"key_id": kid}}
        solo["signature"]["sig_hex"] = rs.sign_hex(ec._canonical_payload(solo), purpose="ea-release")
        assert rs.verify_hex(ec._canonical_payload(solo), solo["signature"]["sig_hex"], purpose="ea-release")
        assert not rs.verify_hex(ec._canonical_payload({**solo, "previous": rogue}), solo["signature"]["sig_hex"], purpose="ea-release")
        # legacy payload (previous record, N103-6) never includes `previous`
        assert b'"previous"' not in ec._canonical_payload(cur, legacy=True)
