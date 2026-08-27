"""iter-136 — security hardening: mandatory admin TOTP MFA, hash-chained
audit log, bridge-token rotation grace, Ed25519 release signing, runbooks."""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ---------------------------------------------------------------- admin MFA

def test_require_admin_role_and_mfa(monkeypatch):
    from auth import require_admin
    with pytest.raises(HTTPException) as e:
        require_admin({"role": "user"})
    assert e.value.status_code == 403

    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    with pytest.raises(HTTPException) as e:
        require_admin({"role": "admin", "two_factor_enabled": False})
    assert e.value.detail["code"] == "admin_mfa_required"
    # enrolled admin passes
    require_admin({"role": "admin", "two_factor_enabled": True})

    # preview/CI escape hatch
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "false")
    require_admin({"role": "admin", "two_factor_enabled": False})


def test_all_admin_gates_use_require_admin():
    import inspect
    from routes import (admin_routes, affiliate_routes, migration_routes,
                        diagnostic_routes, partner_routes, panic_routes,
                        support_routes)
    for mod, fn in ((admin_routes, "_admin_only"),
                    (affiliate_routes, "_admin_only"),
                    (migration_routes, "_admin_only"),
                    (diagnostic_routes, "_admin_only"),
                    (partner_routes, "_require_admin"),
                    (panic_routes, "panic_global"),
                    (support_routes, "admin_tickets")):
        assert "require_admin" in inspect.getsource(getattr(mod, fn)), \
            f"{mod.__name__}.{fn} does not use require_admin"


# ---------------------------------------------------------------- audit chain

def test_audit_chain_append_and_verify():
    from audit_chain import append_chained, verify_chain
    db = _db()
    col = "test_audit_chain_iter136"

    async def _t():
        await db[col].delete_many({})
        try:
            for i in range(3):
                await append_chained(db, {"action": f"a{i}", "actor": "t"},
                                     collection=col)
            out = await verify_chain(db, collection=col)
            assert out["ok"] and out["chained_entries"] == 3

            # tamper with entry 2 → detected
            await db[col].update_one({"seq": 2},
                                     {"$set": {"action": "EVIL"}})
            out2 = await verify_chain(db, collection=col)
            assert not out2["ok"]
            assert any(a["seq"] == 2 for a in out2["anomalies"])

            # deleting an entry breaks the link
            await db[col].update_one({"seq": 2},
                                     {"$set": {"action": "a1"}})  # restore
            await db[col].delete_one({"seq": 2})
            out3 = await verify_chain(db, collection=col)
            assert not out3["ok"]
        finally:
            await db[col].drop()
    _run(_t())


def test_admin_audit_writer_is_chained():
    import inspect
    from routes import admin_routes
    assert "append_chained" in inspect.getsource(admin_routes._audit)


# ---------------------------------------------------------------- signing

def test_ed25519_sign_verify_roundtrip():
    import release_signing
    body = b"artifact-manifest-body"
    sig = release_signing.sign_hex(body)
    pub = release_signing.public_key_b64()
    assert release_signing.verify_hex(body, sig, pub)
    assert not release_signing.verify_hex(body + b"!", sig, pub)
    assert not release_signing.verify_hex(body, "00" * 64, pub)


def test_manifest_signed_with_ed25519():
    from vps_pathb import build_artifact_manifest
    import release_signing, json
    m = build_artifact_manifest()
    sig = m["signature"]
    assert sig["alg"] == "Ed25519"
    body = json.dumps({k: m[k] for k in sig["signed_fields"]},
                      sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    assert release_signing.verify_hex(body, sig["value"],
                                      sig["public_key_b64"])


# ---------------------------------------------------------------- rotation grace

def test_bridge_token_rotation_grace():
    from routes.account_routes import rotate_token
    from routes.bridge_routes import _account_by_token
    from bson import ObjectId
    db = _db()

    async def _t():
        uid = str(ObjectId())
        acc_id = ObjectId()
        await db.accounts.insert_one({"_id": acc_id, "user_id": uid,
                                      "bridge_token": "old_tok_136",
                                      "display_name": "t"})
        try:
            out = await rotate_token(str(acc_id), user={"id": uid})
            new_tok = out["bridge_token"]
            assert new_tok != "old_tok_136"
            # both tokens resolve during grace (prev lookup first — the
            # first NEW-token use retires the old one)
            assert (await _account_by_token("old_tok_136"))["_id"] == acc_id
            assert (await _account_by_token(new_tok))["_id"] == acc_id
            # new-token use retired the previous token immediately
            with pytest.raises(HTTPException):
                await _account_by_token("old_tok_136")
            # re-arm grace to verify the expiry path too
            await db.accounts.update_one(
                {"_id": acc_id},
                {"$set": {"bridge_token_prev": "old_tok_136",
                          "bridge_token_prev_expires": (
                              datetime.now(timezone.utc)
                              + timedelta(minutes=15)).isoformat()}})
            assert (await _account_by_token("old_tok_136"))["_id"] == acc_id
            # expire the grace → old token dies
            await db.accounts.update_one(
                {"_id": acc_id},
                {"$set": {"bridge_token_prev_expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()}})
            with pytest.raises(HTTPException):
                await _account_by_token("old_tok_136")
        finally:
            await db.accounts.delete_one({"_id": acc_id})
    _run(_t())


# ---------------------------------------------------------------- runbooks

def test_runbooks_content():
    from runbooks_content import get_runbooks
    out = get_runbooks()
    ids = {r["id"] for r in out["runbooks"]}
    assert ids == {"incident-response", "backup-restore", "supply-chain"}
    joined = " ".join(r["markdown"] for r in out["runbooks"])
    for marker in ("SEV-1", "Secret rotation", "Restore drill",
                   "Platform responsibilities", "audit chain"):
        assert marker.lower() in joined.lower(), marker


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
