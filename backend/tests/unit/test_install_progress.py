"""Installer progress panel — pure derivation of the VPS pairing state (install_progress.derive)
and the read-only route wiring."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import install_progress as ip  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
H = "a" * 64


def _iso(delta_s):
    return (NOW - timedelta(seconds=delta_s)).isoformat()


def _steps(res):
    return {s["id"]: s for s in res["steps"]}


def test_not_started_without_any_token():
    res = ip.derive({"_id": "x"}, None, None, attested=("unmeasured", None), accepted_hashes=[], now=NOW, request_base="https://s.example/")
    assert res["state"] == "not_started" and res["done"] == 0 and res["total"] == 5
    assert _steps(res)["token"]["status"] == "pending" and res["webrequest_url"] == "https://s.example"


def test_outstanding_token_is_waiting_and_expired_token_warns():
    tok = {"issued_at": _iso(60), "expires_at": (NOW + timedelta(minutes=10)).isoformat()}
    res = ip.derive({"_id": "x"}, tok, None, attested=("unmeasured", None), accepted_hashes=[], now=NOW)
    assert _steps(res)["token"]["status"] == "waiting" and res["state"] == "in_progress"
    tok = {"issued_at": _iso(3600), "expires_at": _iso(1800)}
    res = ip.derive({"_id": "x"}, tok, None, attested=("unmeasured", None), accepted_hashes=[], now=NOW)
    assert _steps(res)["token"]["status"] == "warn" and res["state"] == "attention"


def test_paired_without_heartbeat_blames_webrequest_after_the_grace_period():
    acc = {"_id": "x", "installer_paired_at": _iso(600), "installer_paired_hostname": "VPS-1", "installer_version": "1.3"}
    tok = {"issued_at": _iso(700), "consumed_at": _iso(600), "consumed_by_hostname": "VPS-1", "installer_version": "1.3"}
    inst = {"installation_id": "inst_1", "host_fingerprint": "VPS-1", "ex5_sha256": H}
    res = ip.derive(acc, tok, inst, attested=("installer_attested", H), accepted_hashes=[H], now=NOW, request_base="https://s.example/")
    s = _steps(res)
    assert s["token"]["status"] == "done" and "VPS-1" in s["token"]["detail"]
    assert s["install"]["status"] == "done" and "inst_1" in s["install"]["detail"]
    assert s["binary"]["status"] == "done" and "signed release" in s["binary"]["detail"]
    assert s["heartbeat"]["status"] == "blocked" and "https://s.example" in s["heartbeat"]["hint"] and "WebRequest" in s["heartbeat"]["hint"]
    assert res["state"] == "blocked" and res["headline"].startswith("EA heartbeat")
    # inside the grace window it is merely waiting
    acc["installer_paired_at"] = _iso(30)
    res = ip.derive(acc, tok, inst, attested=("installer_attested", H), accepted_hashes=[H], now=NOW)
    assert _steps(res)["heartbeat"]["status"] == "waiting" and res["state"] == "in_progress"


def test_heartbeat_before_pairing_does_not_count_as_first_heartbeat():
    acc = {"_id": "x", "installer_paired_at": _iso(600), "last_heartbeat": _iso(900), "installer_version": "1.3"}
    res = ip.derive(acc, None, None, attested=("unmeasured", None), accepted_hashes=[], now=NOW)
    assert _steps(res)["heartbeat"]["status"] == "blocked"


def test_ready_when_everything_is_done_and_old_installer_warns():
    acc = {"_id": "x", "installer_paired_at": _iso(600), "last_heartbeat": _iso(5), "installer_version": "1.3",
           "ea_identity": {"authoritative": True, "installation_id": "inst_1", "ea_version": "1.60"},
           "verified_identity": {"installation_id": "inst_1"}, "account_trade_mode": "demo"}
    inst = {"installation_id": "inst_1", "host_fingerprint": "VPS-1", "ex5_sha256": H}
    res = ip.derive(acc, {"consumed_at": _iso(600), "consumed_by_hostname": "VPS-1"}, inst,
                    attested=("installer_attested", H), accepted_hashes=[H], now=NOW)
    assert res["state"] == "ready" and res["done"] == 5
    hb = _steps(res)["heartbeat"]
    assert "EA 1.60" in hb["detail"] and "broker says demo" in hb["detail"]
    acc["installer_version"] = "1.2"
    res = ip.derive(acc, {"consumed_at": _iso(600)}, inst, attested=("installer_attested", H), accepted_hashes=[H], now=NOW)
    assert _steps(res)["install"]["status"] == "warn" and "outdated" in _steps(res)["install"]["detail"] and res["state"] == "attention"


def test_binary_states_and_identity_block():
    acc = {"_id": "x", "installer_paired_at": _iso(600), "last_heartbeat": _iso(5), "installer_version": "1.3",
           "ea_identity": {"authoritative": False, "reason": "EA v1.55+ heartbeat missing installation_id"}}
    inst = {"installation_id": "inst_1", "ex5_sha256": "b" * 64}
    res = ip.derive(acc, {"consumed_at": _iso(600)}, inst, attested=("installer_attested", "b" * 64), accepted_hashes=[H], now=NOW)
    s = _steps(res)
    assert s["binary"]["status"] == "warn" and "not the signed release" in s["binary"]["detail"]
    assert s["identity"]["status"] == "blocked" and "missing installation_id" in s["identity"]["detail"]
    res = ip.derive(acc, {"consumed_at": _iso(600)}, inst, attested=("installer_attested", "b" * 64), accepted_hashes=[], now=NOW)
    assert "no signed release published yet" in _steps(res)["binary"]["detail"]
    acc["ea_binary_sha256_method"] = "installer_mismatch"
    res = ip.derive(acc, {"consumed_at": _iso(600)}, inst, attested=("installer_attested", "b" * 64), accepted_hashes=[H], now=NOW)
    assert _steps(res)["binary"]["status"] == "blocked"
    res = ip.derive({"_id": "x", "installer_paired_at": _iso(600)}, {"consumed_at": _iso(600)}, {"installation_id": "i"},
                    attested=("unmeasured", None), accepted_hashes=[H], now=NOW)
    assert _steps(res)["binary"]["status"] == "waiting" and "F7" in _steps(res)["binary"]["hint"]


def test_trusted_terminal_without_installer_is_not_an_outdated_installer():
    acc = {"_id": "x", "last_heartbeat": _iso(5), "verified_identity": {"installation_id": "inst_t"},
           "ea_identity": {"authoritative": True, "installation_id": "inst_t", "ea_version": "1.56"}}
    inst = {"installation_id": "inst_t", "host_fingerprint": "mt5-login-123", "terminal_path": "trusted"}
    tok = {"issued_at": _iso(900000), "expires_at": _iso(899100)}
    res = ip.derive(acc, tok, inst, attested=("unmeasured", None), accepted_hashes=[], now=NOW)
    s = _steps(res)
    assert s["install"]["status"] == "done" and "without the installer" in s["install"]["detail"]
    assert s["token"]["status"] == "pending" and "expired unused" in s["token"]["detail"]
    assert s["heartbeat"]["status"] == "done" and s["identity"]["status"] == "done"
    assert res["state"] == "not_started" or res["state"] == "in_progress"   # binary unmeasured → not ready, nothing blocked/warned


def test_webrequest_url_prefers_env_then_forwarded_origin_then_request_base(monkeypatch):
    monkeypatch.delenv("PUBLIC_BACKEND_URL", raising=False)
    assert ip.webrequest_url("http://10.0.0.1:8001/", "https", "stoic.example") == "https://stoic.example"
    assert ip.webrequest_url("http://10.0.0.1:8001/", None, "stoic.example, proxy") == "https://stoic.example"
    assert ip.webrequest_url("http://10.0.0.1:8001/", None, None) == "http://10.0.0.1:8001"
    monkeypatch.setenv("PUBLIC_BACKEND_URL", "https://prod.example/")
    assert ip.webrequest_url("http://10.0.0.1:8001/", "https", "other") == "https://prod.example"


def test_stale_heartbeat_warns_and_route_is_owner_or_admin_only():
    acc = {"_id": "x", "installer_paired_at": _iso(6000), "last_heartbeat": _iso(3000), "installer_version": "1.3"}
    res = ip.derive(acc, {"consumed_at": _iso(6000)}, None, attested=("unmeasured", None), accepted_hashes=[], now=NOW)
    assert _steps(res)["heartbeat"]["status"] == "warn" and "offline" in _steps(res)["heartbeat"]["detail"]
    src = open(os.path.join(ROOT, "backend", "routes", "setup_routes.py"), encoding="utf-8").read()
    body = src[src.index('@router.get("/setup/install-progress/{account_id}")'):]
    assert 'account.get("user_id") != user["id"] and user.get("role") != "admin"' in body
    assert "da.attested_hash(installation)" in body and "accepted_ea_sha256s()" in body
    assert '"revoked": {"$ne": True}' in body
    # UI: panel in the Quick Install area, chip in the account header, stale WebRequest copy gone
    qi = open(os.path.join(ROOT, "frontend", "src", "components", "QuickInstallPanel.jsx"), encoding="utf-8").read()
    assert qi.count("<InstallProgressPanel accountId={accountId} />") == 2 and "whitelist dance" not in qi
    accounts = open(os.path.join(ROOT, "frontend", "src", "pages", "Accounts.jsx"), encoding="utf-8").read()
    assert "<InstallProgressChip accountId={a.id} />" in accounts
    panel = open(os.path.join(ROOT, "frontend", "src", "components", "InstallProgressPanel.jsx"), encoding="utf-8").read()
    for tid in ("install-progress-chip-", "install-progress-", "install-step-", "install-webrequest-url-", "install-webrequest-copy-"):
        assert tid in panel, tid
