"""STOIC main104 review live tests (iter242): installer, demo-readiness, inventory.

Credentials: TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD env — never in source.
"""
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))

import re
import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:400]}"
    # cookie must be set
    assert any(c for c in s.cookies), "no auth cookie set"
    return s


def test_health():
    r = requests.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200


def test_demo_readiness_installation_id(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/demo-readiness", timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    ctx = data.get("context", {})
    assert ctx.get("installation_id") == "dev-local", f"context.installation_id={ctx.get('installation_id')}"
    checks = data.get("checks", [])
    iid = next((c for c in checks if c.get("id") == "installation_id"), None)
    assert iid is not None, "no 'installation_id' check in checks[]"
    assert iid.get("status") == "info", f"status={iid.get('status')}"
    assert "STOIC_INSTALLATION_ID" in (iid.get("title") or ""), f"title={iid.get('title')}"


def test_inventory_policies(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/authority/inventory/policies", timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert data.get("policies") == []
    assert data.get("installation_id") == "dev-local"
    assert data.get("environment") == "preview"


def test_installer_ps1_public():
    r = requests.get(f"{BASE_URL}/api/setup/installer.ps1", timeout=30)
    assert r.status_code == 200, r.text[:400]
    body = r.text
    assert re.search(r'\$InstallerVersion = "1\.[3-9]"', body), "missing InstallerVersion >= 1.3"
    assert "function Get-StoicEaDownloadUrl" in body
    assert "function Resolve-StoicTerminal" in body
    # Ordering: Resolve-StoicTerminal -TerminalPath must appear BEFORE /api/setup/claim-pairing INSIDE Install-Stoic
    install_match = re.search(r"function Install-Stoic\b", body)
    assert install_match, "no Install-Stoic function"
    tail = body[install_match.start():]
    idx_resolve = tail.find("Resolve-StoicTerminal -TerminalPath")
    idx_claim = tail.find("/api/setup/claim-pairing")
    assert idx_resolve != -1, "Resolve-StoicTerminal -TerminalPath not found in Install-Stoic"
    assert idx_claim != -1, "/api/setup/claim-pairing not found in Install-Stoic"
    assert idx_resolve < idx_claim, f"Resolve-StoicTerminal must precede claim-pairing (resolve={idx_resolve} claim={idx_claim})"
    assert '"$eaScriptUrl?v=' not in body, "raw $eaScriptUrl?v= concatenation present (N104-1 bug)"
    for ln in body.splitlines():                                  # N104-4 — terminal.ini is READ (N111-6 ProfileLast), never written
        if "terminal.ini" in ln:
            assert not any(w in ln for w in ("Set-Content", "Add-Content", "Out-File")), f"installer writes terminal.ini (N104-4): {ln}"


def test_release_gate(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert "accounts" in data and isinstance(data["accounts"], list)
