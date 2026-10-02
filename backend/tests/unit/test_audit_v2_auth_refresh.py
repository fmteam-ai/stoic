"""Audit v2 P1-01 — refresh-token replay grace no longer mints sessions.

Acceptance (pure, in-memory fake of auth_sessions — no Mongo):
  1. refresh R0 → R1 works.
  2. R0 replayed 1 s later → 409 refresh_superseded, NO new session doc,
     nothing revoked, R1 still usable.
     DELIBERATE DEVIATION from "replay ⇒ revoke": inside the short grace
     window (REFRESH_REUSE_GRACE_SECONDS, default 10 s) a same-hash re-present
     with a live chain head is refused WITHOUT minting rather than revoking,
     so a legitimate second tab racing the first is not logged out. The
     attacker gains nothing: no cookie, no session.
  3. outside the window → 401 + family revoked + R1 unusable.
  4. 20 concurrent R0 → exactly one success, the rest 409/401, at most one
     live successor.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

import security
from security import consume_and_rotate, hash_token, stamp_session_token

pytestmark = pytest.mark.unit

UID = str(ObjectId())


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ fake auth_sessions
def _match(doc, q):
    return all(doc.get(k) == v for k, v in q.items())


class _Res:
    def __init__(self, n):
        self.modified_count = n


class FakeSessions:
    """find_one / find_one_and_update (atomic per call, yields first so
    concurrent coroutines interleave like real I/O) / update_one /
    update_many / insert_one."""

    def __init__(self):
        self.docs = []
        self.inserts = 0

    async def find_one(self, q, projection=None):
        await asyncio.sleep(0)
        for d in self.docs:
            if _match(d, q):
                return copy.deepcopy(d)
        return None

    async def find_one_and_update(self, q, upd):
        await asyncio.sleep(0)
        for d in self.docs:            # no await between match and write → atomic
            if _match(d, q):
                before = copy.deepcopy(d)
                d.update(upd.get("$set", {}))
                return before
        return None

    async def update_one(self, q, upd):
        for d in self.docs:
            if _match(d, q):
                d.update(upd.get("$set", {}))
                return _Res(1)
        return _Res(0)

    async def update_many(self, q, upd):
        n = 0
        for d in self.docs:
            if _match(d, q):
                d.update(upd.get("$set", {}))
                n += 1
        return _Res(n)

    async def insert_one(self, doc):
        self.inserts += 1
        self.docs.append(copy.deepcopy(doc))


class FakeDB:
    def __init__(self):
        self.auth_sessions = FakeSessions()


def _seed(db, jti="j0", token="R0"):
    now = datetime.now(timezone.utc).isoformat()
    db.auth_sessions.docs.append({
        "jti": jti, "session_id": "sid", "family": "fam", "user_id": UID,
        "token_hash": hash_token(token), "created_at": now, "last_used_at": now,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "revoked": False, "consumed": False, "replaced_by_jti": None})


def _doc(db, jti):
    return next(d for d in db.auth_sessions.docs if d["jti"] == jti)


def _age_consume(db, jti, seconds):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    _doc(db, jti).update({"consumed_at": ts, "last_used_at": ts})


async def _rotate_r0(db):
    c1 = await consume_and_rotate(db, {"jti": "j0"}, "R0")
    await stamp_session_token(db, c1["jti"], "R1")
    return c1


@pytest.fixture(autouse=True)
def _grace(monkeypatch):
    monkeypatch.setattr(security, "REFRESH_REUSE_GRACE_SECONDS", 10)


def test_default_grace_is_10s_and_env_configurable():
    src = open(security.__file__).read()
    assert 'os.environ.get("REFRESH_REUSE_GRACE_SECONDS", "10")' in src


# 1 ------------------------------------------------------------------------
def test_refresh_rotates_r0_to_r1():
    db = FakeDB()
    _seed(db)
    c1 = _run(_rotate_r0(db))
    assert c1["jti"] != "j0" and c1["fam"] == "fam" and c1["sid"] == "sid"
    r0 = _doc(db, "j0")
    assert r0["consumed"] and r0["replaced_by_jti"] == c1["jti"]
    assert not _doc(db, c1["jti"])["consumed"]


# 2 ------------------------------------------------------------------------
def test_replay_inside_grace_is_409_without_minting_or_revoking():
    db = FakeDB()
    _seed(db)

    async def go():
        c1 = await _rotate_r0(db)
        _age_consume(db, "j0", 1)                       # replay 1 s later
        n_docs, n_inserts = len(db.auth_sessions.docs), db.auth_sessions.inserts
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": "j0"}, "R0")
        assert e.value.status_code == 409
        assert e.value.detail["code"] == "refresh_superseded"
        # NO new session doc, nothing revoked, chain untouched
        assert len(db.auth_sessions.docs) == n_docs
        assert db.auth_sessions.inserts == n_inserts
        assert not any(d["revoked"] for d in db.auth_sessions.docs)
        assert not _doc(db, c1["jti"])["consumed"]
        # R1 is still usable
        c2 = await consume_and_rotate(db, {"jti": c1["jti"]}, "R1")
        assert c2["jti"] not in ("j0", c1["jti"])
    _run(go())


# 3 ------------------------------------------------------------------------
def test_replay_outside_grace_revokes_family_and_kills_r1():
    db = FakeDB()
    _seed(db)

    async def go():
        c1 = await _rotate_r0(db)
        _age_consume(db, "j0", 30)                      # outside 10 s window
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": "j0"}, "R0")
        assert e.value.status_code == 401 and "reuse" in str(e.value.detail)
        assert all(d["revoked"] for d in db.auth_sessions.docs)
        with pytest.raises(HTTPException) as e2:
            await consume_and_rotate(db, {"jti": c1["jti"]}, "R1")
        assert e2.value.status_code == 401
    _run(go())


def test_replay_with_hash_mismatch_revokes_family():
    db = FakeDB()
    _seed(db)

    async def go():
        await _rotate_r0(db)
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": "j0"}, "forged-token")
        assert e.value.status_code == 401
        assert all(d["revoked"] for d in db.auth_sessions.docs)
    _run(go())


def test_replay_with_broken_chain_revokes_family():
    db = FakeDB()
    _seed(db)

    async def go():
        c1 = await _rotate_r0(db)
        _doc(db, c1["jti"])["revoked"] = True           # successor dead
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": "j0"}, "R0")
        assert e.value.status_code == 401
        assert all(d["revoked"] for d in db.auth_sessions.docs)
    _run(go())


def test_grace_zero_disables_window(monkeypatch):
    monkeypatch.setattr(security, "REFRESH_REUSE_GRACE_SECONDS", 0)
    db = FakeDB()
    _seed(db)

    async def go():
        await _rotate_r0(db)
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": "j0"}, "R0")
        assert e.value.status_code == 401
        assert all(d["revoked"] for d in db.auth_sessions.docs)
    _run(go())


def test_double_replay_inside_grace_never_mints():
    """Even repeated re-presents only ever see 409 — possession of a consumed
    token can never be converted into a session."""
    db = FakeDB()
    _seed(db)

    async def go():
        await _rotate_r0(db)
        before = db.auth_sessions.inserts
        for _ in range(5):
            with pytest.raises(HTTPException) as e:
                await consume_and_rotate(db, {"jti": "j0"}, "R0")
            assert e.value.status_code == 409
        assert db.auth_sessions.inserts == before
    _run(go())


# 4 ------------------------------------------------------------------------
def test_20_concurrent_r0_exactly_one_success():
    db = FakeDB()
    _seed(db)

    async def go():
        return await asyncio.gather(
            *[consume_and_rotate(db, {"jti": "j0"}, "R0") for _ in range(20)],
            return_exceptions=True)
    results = _run(go())
    ok = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if not isinstance(r, dict)]
    assert len(ok) == 1
    assert all(isinstance(e, HTTPException) and e.status_code in (409, 401)
               for e in errs)
    live = [d for d in db.auth_sessions.docs
            if not d["consumed"] and not d["revoked"]]
    assert len(live) <= 1
    assert db.auth_sessions.inserts == 1               # only the winner minted


def test_lost_race_returns_409_not_trusted_rotation():
    """find_one_and_update loses → 409 (no recursion into a rotation)."""
    db = FakeDB()
    _seed(db)
    real = db.auth_sessions.find_one_and_update

    async def racer(q, upd):
        # a parallel request consumes the same jti first
        await real(q, {"$set": {**upd["$set"], "replaced_by_jti": "other"}})
        return None
    db.auth_sessions.find_one_and_update = racer
    with pytest.raises(HTTPException) as e:
        _run(consume_and_rotate(db, {"jti": "j0"}, "R0"))
    assert e.value.status_code == 409
    assert db.auth_sessions.inserts == 0


# --------------------------------------------------------------- route layer
def test_refresh_route_returns_409_without_set_cookie(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-" + "x" * 32)
    import auth
    import routes.auth_routes as ar
    from starlette.responses import Response

    db = FakeDB()
    _seed(db)
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": ObjectId(UID), "email": "a@b.c"})
    db.users = users
    _run(_rotate_r0(db))
    r0 = auth.create_refresh_token(UID, {"jti": "j0", "sid": "sid", "fam": "fam"})
    _doc(db, "j0")["token_hash"] = hash_token(r0)
    req = MagicMock()
    req.cookies = {"refresh_token": r0}
    resp = Response()
    with patch.object(ar, "get_db", return_value=db), \
         patch.object(ar, "rate_limit", AsyncMock()):
        out = _run(ar.refresh_token(req, resp))
    assert out.status_code == 409
    assert b"refresh_superseded" in out.body
    assert "set-cookie" not in {k.lower() for k in out.headers.keys()}
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}
    assert not any(d["revoked"] for d in db.auth_sessions.docs)
