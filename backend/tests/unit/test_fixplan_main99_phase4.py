"""main99 review — Phase 4 (before real money, code half): N99-2 broker demo flag only from an accepted
EX5, A14-9 approved inventory inside the bundle, A14-10 Ed25519 bundle signature, A14-11 fingerprint expiry."""
import asyncio
import base64
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def signer_env() -> dict:
    """Local Ed25519 signer for tests (production refuses local signing by design)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "ED25519_SIGNING_KEY_B64": priv, "RELEASE_PUBLIC_KEY_B64": pub,
            "RELEASE_SIGNER_KEY_ID": "test-key-1", "LEDGER_ANCHOR_KEY": "k" * 32}


def approve_inventory(db, *account_ids, bots=None):
    db.platform_state.rows.append({"_id": "inventory_expectation", "accounts": len(account_ids), "enabled": len(account_ids),
                                   "bots": len(account_ids) if bots is None else bots,
                                   "account_ids": [str(a) for a in account_ids], "approved_by": "approver@stoicaibot.com",
                                   "set_at": "2026-06-01T00:00:00+00:00", "policy_version": "v1"})


def _proj(configured, enabled, bots, violations=()):
    return {"counts": {"configured": configured, "live_enabled": enabled, "bots_enabled": bots},
            "structural_defects": [], "violations": list(violations), "inventory_hash": "h"}


# ── N99-2 ─────────────────────────────────────────────────────────────────────
def test_n99_2_broker_demo_flag_trusted_only_from_accepted_ex5():
    import broker_env as be
    acc = {"mode": "live", "ea_identity": {"authoritative": True, "trade_mode": "demo"}, "ea_binary_sha256": "a" * 64, "ea_binary_sha256_method": "installer_attested"}
    with patch("ea_capabilities.accepted_ea_sha256s", lambda: ["a" * 64]):
        assert be.broker_reports_demo(acc) is True
    with patch("ea_capabilities.accepted_ea_sha256s", lambda: ["b" * 64]):
        assert be.broker_reports_demo(acc) is False          # self-compiled EA saying "demo" is not evidence
    with patch("ea_capabilities.accepted_ea_sha256s", lambda: []):
        assert be.broker_reports_demo(acc) is False
    assert be.broker_reports_real({**acc, "ea_identity": {"authoritative": True, "trade_mode": "real"}})  # real still fail-closed


# ── A14-9 ─────────────────────────────────────────────────────────────────────
def test_a14_9_inventory_failures_exact_counts_orphans_duplicates_and_no_expectation():
    import acceptance_bundle as ab
    exp = {"accounts": 2, "enabled": 2, "bots": 2, "approved_by": "x"}
    assert ab.inventory_failures(_proj(2, 2, 2), exp) == []
    assert any("enabled bots 3" in f for f in ab.inventory_failures(_proj(2, 2, 3), exp))     # extra enabled bot → FAIL
    assert any("orphan" in f for f in ab.inventory_failures(_proj(2, 2, 2, ["1 orphan bot(s) without an account"]), exp))
    assert ab.inventory_failures(_proj(2, 2, 2), {}) == ["no approved inventory expectation — a bundle cannot be produced without one"]
    assert any("accounts 3" in f for f in ab.inventory_failures(_proj(3, 2, 2), exp))


def test_a14_9_a14_10_a14_11_bundle_build_sign_verify_and_fingerprint():
    import acceptance_bundle as ab
    db = FakeDb()
    acc_id = ObjectId()
    now = datetime.now(timezone.utc)
    db.accounts.rows.append({"_id": acc_id, "mode": "live", "trading_enabled": True, "label": "L1", "status": "active",
                             "last_heartbeat": now.isoformat(), "last_full_sync_at": now.isoformat(), "reconciliation_seq": 1,
                             "ea_identity": {"installation_id": "i", "broker_server": "B"}, "ea_version": "1.60"})
    db.reconciliation_ledger.rows.append({"account_id": str(acc_id), "discrepancy": 0.0, "period_to": "2026-06-01", "seq": 1})
    db.bot_configs.rows.append({"_id": ObjectId(), "account_id": str(acc_id), "user_id": "u", "risk_per_trade": 1.0})
    full = {"level": "FULL", "domains": {"release": {"level": "FULL", "reason": "ok"},
                                         "acceptance": {"level": "FULL", "reason": "ok"}}, "reasons": []}
    rel = {"build_sha": "deadbeef1", "image_digest": "sha256:abc"}
    # no approved expectation ⇒ no bundle
    with patch.dict(os.environ, signer_env()), patch("trading_authority.compute_authority", AsyncMock(return_value=full)), \
            patch("inventory_projection.projection", AsyncMock(return_value=_proj(1, 1, 1))), \
            patch.object(ab, "release_identity", lambda: rel):
        with pytest.raises(RuntimeError):
            run(ab.build_bundle(db, actor="admin"))
        approve_inventory(db, acc_id)
        b = run(ab.build_bundle(db, actor="admin"))
        assert b["verdict"] == "PASS", b["failures"]
        assert b["algo"] == "ed25519" and b["schema_version"] == 2 and b["key_id"] == "test-key-1"
        assert b["payload"]["inventory"]["approved_by"] == "approver@stoicaibot.com" and b["payload"]["inventory"]["expected"]["bots"] == 1
        assert ab.verify_signature(b)
        assert ab.verify_signature({**b, "config_fingerprint": "tampered"}) is False
        # the trading server cannot forge: a different key or a revoked key id is refused
        with patch.dict(os.environ, {"RELEASE_REVOKED_KEY_IDS": "test-key-1"}):
            assert ab.verify_signature(b) is False
        with patch.dict(os.environ, {"RELEASE_PUBLIC_KEY_B64": signer_env()["RELEASE_PUBLIC_KEY_B64"]}):
            assert ab.verify_signature(b) is False
        with patch.dict(os.environ, {"RELEASE_SIGNER_KEY_ID": "other-key"}):
            assert ab.verify_signature(b) is False
        # legacy HMAC (schema v1) bundles are refused
        assert ab.verify_signature({**b, "schema_version": 1}) is False
        # A14-11 — fingerprint: unchanged ⇒ covered; a risk setting change ⇒ close-only until a new bundle
        fp = run(ab.config_fingerprint(db, [str(acc_id)], rel))
        assert fp == b["config_fingerprint"]
        assert ab.bundle_covers(b, str(acc_id), rel, current_fingerprint=fp)[0] is True
        # N100-12 — runtime churn (bot pulse, heartbeat identity restamp) must NOT void coverage
        db.bot_configs.rows[0]["_last_pulse"] = "2026-10-06T15:00:00+00:00"
        db.bot_configs.rows[0]["_tick_lock_until"] = "2026-10-06T15:00:05+00:00"
        db.accounts.rows[0]["verified_identity"] = {"login": "123", "stamped_at": "now"}
        db.accounts.rows[0]["last_heartbeat"] = "2026-10-06T15:00:00+00:00"
        assert run(ab.config_fingerprint(db, [str(acc_id)], rel)) == fp
        db.bot_configs.rows[0]["risk_percent"] = 2.0
        fp2 = run(ab.config_fingerprint(db, [str(acc_id)], rel))
        ok, why = ab.bundle_covers(b, str(acc_id), rel, current_fingerprint=fp2)
        assert ok is False and "configuration changed" in why
        # an extra enabled bot gives FAIL
        with patch("inventory_projection.projection", AsyncMock(return_value=_proj(1, 1, 2))):
            b2 = run(ab.build_bundle(db, actor="admin"))
        assert b2["verdict"] == "FAIL" and any("enabled bots 2" in f for f in b2["failures"])
    src = open(ab.__file__, encoding="utf-8").read()
    assert "hmac" not in src.lower()   # no shared-secret signing left
    ta = open(os.path.join(os.path.dirname(ab.__file__), "trading_authority.py"), encoding="utf-8").read()
    assert "current_fingerprint=fp" in ta
