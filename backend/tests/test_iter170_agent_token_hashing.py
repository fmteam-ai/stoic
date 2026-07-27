"""iter-170 — host-agent tokens stored HASHED at rest (DB read alone can't
yield live credentials)."""
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def test_register_stores_hash_not_plaintext():
    import vps_agent
    db = _db()
    uid = f"iter170-{os.urandom(4).hex()}"

    async def scenario():
        bt = await vps_agent.create_bootstrap_token(db, uid, f"dep-{uid}")
        reg = await vps_agent.register_agent(db, bt["token"],
                                             {"machine_fingerprint": "fp"})
        tok = reg["agent_token"]
        doc = await db.vps_agents.find_one({"agent_id": reg["agent_id"]})
        try:
            return tok, doc, reg["agent_id"]
        finally:
            pass
    tok, doc, aid = _run(scenario())
    try:
        assert "agent_token" not in doc, "plaintext token stored at rest!"
        assert doc.get("agent_token_hash") == vps_agent.hash_agent_token(tok)
        # the hash is not the token, and the token isn't derivable from it
        assert doc["agent_token_hash"] != tok
        assert len(doc["agent_token_hash"]) == 64  # sha256 hex
    finally:
        _run(_db().vps_agents.delete_many({"user_id": tok and aid and
                                           doc["user_id"]}))


def test_auth_by_plaintext_and_reject_wrong():
    import vps_agent
    db = _db()
    uid = f"iter170-{os.urandom(4).hex()}"

    async def scenario():
        bt = await vps_agent.create_bootstrap_token(db, uid, f"dep-{uid}")
        reg = await vps_agent.register_agent(db, bt["token"],
                                             {"machine_fingerprint": "fp"})
        tok = reg["agent_token"]
        try:
            found = await vps_agent.agent_by_token(db, tok)
            rejected = False
            try:
                await vps_agent.agent_by_token(db, "agt_tok_not_real")
            except ValueError:
                rejected = True
            return found["agent_id"] == reg["agent_id"], rejected
        finally:
            await db.vps_agents.delete_many({"user_id": uid})
            await db.vps_bootstrap_tokens.delete_many({"user_id": uid})
    ok, rejected = _run(scenario())
    assert ok, "valid token did not authenticate"
    assert rejected, "wrong token was accepted"


def test_rotate_invalidates_old_token_and_stays_hashed():
    import vps_agent
    db = _db()
    uid = f"iter170-{os.urandom(4).hex()}"

    async def scenario():
        bt = await vps_agent.create_bootstrap_token(db, uid, f"dep-{uid}")
        reg = await vps_agent.register_agent(db, bt["token"],
                                             {"machine_fingerprint": "fp"})
        old = reg["agent_token"]
        rot = await vps_agent.rotate_agent_token(db, old)
        new = rot["agent_token"]
        doc = await db.vps_agents.find_one({"agent_id": reg["agent_id"]})
        old_dead = False
        try:
            await vps_agent.agent_by_token(db, old)
        except ValueError:
            old_dead = True
        new_ok = (await vps_agent.agent_by_token(db, new))["agent_id"] == \
            reg["agent_id"]
        try:
            return old_dead, new_ok, doc
        finally:
            await db.vps_agents.delete_many({"user_id": uid})
            await db.vps_bootstrap_tokens.delete_many({"user_id": uid})
    old_dead, new_ok, doc = _run(scenario())
    assert old_dead, "old token still valid after rotation"
    assert new_ok, "new token invalid after rotation"
    assert "agent_token" not in doc, "rotation left plaintext at rest"
    assert doc.get("agent_token_hash")


def test_legacy_plaintext_fallback_authenticates():
    """A not-yet-migrated agent (plaintext agent_token) must still auth during
    rollout (startup migration removes plaintext in prod)."""
    import vps_agent
    db = _db()
    aid = f"iter170-legacy-{os.urandom(4).hex()}"
    tok = f"agt_tok_legacy_{os.urandom(6).hex()}"

    async def scenario():
        await db.vps_agents.insert_one(
            {"agent_id": aid, "agent_token": tok, "revoked": False,
             "user_id": aid})
        try:
            return (await vps_agent.agent_by_token(db, tok))["agent_id"]
        finally:
            await db.vps_agents.delete_many({"agent_id": aid})
    assert _run(scenario()) == aid
