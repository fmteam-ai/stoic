"""Refresh-token rotation — concurrent refresh must NOT log the user out.

Two tabs whose 30-min access tokens expire together both call /auth/refresh with
the same cookie. The second presents the token the first just consumed: inside the
grace window (same hash) the chain continues from the newest successor; a real
replay (hash mismatch / outside grace / revoked successor) still kills the family.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from conftest import run_async  # noqa: E402
from database import get_db  # noqa: E402
from security import consume_and_rotate, hash_token, stamp_session_token  # noqa: E402


def _seed(db, jti, token, **extra):
    now = datetime.now(timezone.utc).isoformat()
    doc = {"jti": jti, "session_id": f"sid-{jti}", "family": f"fam-{jti}", "user_id": "u-refresh",
           "token_hash": hash_token(token), "created_at": now, "last_used_at": now,
           "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
           "revoked": False, "consumed": False, "replaced_by_jti": None, **extra}
    return db.auth_sessions.insert_one(doc)


def test_concurrent_refresh_within_grace_continues_chain_instead_of_revoking():
    db = get_db(); jti = f"grace-{os.urandom(6).hex()}"; tok = f"tok-{jti}"

    async def go():
        await _seed(db, jti, tok)
        first = await consume_and_rotate(db, {"jti": jti}, tok)          # tab A rotates
        await stamp_session_token(db, first["jti"], f"tok-{first['jti']}")
        second = await consume_and_rotate(db, {"jti": jti}, tok)         # tab B re-presents the consumed token
        assert second["jti"] not in (jti, first["jti"]) and second["fam"] == first["fam"]
        fam = [d async for d in db.auth_sessions.find({"family": f"fam-{jti}"})]
        assert all(not d["revoked"] for d in fam), "family must stay alive"
        assert sum(1 for d in fam if not d["consumed"]) == 1                 # exactly one live head
        head = next(d for d in fam if not d["consumed"]); assert head["jti"] == second["jti"]
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    run_async(go())


def test_replay_with_wrong_token_still_revokes_family():
    db = get_db(); jti = f"replay-{os.urandom(6).hex()}"; tok = f"tok-{jti}"

    async def go():
        await _seed(db, jti, tok)
        await consume_and_rotate(db, {"jti": jti}, tok)
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": jti}, "stolen-or-forged-token")
        assert e.value.status_code == 401 and "reuse" in str(e.value.detail)
        assert all([d["revoked"] async for d in db.auth_sessions.find({"family": f"fam-{jti}"})])
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    run_async(go())


def test_replay_outside_grace_window_revokes_family(monkeypatch):
    import security
    monkeypatch.setattr(security, "REFRESH_REUSE_GRACE_SECONDS", 60)
    db = get_db(); jti = f"late-{os.urandom(6).hex()}"; tok = f"tok-{jti}"
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    async def go():
        await _seed(db, jti, tok, consumed=True, consumed_at=old, last_used_at=old, replaced_by_jti=f"{jti}-next")
        await _seed(db, f"{jti}-next", f"tok-{jti}-next", family=f"fam-{jti}")
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": jti}, tok)
        assert e.value.status_code == 401
        assert all([d["revoked"] async for d in db.auth_sessions.find({"family": f"fam-{jti}"})])
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    run_async(go())


def test_consume_is_atomic_no_duplicate_live_successors():
    import asyncio
    db = get_db(); jti = f"atomic-{os.urandom(6).hex()}"; tok = f"tok-{jti}"

    async def go():
        await _seed(db, jti, tok)
        results = await asyncio.gather(*[consume_and_rotate(db, {"jti": jti}, tok) for _ in range(4)],
                                       return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        assert ok, "at least one refresh must succeed"
        assert not any(isinstance(r, HTTPException) and "reuse" in str(r.detail) for r in results)
        fam = [d async for d in db.auth_sessions.find({"family": f"fam-{jti}"})]
        assert all(not d["revoked"] for d in fam)
        assert sum(1 for d in fam if not d["consumed"]) == 1
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    run_async(go())
