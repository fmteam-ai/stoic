"""Audit v2 P2-01 — bridge-token rotation requires a fresh proof.

Users WITHOUT TOTP/passkey must send `current_password` (lockout scope
"bridge_rotate", 5 failures / 10 min); MFA users keep the step-up gate.
Every rotation is audit-logged. Pure fakes — no Mongo."""
import asyncio
import copy
import hashlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

pytestmark = pytest.mark.unit

UID = str(ObjectId())
AID = str(ObjectId())
PW = "correct horse battery staple"


def _run(coro):
    return asyncio.run(coro)


class _Res:
    def __init__(self, n=1):
        self.modified_count = n
        self.matched_count = n


class Coll:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def find_one(self, q, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return copy.deepcopy(d)
        return None

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                break
        else:
            if not upsert:
                return _Res(0)
            d = dict(q)
            d.update(upd.get("$setOnInsert", {}))
            self.docs.append(d)
        d.update(upd.get("$set", {}))
        for k in upd.get("$unset", {}):
            d.pop(k, None)
        for k, v in upd.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v
        return _Res(1)

    async def delete_many(self, q):
        rx = re.compile(q["_id"]["$regex"])
        self.docs = [d for d in self.docs if not rx.search(str(d["_id"]))]

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def count_documents(self, q, limit=None):
        return 0


def _db(with_password=True, mfa=False):
    from auth import bridge_token_fields, hash_password
    user = {"_id": ObjectId(UID), "email": "a@b.c", "two_factor_enabled": mfa}
    if with_password:
        user["password_hash"] = hash_password(PW)
    db = MagicMock()
    db.users = Coll([user])
    db.accounts = Coll([{"_id": ObjectId(AID), "user_id": UID,
                         **bridge_token_fields("old-token")}])
    db.rate_limits = Coll()
    db.audit_log = Coll()
    db.webauthn_credentials = Coll()
    return db


def _req(body):
    r = MagicMock()
    if body is None:
        r.json = AsyncMock(side_effect=ValueError("no body"))
    else:
        r.json = AsyncMock(return_value=body)
    r.headers = {}
    r.client = MagicMock(host="1.2.3.4")
    return r


def _rotate(db, body, has_passkey=False, step_up=None):
    import routes.account_routes as ar
    with patch.object(ar, "get_db", return_value=db), \
         patch("webauthn_mfa.has_passkey", AsyncMock(return_value=has_passkey)), \
         patch("step_up.require_step_up", step_up or AsyncMock()), \
         patch("security.client_ip", return_value="1.2.3.4"):
        return _run(ar.rotate_token(AID, _req(body), {"id": UID}))


def _failures(db):
    return sum(int(d.get("n") or 0) for d in db.rate_limits.docs
               if str(d["_id"]).startswith("bridge_rotate:"))


def test_no_mfa_without_password_refused_and_not_counted():
    db = _db()
    for body in (None, {}, {"current_password": ""}):
        with pytest.raises(HTTPException) as e:
            _rotate(db, body)
        assert e.value.status_code == 401
        assert e.value.detail["code"] == "password_required"
    assert _failures(db) == 0
    assert db.accounts.docs[0]["bridge_token_hash"] == hashlib.sha256(b"old-token").hexdigest()


def test_wrong_password_refused_counted_and_audited():
    db = _db()
    with pytest.raises(HTTPException) as e:
        _rotate(db, {"current_password": "nope"})
    assert e.value.status_code == 401
    assert e.value.detail["code"] == "password_required"
    assert _failures(db) == 1
    assert any(a["action"] == "bridge_token_rotate_failed" for a in db.audit_log.docs)
    assert db.accounts.docs[0]["bridge_token_hash"] == hashlib.sha256(b"old-token").hexdigest()


def test_five_failures_lock_out_even_correct_password():
    db = _db()
    for _ in range(5):
        with pytest.raises(HTTPException):
            _rotate(db, {"current_password": "nope"})
    with pytest.raises(HTTPException) as e:
        _rotate(db, {"current_password": PW})
    assert e.value.status_code == 429
    assert db.accounts.docs[0]["bridge_token_hash"] == hashlib.sha256(b"old-token").hexdigest()


def test_correct_password_rotates_hashes_and_audits():
    db = _db()
    with pytest.raises(HTTPException):
        _rotate(db, {"current_password": "nope"})
    out = _rotate(db, {"current_password": PW})
    tok = out["bridge_token"]
    acc = db.accounts.docs[0]
    assert acc["bridge_token_hash"] == hashlib.sha256(tok.encode()).hexdigest()
    assert acc["bridge_token_last4"] == tok[-4:]
    assert acc["bridge_token_prev_hash"] == hashlib.sha256(b"old-token").hexdigest()
    assert "bridge_token" not in acc and "bridge_token_prev" not in acc
    assert out["prev_token_grace_until"]
    assert _failures(db) == 0                                  # cleared on success
    ev = [a for a in db.audit_log.docs if a["action"] == "bridge_token_rotated"]
    assert len(ev) == 1 and ev[0]["detail"]["method"] == "password"
    assert ev[0]["detail"]["account_id"] == AID
    assert tok not in str(ev[0])                               # no plaintext in audit


def test_mfa_user_still_requires_step_up_and_password_is_not_enough():
    db = _db(mfa=True)
    gate = AsyncMock(side_effect=HTTPException(
        status_code=403, detail={"code": "step_up_required"}))
    with pytest.raises(HTTPException) as e:
        _rotate(db, {"current_password": PW}, step_up=gate)
    assert e.value.status_code == 403 and e.value.detail["code"] == "step_up_required"
    gate.assert_awaited_once()
    assert gate.await_args.args[3] == "bridge_token_rotate"


def test_passkey_user_uses_step_up_and_rotation_is_audited():
    db = _db()
    gate = AsyncMock()
    out = _rotate(db, None, has_passkey=True, step_up=gate)
    gate.assert_awaited_once()
    assert out["bridge_token"]
    ev = [a for a in db.audit_log.docs if a["action"] == "bridge_token_rotated"]
    assert ev and ev[0]["detail"]["method"] == "step_up" and ev[0]["step_up_verified"]
