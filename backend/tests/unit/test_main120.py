"""main120 — M120-1 (overlay unbindable only on cPanel/VirtFS hosts — see test_main117_overlay_unbindable), M120-2 key /
certificate age reminders (deploy/state/key_ages.json → readiness `key_ages` WARN + `key_rotation_due` ops alert),
reboot-recipe duplicate line removed."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402
import key_ages as ka  # noqa: E402
import key_age_alerts as kaa  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc(release_days=10, runtime_days=10, cert_days_left=400, cert=True):
    return {"schema": 1, "generated_at": _iso(NOW),
            "release_key": {"key_id": "stoic-release-ed25519-v1", "created": (NOW - timedelta(days=release_days)).strftime("%Y-%m-%d")},
            "runtime_key": {"path": "secrets/signer_ed25519_key", "created": _iso(NOW - timedelta(days=runtime_days))},
            "origin_cert": {"path": "secrets/origin_cert.pem", "present": cert, "not_after": _iso(NOW + timedelta(days=cert_days_left))}}


def test_key_ages_fresh_everything_ok():
    p = ka.plan(_doc(), NOW)
    assert p["available"] and p["ok"] and p["due"] == [] and p["warning"] is None and not p["stale"]
    assert [i["name"] for i in p["items"]] == ["release_key", "runtime_key", "origin_cert"]
    assert p["items"][0]["age_days"] == 10 and p["items"][2]["days_left"] == 400
    assert ka.plan(None, NOW) == {"available": False, "ok": True, "due": [], "items": [], "warning": None, "generated_at": None}


def test_key_ages_warn_at_180_days_and_cert_30_days():
    p = ka.plan(_doc(release_days=181, runtime_days=180), NOW)       # 180 is still fine, 181 is due
    assert p["due"] == ["release_key"] and "181 days old" in p["warning"] and "180 d" in p["warning"]
    assert p["items"][0]["fix"].startswith("docs/RELEASE_KEY_ROTATION.md") and p["items"][1]["fix"] is None
    p = ka.plan(_doc(runtime_days=400, cert_days_left=29), NOW)
    assert p["due"] == ["runtime_key", "origin_cert"]
    assert "rotate-runtime-key.sh" in p["items"][1]["fix"] and "Origin Server" in p["items"][2]["fix"]
    assert "expires in 29 days" in p["warning"]
    p = ka.plan(_doc(cert_days_left=-3), NOW)
    assert "EXPIRED 3 days ago" in p["warning"]
    # non-Cloudflare install: no cert item; unknown dates never fire but are noted
    p = ka.plan(_doc(cert=False), NOW)
    assert len(p["items"]) == 2
    d = _doc(); d["release_key"]["created"] = None; d["runtime_key"]["created"] = None
    p = ka.plan(d, NOW)
    assert p["due"] == [] and "no created= date" in p["items"][0]["note"] and "not found" in p["items"][1]["note"]
    d = _doc(); d["generated_at"] = _iso(NOW - timedelta(days=5))
    assert ka.plan(d, NOW)["stale"] is True


def test_key_ages_reads_file_next_to_host_profile(tmp_path):
    (tmp_path / "key_ages.json").write_text(json.dumps(_doc(release_days=200)))
    env = {"STOIC_HOST_PROFILE_FILE": str(tmp_path / "host_profile.json")}
    assert ka.key_ages(env, NOW)["due"] == ["release_key"]
    assert ka.key_ages({"STOIC_KEY_AGES_FILE": str(tmp_path / "missing.json")}, NOW)["available"] is False
    (tmp_path / "bad.json").write_text("{not json")
    assert ka.key_ages({"STOIC_KEY_AGES_FILE": str(tmp_path / "bad.json")}, NOW)["available"] is False


def test_key_rotation_due_alert_plan_and_evaluate(tmp_path, monkeypatch):
    p = kaa.plan(ka.plan(_doc(release_days=200, cert_days_left=5), NOW))
    assert p["active"] == {"key_age:release_key", "key_age:origin_cert"}
    kinds = {k for k, _s, _d, _t in p["raise"]}; sev = {s for _k, s, _d, _t in p["raise"]}
    assert kinds == {kaa.KIND} and sev == {"warning"}
    texts = " | ".join(t for *_r, t in p["raise"])
    assert "200 days old" in texts and "expires in 5 days" in texts and "rotate-release-pin" in texts
    assert kaa.plan(ka.plan(_doc(), NOW)) == {"active": set(), "raise": []}
    # evaluate → raise_alert called with dedup keys; auto-resolve path = keys drop out of `active`
    (tmp_path / "key_ages.json").write_text(json.dumps(_doc(runtime_days=300)))
    monkeypatch.setenv("STOIC_KEY_AGES_FILE", str(tmp_path / "key_ages.json"))
    calls = []

    async def raise_alert(db, kind, severity, text, dedup_key=None, **kw):
        calls.append((kind, severity, dedup_key)); return "id"

    active, n = asyncio.new_event_loop().run_until_complete(kaa.evaluate(FakeDb(), NOW, raise_alert=raise_alert))
    assert active == {"key_age:runtime_key"} and n == 1 and calls == [(kaa.KIND, "warning", "key_age:runtime_key")]
    src = open(os.path.join(ROOT, "backend", "alerting.py")).read()
    assert '"key_rotation_due"' in src and "key_age_alerts.evaluate" in src       # wired + auto-resolving kind
    ops = open(os.path.join(ROOT, "backend", "routes", "ops_routes.py")).read()
    assert 'checks["key_ages"]' in ops


def test_write_key_ages_file_shell(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(os.path.join(ROOT, "deploy"), root / "deploy", ignore=shutil.ignore_patterns("state", "*.log"))
    (root / "release").mkdir(); (root / "backend").mkdir(); (root / "secrets").mkdir(); (root / ".env").write_text("")
    (root / "release" / "release_key.fingerprint").write_text(
        "# hdr\nstoic-release-ed25519-v2 SHA256:ab current created=2026-09-01\nstoic-release-ed25519-v1 SHA256:cd transition created=2026-01-15\n")
    (root / "backend" / ".env").write_text("RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v1\n")
    (root / "secrets" / "signer_ed25519_key").write_text("k")
    os.utime(root / "secrets" / "signer_ed25519_key", (1_700_000_000, 1_700_000_000))
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ed25519", "-nodes", "-keyout", str(root / "secrets" / "origin_key.pem"),
                    "-out", str(root / "secrets" / "origin_cert.pem"), "-days", "400", "-subj", "/CN=stoicaibot.com"], check=True, capture_output=True)
    r = subprocess.run(["bash", "-c", "set -u; . deploy/lib.sh; . deploy/preflight.sh; write_key_ages_file; echo RC=$?"], cwd=root, capture_output=True, text=True)
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    doc = json.loads((root / "deploy" / "state" / "key_ages.json").read_text())
    assert doc["release_key"] == {"key_id": "stoic-release-ed25519-v1", "created": "2026-01-15"}   # the PINNED key id, not the repo's current
    assert doc["runtime_key"]["created"] == "2023-11-14T22:13:20Z"
    assert doc["origin_cert"]["present"] is True
    na = datetime.fromisoformat(doc["origin_cert"]["not_after"].replace("Z", "+00:00"))
    assert 398 <= (na - datetime.now(timezone.utc)).days <= 400
    # the backend evaluates exactly this file
    p = ka.key_ages({"STOIC_KEY_AGES_FILE": str(root / "deploy" / "state" / "key_ages.json")})
    assert p["available"] and "runtime_key" in p["due"] and "origin_cert" not in p["due"]
    # no cert / no runtime key / no created= → nulls, still RC=0
    for f in ("origin_cert.pem", "signer_ed25519_key"):
        (root / "secrets" / f).unlink()
    (root / "backend" / ".env").write_text("RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v9\n")
    r = subprocess.run(["bash", "-c", "set -u; . deploy/lib.sh; . deploy/preflight.sh; write_key_ages_file; echo RC=$?"], cwd=root, capture_output=True, text=True)
    doc = json.loads((root / "deploy" / "state" / "key_ages.json").read_text())
    assert "RC=0" in r.stdout and doc["release_key"]["created"] is None and doc["runtime_key"]["created"] is None and doc["origin_cert"]["present"] is False
    src = open(os.path.join(ROOT, "deploy", "host-profile-refresh.sh")).read()
    pre = open(os.path.join(ROOT, "deploy", "preflight.sh")).read()
    assert "write_key_ages_file" in src and pre.count("write_key_ages_file") >= 2          # timer + record_host_profile


def test_fingerprint_file_has_created_column_and_status_parsing_ignores_it():
    lines = [ln.split() for ln in open(os.path.join(ROOT, "release", "release_key.fingerprint")) if ln.strip() and not ln.startswith("#")]
    assert lines and all(len(p) == 4 and p[2] in ("current", "transition", "revoked") and p[3].startswith("created=") for p in lines)
    r = subprocess.run(["bash", "-c", ". deploy/lib.sh; . deploy/preflight.sh; release_key_status stoic-release-ed25519-v1; current_release_key_id; expected_release_fingerprint stoic-release-ed25519-v2"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.stdout.split() == ["revoked", "stoic-release-ed25519-v2", "SHA256:d01efc8d4288cd5266218e65b20ef93e415d5696f15b5d9b456ea560c0bb8620"]


def test_reboot_recipe_has_no_duplicate_line():
    r = subprocess.run(["bash", "-c", ". deploy/lib.sh; . deploy/preflight.sh; reboot_recipe"], cwd=ROOT, capture_output=True, text=True)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(set(lines)) and sum("volumes and data are untouched" in ln for ln in lines) == 1
    assert "move-docker-root.sh" in r.stdout
