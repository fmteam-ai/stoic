"""impr-auth hardening: access-token revocation watermark, bridge-token
hashing (+ plaintext fallback toggle + migration), 2FA enrolment re-auth,
passkey_enroll TOTP-minted step-up, must_change_password allow-list, legacy
refresh rejection, suspension side effects. Pure mocks — no Mongo."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

pytestmark = pytest.mark.unit

UID = str(ObjectId())


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-" + "x" * 32)
    monkeypatch.delenv("STEP_UP_BYPASS_TOKEN", raising=False)
    monkeypatch.delenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", raising=False)


# ------------------------------------------------------------ tiny fake DB
def _get(doc, path):
    cur = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None, False
        cur = cur[p]
    return cur, True


def _match(doc, q):
    for k, cond in q.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
            continue
        val, present = _get(doc, k)
        if isinstance(cond, dict) and any(str(x).startswith("$") for x in cond):
            for op, arg in cond.items():
                if op == "$exists" and bool(arg) != present:
                    return False
                if op == "$gt" and not (present and val is not None and val > arg):
                    return False
                if op == "$in" and val not in arg:
                    return False
        elif val != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.indexes = {"_id_": {}}

    async def find_one(self, q, projection=None):
        for d in self.docs:
            if _match(d, q):
                return dict(d)
        return None

    def find(self, q=None):
        return _Cursor([dict(d) for d in self.docs if _match(d, q or {})])

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return MagicMock(inserted_id=doc["_id"])

    def _apply(self, d, upd):
        for k, v in (upd.get("$set") or {}).items():
            d[k] = v
        for k in (upd.get("$unset") or {}):
            d.pop(k, None)
        for k, v in (upd.get("$max") or {}).items():
            d[k] = max(d.get(k) or v, v)

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs:
            if _match(d, q):
                self._apply(d, upd)
                return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)

    async def update_many(self, q, upd):
        n = 0
        for d in self.docs:
            if _match(d, q):
                self._apply(d, upd)
                n += 1
        return MagicMock(matched_count=n, modified_count=n)

    async def find_one_and_update(self, q, upd, **kw):
        for d in self.docs:
            if _match(d, q):
                before = dict(d)
                self._apply(d, upd)
                return before
        return None

    async def create_index(self, key, name=None, **kw):
        self.indexes[name or f"{key}_1"] = kw
        return name

    async def index_information(self):
        return dict(self.indexes)

    async def drop_index(self, name):
        self.indexes.pop(name)


class FakeDB:
    def __init__(self, **colls):
        self._c = {k: (v if isinstance(v, FakeColl) else FakeColl(v))
                   for k, v in colls.items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, FakeColl())

    def __getitem__(self, name):
        return getattr(self, name)


class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _Url:
    def __init__(self, path):
        self.path = path


class _Req:
    def __init__(self, token=None, path="/api/accounts", headers=None):
        self.cookies = {"access_token": token} if token else {}
        self.headers = _Headers({k.lower(): v for k, v in (headers or {}).items()})
        self.url = _Url(path)
        self.client = None


# ------------------------------------------- 1. access-token revocation
def test_access_token_carries_iat_and_watermark_compare():
    import auth
    payload = auth.decode_token(auth.create_access_token(UID, "a@b.c"))
    assert isinstance(payload["iat"], int)
    iat = payload["iat"]
    assert not auth.access_token_revoked(payload, {})                    # no watermark
    assert not auth.access_token_revoked(payload, {"tokens_valid_after": iat})
    # 1s clock-skew grace
    assert not auth.access_token_revoked(payload, {"tokens_valid_after": iat + 1})
    assert auth.access_token_revoked(payload, {"tokens_valid_after": iat + 2})
    # legacy token without iat dies as soon as a watermark exists
    assert auth.access_token_revoked({"sub": UID}, {"tokens_valid_after": 5})


def test_get_current_user_rejects_revoked_and_accepts_fresh():
    import auth
    oid = ObjectId(UID)
    tok = auth.create_access_token(UID, "a@b.c")
    iat = auth.decode_token(tok)["iat"]
    db = FakeDB(users=[{"_id": oid, "email": "a@b.c", "role": "user",
                        "tokens_valid_after": iat + 10}])
    with patch.object(auth, "get_db", return_value=db):
        with pytest.raises(HTTPException) as e:
            _run(auth.get_current_user(_Req(tok)))
        assert e.value.status_code == 401 and e.value.detail == "Token revoked"
        db.users.docs[0]["tokens_valid_after"] = iat
        u = _run(auth.get_current_user(_Req(tok)))
        assert u["id"] == UID and "password_hash" not in u


def test_revoke_all_user_sessions_stamps_watermark():
    from security import revoke_all_user_sessions
    oid = ObjectId(UID)
    db = FakeDB(users=[{"_id": oid}],
                auth_sessions=[{"user_id": UID, "revoked": False}])
    before = int(time.time())
    n = _run(revoke_all_user_sessions(db, UID, "user_requested"))
    assert n == 1 and db.auth_sessions.docs[0]["revoked"] is True
    assert db.users.docs[0]["tokens_valid_after"] >= before


def test_ws_helper_rejects_suspended_revoked_and_accepts_valid():
    import auth
    oid = ObjectId(UID)
    tok = auth.create_access_token(UID, "a@b.c")
    iat = auth.decode_token(tok)["iat"]
    db = FakeDB(users=[{"_id": oid, "role": "user", "status": "suspended"}])
    with patch.object(auth, "get_db", return_value=db):
        assert _run(auth.authenticate_ws_token(tok)) is None
        db.users.docs[0]["status"] = "active"
        db.users.docs[0]["tokens_valid_after"] = iat + 10
        assert _run(auth.authenticate_ws_token(tok)) is None
        db.users.docs[0]["tokens_valid_after"] = iat
        assert _run(auth.authenticate_ws_token(tok)) == UID
        assert _run(auth.authenticate_ws_token("garbage")) is None


def test_admin_suspend_revokes_sessions_tokens_and_api_keys():
    import routes.admin_routes as ar
    oid = ObjectId(UID)
    db = FakeDB(users=[{"_id": oid, "email": "u@x", "role": "user"}],
                auth_sessions=[{"user_id": UID, "revoked": False}],
                api_keys=[{"user_id": UID, "revoked_at": None},
                          {"user_id": "other", "revoked_at": None}])
    _run(ar._set_user_status(db, user_id=UID, new_status="suspended",
                             reason="tos", actor_email="admin@x"))
    assert db.api_keys.docs[0]["revoked_at"] and db.api_keys.docs[1]["revoked_at"] is None
    assert db.auth_sessions.docs[0]["revoked"] is True
    assert db.users.docs[0]["tokens_valid_after"] > 0


# ------------------------------------------------- 2. bridge token hashing
def _bridge_db(acc):
    return FakeDB(accounts=[acc])


def test_bridge_lookup_by_hash():
    import routes.bridge_routes as br
    from auth import hash_bridge_token
    tok = "tok-abc-123"
    db = _bridge_db({"_id": ObjectId(), "bridge_token_hash": hash_bridge_token(tok)})
    with patch.object(br, "get_db", return_value=db):
        acc = _run(br._account_by_token(tok))
        assert acc["bridge_token_hash"] == hash_bridge_token(tok)
        with pytest.raises(HTTPException):
            _run(br._account_by_token("wrong"))


def test_bridge_plaintext_fallback_toggle_and_lazy_backfill(monkeypatch):
    import routes.bridge_routes as br
    from auth import hash_bridge_token
    tok = "legacy-plain-token"
    db = _bridge_db({"_id": ObjectId(), "bridge_token": tok})
    with patch.object(br, "get_db", return_value=db):
        monkeypatch.setenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", "false")
        with pytest.raises(HTTPException):
            _run(br._account_by_token(tok))
        monkeypatch.setenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", "true")
        assert _run(br._account_by_token(tok))
        assert db.accounts.docs[0]["bridge_token_hash"] == hash_bridge_token(tok)
        # after the back-fill the hash path works even with fallback off
        monkeypatch.setenv("BRIDGE_TOKEN_PLAINTEXT_FALLBACK", "false")
        assert _run(br._account_by_token(tok))


def test_bridge_stale_hash_is_not_honoured():
    """A legacy writer rotated only the plaintext: the old token's hash must
    stop working."""
    import routes.bridge_routes as br
    from auth import hash_bridge_token
    db = _bridge_db({"_id": ObjectId(), "bridge_token": "new-token",
                     "bridge_token_hash": hash_bridge_token("old-token")})
    with patch.object(br, "get_db", return_value=db):
        with pytest.raises(HTTPException):
            _run(br._account_by_token("old-token"))
        assert _run(br._account_by_token("new-token"))


def test_bridge_prev_hash_grace():
    import routes.bridge_routes as br
    from auth import hash_bridge_token
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    db = _bridge_db({"_id": ObjectId(), "bridge_token_hash": hash_bridge_token("cur"),
                     "bridge_token_prev_hash": hash_bridge_token("prev"),
                     "bridge_token_prev_expires": future})
    with patch.object(br, "get_db", return_value=db):
        assert _run(br._account_by_token("prev"))


def test_migration_hashes_strips_plaintext_and_is_idempotent():
    from migrations import hash_bridge_tokens as mig
    from auth import hash_bridge_token
    db = FakeDB(accounts=[
        {"_id": 1, "bridge_token": "t-one", "bridge_token_prev": "t-old"},
        {"_id": 2, "bridge_token": "t-two"},
        {"_id": 3, "bridge_token_hash": hash_bridge_token("t-three")},
    ])
    db.accounts.indexes["bridge_token_1"] = {"unique": True}
    s1 = _run(mig.migrate(db))
    a1, a2, a3 = db.accounts.docs
    assert a1["bridge_token_hash"] == hash_bridge_token("t-one")
    assert a1["bridge_token_prev_hash"] == hash_bridge_token("t-old")
    assert a1["bridge_token_last4"] == "-one"
    assert "bridge_token" not in a1 and "bridge_token_prev" not in a1
    assert a2["bridge_token_hash"] == hash_bridge_token("t-two") and "bridge_token" not in a2
    assert a3 == {"_id": 3, "bridge_token_hash": hash_bridge_token("t-three")}
    assert s1["hashed"] == 2 and s1["stripped"] == 2 and s1["index_dropped"] is True
    assert "bridge_token_1" not in db.accounts.indexes
    assert db.accounts.indexes["bridge_token_hash_1"]["unique"] is True
    snapshot = [dict(d) for d in db.accounts.docs]
    s2 = _run(mig.migrate(db))
    assert s2["scanned"] == 0 and db.accounts.docs == snapshot


def test_migration_hash_only_keeps_plaintext():
    from migrations import hash_bridge_tokens as mig
    db = FakeDB(accounts=[{"_id": 1, "bridge_token": "t-one"}])
    _run(mig.migrate(db, drop_plaintext=False))
    assert db.accounts.docs[0]["bridge_token"] == "t-one"
    assert db.accounts.docs[0]["bridge_token_hash"]


# --------------------------------------------- 3. 2FA enrolment re-auth
def _auth_routes_patches(ar, db):
    return (patch.object(ar, "get_db", return_value=db),
            patch.object(ar, "check_failure_limit", AsyncMock()),
            patch.object(ar, "record_failure", AsyncMock()),
            patch.object(ar, "clear_failures", AsyncMock()))


def test_enroll_requires_current_password():
    import routes.auth_routes as ar
    from auth import hash_password
    oid = ObjectId(UID)
    db = FakeDB(users=[{"_id": oid, "email": "a@b.c",
                        "password_hash": hash_password("right-pw")}])
    p1, p2, p3, p4 = _auth_routes_patches(ar, db)
    with p1, p2, p3 as rf, p4 as cf:
        for body in (None, ar.TwoFAEnrollRequest(),
                     ar.TwoFAEnrollRequest(current_password="wrong")):
            with pytest.raises(HTTPException) as e:
                _run(ar.two_fa_enroll(body, user={"id": UID}))
            assert e.value.status_code == 401
        assert rf.await_count == 3
        assert "totp_secret_pending" not in db.users.docs[0]
        out = _run(ar.two_fa_enroll(ar.TwoFAEnrollRequest(current_password="right-pw"),
                                    user={"id": UID}))
        assert out["secret"] and cf.await_count == 1
        assert db.users.docs[0]["totp_secret_pending"] == out["secret"]
        # verify-enroll also demands the password
        with pytest.raises(HTTPException) as e:
            _run(ar.two_fa_verify_enroll(
                ar.TwoFAVerifyEnrollRequest(code="123456"), user={"id": UID}))
        assert e.value.status_code == 401


def test_enroll_passwordless_account_skips_reauth():
    import routes.auth_routes as ar
    oid = ObjectId(UID)
    db = FakeDB(users=[{"_id": oid, "email": "a@b.c"}])
    p1, p2, p3, p4 = _auth_routes_patches(ar, db)
    with p1, p2, p3, p4:
        assert _run(ar.two_fa_enroll(None, user={"id": UID}))["secret"]


# -------------------------------------------- 4. passkey_enroll step-up
def test_passkey_enroll_action_registered_with_short_ttl():
    from step_up import STEP_UP_ACTIONS, step_up_ttl, STEP_UP_TTL_SECONDS
    assert "passkey_enroll" in STEP_UP_ACTIONS
    assert 0 < step_up_ttl("passkey_enroll") <= STEP_UP_TTL_SECONDS


def test_passkey_enroll_requires_totp_minted_step_up():
    import step_up
    oid = ObjectId(UID)
    db = FakeDB(users=[{"_id": oid, "two_factor_enabled": True}])
    user = {"id": UID}
    wa = _run(step_up.issue_step_up_token(db, UID, "passkey_enroll", method="webauthn"))
    assert db.step_up_tokens.docs[-1]["method"] == "webauthn"
    req = _Req(headers={"X-Step-Up-Token": wa["step_up_token"]})
    with pytest.raises(HTTPException) as e:
        _run(step_up.require_step_up(db, user, req, "passkey_enroll",
                                     required_method="totp"))
    assert e.value.detail["code"] == "step_up_invalid"
    tt = _run(step_up.issue_step_up_token(db, UID, "passkey_enroll", method="totp"))
    req = _Req(headers={"X-Step-Up-Token": tt["step_up_token"]})
    _run(step_up.require_step_up(db, user, req, "passkey_enroll",
                                 required_method="totp"))
    # single-use
    with pytest.raises(HTTPException):
        _run(step_up.require_step_up(db, user, req, "passkey_enroll",
                                     required_method="totp"))


def test_enrolment_proof_uses_passkey_enroll_and_pins_totp():
    import routes.webauthn_routes as wr
    db = FakeDB(users=[{"_id": ObjectId(UID), "two_factor_enabled": True}])
    su = AsyncMock()
    with patch("step_up.require_step_up", su):
        _run(wr._require_enrolment_proof(db, {"id": UID}, _Req(), {}))
    assert su.await_args.args[3] == "passkey_enroll"
    assert su.await_args.kwargs.get("required_method") == "totp"
    # passkey-only user (no TOTP): a passkey-minted token is acceptable
    db = FakeDB(users=[{"_id": ObjectId(UID)}],
                webauthn_credentials=[{"user_id": UID}])
    db.webauthn_credentials.count_documents = AsyncMock(return_value=1)
    su = AsyncMock()
    with patch("step_up.require_step_up", su):
        _run(wr._require_enrolment_proof(db, {"id": UID}, _Req(), {}))
    assert su.await_args.args[3] == "passkey_enroll"
    assert not su.await_args.kwargs.get("required_method")


# ------------------------------------------ 5. must_change_password
@pytest.mark.parametrize("path,allowed", [
    ("/api/auth/me", True), ("/api/auth/change-password", True),
    ("/api/auth/logout", True), ("/api/accounts", False),
    ("/api/auth/2fa/enroll", False), ("/api/auth/me/../x", False)])
def test_must_change_password_allow_list(path, allowed):
    import auth
    db = FakeDB(users=[{"_id": ObjectId(UID), "email": "a@b.c", "role": "admin",
                        "must_change_password": True}])
    tok = auth.create_access_token(UID, "a@b.c")
    with patch.object(auth, "get_db", return_value=db):
        if allowed:
            assert _run(auth.get_current_user(_Req(tok, path)))["id"] == UID
        else:
            with pytest.raises(HTTPException) as e:
                _run(auth.get_current_user(_Req(tok, path)))
            assert e.value.status_code == 403
            assert e.value.detail["code"] == "password_change_required"


# ------------------------------------------------ 6. legacy refresh
def test_legacy_refresh_without_jti_rejected():
    from security import consume_and_rotate
    db = FakeDB()
    with pytest.raises(HTTPException) as e:
        _run(consume_and_rotate(db, {"sub": UID, "type": "refresh"}, "tok"))
    assert e.value.status_code == 401


def test_refresh_endpoint_rejects_legacy_token():
    import auth
    import routes.auth_routes as ar
    legacy = auth.create_refresh_token(UID)          # no jti/sid/fam claims
    db = FakeDB(users=[{"_id": ObjectId(UID), "email": "a@b.c"}])
    req = MagicMock()
    req.cookies = {"refresh_token": legacy}
    with patch.object(ar, "get_db", return_value=db), \
         patch.object(ar, "rate_limit", AsyncMock()), \
         patch.object(ar, "create_session", AsyncMock()) as cs:
        with pytest.raises(HTTPException) as e:
            _run(ar.refresh_token(req, MagicMock()))
    assert e.value.status_code == 401
    cs.assert_not_awaited()
