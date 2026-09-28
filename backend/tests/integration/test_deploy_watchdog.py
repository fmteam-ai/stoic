"""Deploy Watchdog — target allow-list, arm/poll state machine, live + stalled
notifications (e-mail sender stubbed), signer-health preflight row details."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def fake_send(recipient, subject, html, text=None, sender=None, idempotency_key=None):
        sent.append({"to": recipient, "subject": subject, "html": html, "text": text})
        return {"ok": True, "id": f"msg-{len(sent)}"}

    import email_sender
    monkeypatch.setattr(email_sender, "send_email", fake_send)
    return sent


def _fetcher(seq):
    calls = {"n": 0}

    def fetch(target, timeout=10.0):
        obs = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return {"http": obs[0], "build_sha": obs[1], "app_env": "production", "status": "ok", "error": None}
    return fetch


def test_target_validation_is_https_and_allow_listed(monkeypatch):
    import deploy_watch as dw
    monkeypatch.setenv("DEPLOY_WATCH_ALLOWED_HOSTS", "www.stoicaibot.com")
    assert dw.validate_target("https://www.stoicaibot.com/") == "https://www.stoicaibot.com"
    for bad in ("http://www.stoicaibot.com", "https://evil.example", "https://www.stoicaibot.com/?x=1",
                "https://user:pw@www.stoicaibot.com", ""):
        with pytest.raises(ValueError):
            dw.validate_target(bad)
    assert dw.validate_sha("ABCDEF1") == "abcdef1"
    assert dw.validate_sha("") is None
    with pytest.raises(ValueError):
        dw.validate_sha("not-hex")


def test_arm_then_live_sends_one_email(mail, monkeypatch):
    import deploy_watch as dw
    from database import get_db

    async def go():
        db = get_db()
        await db.deploy_watch.delete_many({})
        monkeypatch.setattr(dw, "fetch_health", _fetcher([(500, None)]))
        w = await dw.arm(db, target_url="https://www.stoicaibot.com", expected_sha="cfdb46c8",
                         notify_email="ops@example.com", armed_by="ops@example.com", timeout_h=1)
        assert w["status"] == "watching" and w["baseline_sha"] is None and w["baseline_http"] == 500
        # old build still up → no state change
        assert await dw.poll_once(db, fetch=_fetcher([(200, "1111111deadbeef")])) == []
        cur = await dw.latest(db)
        assert cur["status"] == "watching" and cur["polls"] == 2 and cur["last"]["build_sha"] == "1111111deadbeef"
        changed = await dw.poll_once(db, fetch=_fetcher([(200, "cfdb46c89a8351a0983f75ee")]))
        assert len(changed) == 1 and changed[0]["status"] == "live"
        assert changed[0]["live_sha"].startswith("cfdb46c8")
        assert len(mail) == 1 and "LIVE" in mail[0]["subject"] and mail[0]["to"] == "ops@example.com"
        # finished watches are never polled again
        assert await dw.poll_once(db, fetch=_fetcher([(200, "cfdb46c8")])) == []
        assert len(mail) == 1
        assert (await dw.latest(db))["notified"]["ok"] is True
    _run(go())


def test_any_new_build_mode_and_stall_email(mail, monkeypatch):
    import deploy_watch as dw
    from database import get_db

    async def go():
        db = get_db()
        await db.deploy_watch.delete_many({})
        monkeypatch.setattr(dw, "fetch_health", _fetcher([(200, "oldsha0000")]))
        w = await dw.arm(db, target_url="https://stoicaibot.com", expected_sha=None,
                         notify_email="ops@example.com", armed_by="ops@example.com", timeout_h=1)
        assert w["baseline_sha"] == "oldsha0000"
        assert await dw.poll_once(db, fetch=_fetcher([(200, "oldsha0000")])) == []
        # expire the watch → stalled mail
        await db.deploy_watch.update_one({"_id": w["id"]},
                                         {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        changed = await dw.poll_once(db, fetch=_fetcher([(200, "oldsha0000")]))
        assert changed[0]["status"] == "stalled" and "STALLED" in mail[-1]["subject"]
        # arming again supersedes; a different sha flips to live in any-new-build mode
        w2 = await dw.arm(db, target_url="https://stoicaibot.com", expected_sha=None,
                          notify_email="ops@example.com", armed_by="ops@example.com", timeout_h=1)
        changed = await dw.poll_once(db, fetch=_fetcher([(200, "newsha1111")]))
        assert changed[0]["id"] == w2["id"] and changed[0]["status"] == "live"
        assert (await dw.cancel(db, "x")) is None
    _run(go())


def test_arm_already_live_and_cancel(mail, monkeypatch):
    import deploy_watch as dw
    from database import get_db

    async def go():
        db = get_db()
        await db.deploy_watch.delete_many({})
        monkeypatch.setattr(dw, "fetch_health", _fetcher([(200, "cfdb46c89a83")]))
        w = await dw.arm(db, target_url="https://www.stoicaibot.com", expected_sha="cfdb46c8",
                         notify_email="ops@example.com", armed_by="ops@example.com")
        assert w["status"] == "live" and w["ended_reason"] == "already_live"
        w = await dw.arm(db, target_url="https://www.stoicaibot.com", expected_sha="ffffffff",
                         notify_email="ops@example.com", armed_by="ops@example.com")
        assert w["status"] == "watching"
        c = await dw.cancel(db, "admin@example.com")
        assert c["status"] == "cancelled" and "admin@example.com" in c["ended_reason"]
        with pytest.raises(ValueError):
            await dw.arm(db, target_url="https://www.stoicaibot.com", expected_sha=None,
                         notify_email="a", armed_by="a", timeout_h=999)
    _run(go())


def test_preflight_signer_health_row_details(monkeypatch):
    import deploy_preflight as dp
    import release_signing as rs
    monkeypatch.setenv("RELEASE_SIGNER", "local")
    monkeypatch.delenv("RELEASE_SIGNER_URL", raising=False)
    row = [c for c in dp.run_preflight(True)["checks"] if c["id"] == "release_signer_health"][0]
    assert row["signer"]["mode"] == "local" and row["status"] in ("warn", "fail")
    import base64
    pub = base64.b64encode(b"\x02" * 32).decode()
    for k, v in {"RELEASE_SIGNER": "external", "RELEASE_SIGNER_URL": "https://signer.example",
                 "RELEASE_SIGNER_ALLOWED_HOSTS": "signer.example", "RELEASE_SIGNER_TOKEN": "t" * 20,
                 "RELEASE_SIGNER_KEY_ID": rs.KEY_ID, "RELEASE_PUBLIC_KEY_B64": pub,
                 "RELEASE_SIGNER_TIMEOUT": "5"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
    monkeypatch.setattr(rs, "signer_health", lambda env: {
        "ok": True, "key_id": rs.KEY_ID, "remote_key_id": rs.KEY_ID, "identity_matches": True})
    monkeypatch.setattr(dp, "signer_health", rs.signer_health, raising=False)
    row = [c for c in dp.run_preflight(True)["checks"] if c["id"] == "release_signer_health"][0]
    assert row["status"] == "pass"
    assert row["signer"]["host"] == "signer.example" and row["signer"]["identity_matches"] is True
    assert row["signer"]["pinned_public_key_prefix"] == pub[:12] and row["signer"]["latency_ms"] is not None
