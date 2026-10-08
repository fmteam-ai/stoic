"""Audit r18 — P0-01 durable per-trade close_seq + terminal ack, P0-02 EA
capability gate (numeric versions, canonical authority), P1-01 transactions by
capability, P1-02 PANIC db-only transaction + post-commit outbox, P2-01 signed
pair admission, P2-02 notification retention incident, P2-03 replica-set
conflict (AT-P1-02; runs on the CI replica set, skips on standalone)."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))

READY = {"state": "READY", "new_exposure_allowed": True, "decision_id": "dec_test", "input_version": 1}


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())

    def _cleanup():
        for c in ("nl_proposals", "trades", "bot_configs", "nl_effects", "ops_outbox", "accounts"):
            _run(db[c].delete_many({"user_id": uid}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "db": db}


def _ctx(key, owner="w1", fence=1):
    return {"idempotency_key": key, "execution_id": "ex", "owner": owner, "fence": fence, "target_ids": None}


def _armed(db, uid, key, owner="w1", fence=1):
    import nl_execution as nx
    ctx = _ctx(key, owner, fence)
    assert _run(nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx)) == "reserved"
    assert _run(nx.dispatch_effect(db, ctx))
    return ctx


# ---------------------------------------------------------------- P0-01
def test_close_seq_is_durable_and_monotonic_across_proposals(world):
    """The auditor's sequence: proposal A (recovered to fence 2) then a NEWER
    proposal B at fence 1. B must carry a HIGHER close_seq than A — the EA orders
    by close_seq, never by the proposal-local fence."""
    from routes.nl_routes import execute_one
    db, uid = world["db"], world["id"]
    tid = _run(db.trades.insert_one({"user_id": uid, "symbol": "XAUUSD", "status": "open", "action": "BUY",
                                     "entry_price": 1.0, "mt5_ticket": 1})).inserted_id
    ctx_a = _armed(db, uid, "a" * 32, owner="A2", fence=2)         # A after recovery → fence 2
    _run(execute_one(uid, {"type": "CLOSE_ALL_TRADES", "target": "all"}, idem_key="a" * 32, ctx=ctx_a))
    t = _run(db.trades.find_one({"_id": tid}))
    assert t["close_seq"] == 1 and t["close_fence"] == 2 and t["close_command"]["state"] == "requested"
    ctx_b = _armed(db, uid, "b" * 32, owner="B1", fence=1)         # newer proposal B starts at fence 1
    _run(execute_one(uid, {"type": "CLOSE_ALL_TRADES", "target": "all"}, idem_key="b" * 32, ctx=ctx_b))
    t = _run(db.trades.find_one({"_id": tid}))
    assert t["close_seq"] == 2 > 1 and t["close_idem_key"] == "b" * 32 and t["close_fence"] == 1
    assert [h["key"] for h in t["close_command_history"]] == ["a" * 32, "b" * 32]   # supersession recorded
    # EA source orders by close_seq and never reads close_fence
    ea = open(os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5"), encoding="utf-8", errors="ignore").read()
    assert '"\\"close_seq\\":"' in ea and '"\\"close_fence\\":"' not in ea
    assert "NlCloseAdmitted(trade_id, nl_key, nl_seq)" in ea
    # bridge poll payload forwards close_seq
    br = open(os.path.join(ROOT, "backend", "routes", "bridge_routes.py")).read()
    assert '"close_seq": t.get("close_seq", 0)' in br and "broker_confirmed" in br


def test_panic_increments_the_same_close_seq(world, monkeypatch):
    from routes.panic_routes import _disable_all_bots_and_close_trades
    import routes.panic_routes as pr
    db, uid = world["db"], world["id"]
    tid = _run(db.trades.insert_one({"user_id": uid, "symbol": "BTCUSD", "status": "open", "close_seq": 4})).inserted_id
    sent = []

    async def _bc(u, ev, payload):
        sent.append(ev)
    monkeypatch.setattr(pr.ws_manager, "broadcast", _bc)
    out = _run(_disable_all_bots_and_close_trades({"user_id": uid}, broadcast_user_id=uid))
    t = _run(db.trades.find_one({"_id": tid}))
    assert t["close_seq"] == 5 and t["close_reason"] == "panic" and t["close_command"]["reason"] == "panic"
    ledger = _run(db.close_commands.find_one({"command_id": out["close_command_id"], "trade_id": str(tid)}))
    assert ledger["close_seq"] == 5 and ledger["state"] == "requested" and ledger["actor"].startswith("panic:")
    _run(db.close_commands.delete_many({"trade_id": str(tid)}))
    row = _run(db.ops_outbox.find_one({"_id": out["outbox_id"]}))
    assert row["state"] == "published" and sent == ["panic_lock"]          # no session → published immediately


# ---------------------------------------------------------------- P0-02
def test_ea_capability_gate_numeric_versions_and_hash(monkeypatch):
    from ea_capabilities import live_gate, version_tuple, capabilities_for
    monkeypatch.setenv("EA_RELEASE_SHA256", "c" * 64)                 # pinned from the release record
    assert version_tuple("1.57") == (1, 57) and version_tuple("v1.60") == (1, 60) and version_tuple("1.57.1") == (1, 57, 1)
    assert version_tuple("1.9") < version_tuple("1.10")               # never string compare
    assert version_tuple("") is None and version_tuple("beta") is None
    # r25 P1-01: a hash only counts when it is installer-attested for the installation
    blocked = {v: live_gate({"ea_version": v, "ea_binary_sha256": "c" * 64, "ea_binary_sha256_method": "installer_attested"})
               for v in (None, "unknown", "1.50", "1.56", "1.57", "1.57.1", "1.60")}
    assert blocked[None]["code"] == "EA_VERSION_UNKNOWN" and blocked["unknown"]["code"] == "EA_VERSION_UNKNOWN"
    assert blocked["1.50"]["code"] == "EA_CAPABILITY_BELOW_MIN" and "nl_close_fence_v1" in blocked["1.50"]["reason"]
    assert blocked["1.56"]["code"] == "EA_CAPABILITY_BELOW_MIN"
    assert blocked["1.57"] is None and blocked["1.57.1"] is None and blocked["1.60"] is None
    assert "nl_close_fence_v1" in capabilities_for("1.57") and "nl_close_fence_v1" not in capabilities_for("1.56")
    forged = live_gate({"ea_version": "1.57", "ea_binary_sha256": "b" * 64, "ea_binary_sha256_method": "installer_attested"})
    assert forged["code"] == "EA_BINARY_HASH_MISMATCH"
    # r20 P1-01: proof is REQUIRED for live — missing report or unpinned release both block
    assert live_gate({"ea_version": "1.57"})["code"] == "EA_BINARY_PROOF_MISSING"
    assert live_gate({"ea_version": "1.57", "mode": "paper"}) is None            # paper: no terminal dependency
    # P1-03: local compile stays demo-only — but ONLY an admin-attested DEMO skips the binary proof
    # (security audit: a live account must not self-label demo). Capability floor applies to all.
    from broker_env import attestation_identity
    demo = {"ea_version": "1.57", "account_type": "demo"}
    att = {"environment": "DEMO", "approved_by": "admin@example.com", "at": "2026-01-01T00:00:00+00:00",
           "identity_hash": attestation_identity(demo),       # r28 P1-01: bound to the identity digest
           "proof": {"verifier": "ea_heartbeat", "proof_id": "p"}}   # r29 P1-02: independent demo evidence
    assert live_gate(demo)["code"] == "EA_DEMO_UNATTESTED"
    assert live_gate({"ea_version": "1.57", "broker_server": "RoboForex-Demo"})["code"] == "EA_DEMO_UNATTESTED"
    assert live_gate({"ea_version": "1.57", "broker_environment": "DEMO"})["code"] == "EA_DEMO_UNATTESTED"
    assert live_gate({**demo, "environment_attestation": att}) is None
    assert live_gate({**demo, "ea_version": "1.56", "environment_attestation": att})["code"] == "EA_CAPABILITY_BELOW_MIN"
    # attestation never overrides a LIVE declaration, nor works without an approver / digest
    assert live_gate({"ea_version": "1.57", "environment_attestation": att})["code"] == "EA_BINARY_PROOF_MISSING"
    assert live_gate({**demo, "broker_environment": "LIVE", "environment_attestation": att})["code"] == "EA_BINARY_PROOF_MISSING"
    assert live_gate({**demo, "environment_attestation": {"environment": "DEMO"}})["code"] == "EA_DEMO_UNATTESTED"
    assert live_gate({**demo, "broker": "moved", "environment_attestation": att})["code"] == "EA_DEMO_ATTESTATION_INVALIDATED"
    monkeypatch.delenv("EA_RELEASE_SHA256")
    monkeypatch.setattr("ea_capabilities._EA_RELEASE_FILES", ())
    monkeypatch.setattr("ea_capabilities._signed_release_record", lambda: None)   # v1.60.3: a tree with a signed EX5 record is pinned
    assert live_gate({"ea_version": "1.57", "ea_binary_sha256": "c" * 64, "ea_binary_sha256_method": "installer_attested"})["code"] == "EA_RELEASE_HASH_UNPINNED"


def test_incompatible_ea_blocks_activation_and_canonical_authority(world, monkeypatch):
    from routes.bot_routes import _activation_readiness, FENCING_MIN_EA
    monkeypatch.setenv("EA_RELEASE_SHA256", "c" * 64)
    from trading_authority import infrastructure_domain
    from canonical_decision import reason_code
    assert FENCING_MIN_EA == "1.57"
    hb = datetime.now(timezone.utc).isoformat()
    acc = {"user_id": world["id"], "mode": "live", "trading_enabled": True, "ea_version": "1.56",
           "ea_binary_sha256": "c" * 64, "ea_binary_sha256_method": "installer_attested",
           "last_heartbeat": hb, "equity": 1000, "status": "active"}
    problems = _run(_activation_readiness(world["db"], acc))
    assert any("nl_close_fence_v1" in p for p in problems)
    dom = _run(infrastructure_domain(world["db"], acc))
    assert dom["level"] == "CLOSE_ONLY" and reason_code("infrastructure", dom) == "EA_CAPABILITY_BELOW_MIN"
    ok = _run(infrastructure_domain(world["db"], {**acc, "ea_version": "1.57"}))
    assert ok["level"] == "FULL"
    unproved = _run(infrastructure_domain(world["db"], {**acc, "ea_version": "1.57", "ea_binary_sha256": None}))
    assert unproved["level"] == "CLOSE_ONLY" and unproved["code"] == "EA_BINARY_PROOF_MISSING"
    paper = _run(infrastructure_domain(world["db"], {**acc, "mode": "paper", "ea_version": "1.20"}))
    assert paper["level"] == "FULL"                                      # paper: no terminal dependency


# ---------------------------------------------------------------- P1-01
def test_transactions_required_by_capability(world, monkeypatch):
    import nl_execution as nx
    import app_env
    db, uid = world["db"], world["id"]
    fn = nx._transactions_required_real          # the autouse synthetic patch is bypassed
    monkeypatch.setattr(app_env, "is_production", lambda: False)
    monkeypatch.delenv("NL_EFFECTS_SYNTHETIC_ONLY", raising=False)
    live_ids = _run(db.accounts.find({"trading_enabled": True, "mode": "live", "status": {"$ne": "deleted"}},
                                     {"_id": 1}).to_list(100))
    # without the explicit synthetic posture → required even in preview
    assert _run(fn(db)) is True
    monkeypatch.setenv("NL_EFFECTS_SYNTHETIC_ONLY", "true")
    # with live-enabled accounts present → still required
    _run(db.accounts.insert_one({"user_id": uid, "trading_enabled": True, "mode": "live", "status": "active"}))
    assert _run(fn(db)) is True
    _run(db.accounts.delete_many({"user_id": uid}))
    if not live_ids and not _run(nx.capital_capable(db)):                   # r20 P1-02 — bound terminals left by other suites also count
        assert _run(fn(db)) is False                                        # synthetic-only + zero live accounts
    monkeypatch.setattr(app_env, "is_production", lambda: True)
    assert _run(fn(db)) is True


# ---------------------------------------------------------------- P1-02
def test_panic_inside_session_defers_broadcast_and_authority_bump(world, monkeypatch):
    """With a session the helper writes ONLY database rows (incl. the outbox);
    broadcast + authority bump happen in publish_panic_outbox after commit,
    exactly once."""
    import routes.panic_routes as pr
    import canonical_decision as cd
    db, uid = world["db"], world["id"]
    _run(db.trades.insert_one({"user_id": uid, "symbol": "BTCUSD", "status": "open"}))
    sent, bumps = [], []

    async def _bc(u, ev, payload):
        sent.append(ev)

    async def _bump(db_, reason, user_id=None, session=None):
        bumps.append((reason, session is not None))
        return 99
    monkeypatch.setattr(pr.ws_manager, "broadcast", _bc)
    monkeypatch.setattr(cd, "bump_authority_version", _bump)

    class FakeSession:  # marks "inside a transaction" — motor ignores session=None only
        pass
    out = _run(pr._disable_all_bots_and_close_trades({"user_id": uid}, broadcast_user_id=uid, session=None,
                                                     stamp={}))  # baseline path publishes immediately
    assert sent == ["panic_lock"] and bumps == [("panic", False)]      # bump is part of the (no-)txn write path
    assert out["authority_version"] == 99
    sent.clear(); bumps.clear()
    # simulate the transactional path: outbox row pending, nothing published yet
    oid = "outbox-test-" + uid
    _run(db.ops_outbox.insert_one({"_id": oid, "kind": "panic_lock", "user_id": uid, "payload": {"x": 1},
                                   "state": "pending", "created_at": "now"}))
    assert sent == [] and bumps == []
    assert _run(pr.publish_panic_outbox(db, oid)) is True
    assert _run(pr.publish_panic_outbox(db, oid)) is False                 # exactly once
    assert sent == ["panic_lock"] and bumps == []                          # publisher never bumps: it committed in the txn
    row = _run(db.ops_outbox.find_one({"_id": oid}))
    assert row["state"] == "published" and row["outcome"] == "delivered" and row["attempts"] == 1
    # r20 P2-03 crash recovery: a row stuck in `publishing` with an expired lease is reclaimed by the sweeper
    _run(db.ops_outbox.update_one({"_id": oid}, {"$set": {"state": "publishing", "lease_until": "2000-01-01T00:00:00+00:00"}}))
    assert _run(pr.sweep_ops_outbox(db)) >= 1
    row = _run(db.ops_outbox.find_one({"_id": oid}))
    assert row["state"] == "published" and row["attempts"] == 2 and sent == ["panic_lock", "panic_lock"]
    _run(db.ops_outbox.delete_many({"_id": oid}))
    src = open(pr.__file__).read()
    body = src[src.index("async def _disable_all_bots_and_close_trades"):src.index("async def publish_panic_outbox")]
    assert "ws_manager.broadcast" not in body.replace("publish_panic_outbox", "")
    assert "session=session)" in body[body.index("bump_authority_version("):]


# ---------------------------------------------------------------- P2-01 / P2-02
def test_admission_manifest_emit_verify_and_deploy_gate(tmp_path):
    script = os.path.join(ROOT, "scripts", "verify_admission.py")
    be = "ghcr.io/o/stoic-backend@sha256:" + "a" * 64
    fe = "ghcr.io/o/stoic-frontend@sha256:" + "b" * 64
    f = tmp_path / "adm.json"
    assert subprocess.run([sys.executable, script, str(f), "--emit", "--backend-digest", be, "--frontend-digest", fe,
                           "--tag", "v9", "--commit", "c" * 40]).returncode == 0
    assert subprocess.run([sys.executable, script, str(f), "--backend-digest", be, "--frontend-digest", fe, "--tag", "v9"],
                          capture_output=True).returncode == 0
    assert subprocess.run([sys.executable, script, str(f), "--backend-digest", be, "--frontend-digest",
                           "ghcr.io/o/stoic-frontend:v9"], capture_output=True).returncode == 1   # tag ≠ digest pair
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert rel.index("verify_admission.py --emit") < rel.index("> SHA256SUMS") < rel.index("Emit signed release attestation")
    assert "release-manifest.json release-admission.json" in rel
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "release-admission.json missing — refusing to deploy" in lib and "verify_admission.py" in lib
    att = open(os.path.join(ROOT, "scripts", "release_attestation.py")).read()
    assert '"release-admission.json.sig"' in att


def test_notification_older_than_provider_retention_becomes_incident(monkeypatch):
    import deploy_watch as dw
    import email_sender
    from database import get_db
    db = get_db()
    calls = []

    async def fake_send(recipient, subject, html, text=None, sender=None, idempotency_key=None):
        calls.append(idempotency_key)
        return {"ok": True, "id": "msg-1"}
    monkeypatch.setattr(email_sender, "send_email", fake_send)
    wid = "watch-retention-test"
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))
    doc = {"_id": wid, "target_url": "https://www.stoicaibot.com", "expected_sha": "abc1234", "notify_email": "ops@example.com",
           "armed_by": "ops@example.com", "armed_at": datetime.now(timezone.utc), "baseline_sha": None}
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch.insert_one(doc))
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    _run(db.deploy_watch_outbox.insert_one({"_id": f"{wid}:live", "watch_id": wid, "kind": "live", "state": "claimed",
                                            "claimed_at": old, "first_claimed_at": old, "to": "ops@example.com"}))
    r = _run(dw._notify(db, doc, "live", {"http": 200, "build_sha": "abc1234"}))
    assert r["incident"] is True and calls == []                          # never a blind re-send
    assert _run(db.deploy_watch_outbox.find_one({"_id": f"{wid}:live"}))["state"] == "incident"
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))


# ---------------------------------------------------------------- P2-03 (AT-P1-02)
def test_replica_set_stale_worker_conflict_commits_exactly_once(world):
    """Real transaction conflict: A is paused INSIDE its transaction (after the
    dispatching→applying write), B takes over the row; exactly ONE domain state
    and ONE effect outcome survive. Runs on the CI replica set."""
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    if not _run(db.command("hello")).get("setName"):
        pytest.skip("standalone MongoDB — transactional path needs the CI replica set")
    key = "r" * 32
    ctx_a = _armed(db, uid, key, owner="A", fence=1)
    ctx_b = {**ctx_a, "owner": "B", "fence": 2}
    entered, resume = asyncio.Event(), asyncio.Event()
    writes = []

    async def work_a(session):
        entered.set()
        await asyncio.wait_for(resume.wait(), 20)
        await db.trades.insert_one({"user_id": uid, "symbol": "TXN", "status": "open", "by": "A"}, session=session)
        writes.append("A")
        return {"by": "A"}

    async def work_b(session):
        await db.trades.insert_one({"user_id": uid, "symbol": "TXN", "status": "open", "by": "B"}, session=session)
        writes.append("B")
        return {"by": "B"}

    async def scenario():
        task_a = asyncio.create_task(nx.fenced(db, ctx_a, work_a))
        await entered.wait()

        async def b_path():
            st = await nx.reserve_effect(db, key, {"user_id": uid}, ctx=ctx_b)    # blocks on A's row lock
            if st == "reserved":
                await nx.dispatch_effect(db, ctx_b)
                return await nx.fenced(db, ctx_b, work_b)
            return {"state": st}
        task_b = asyncio.create_task(b_path())
        await asyncio.sleep(0.5)
        resume.set()
        ra = await asyncio.gather(task_a, return_exceptions=True)
        rb = await task_b
        return ra[0], rb

    ra, rb = _run(scenario())
    rows = _run(db.trades.count_documents({"user_id": uid, "symbol": "TXN"}))
    eff = _run(db.nl_effects.find_one({"_id": key}))
    committed = [w for w in ("A", "B") if isinstance((ra if w == "A" else rb), dict) and (ra if w == "A" else rb).get("by") == w]
    assert rows == 1 and eff["state"] == "completed" and len(committed) == 1, (ra, rb, eff)
    assert eff["result"]["by"] == committed[0] and eff["committed"] == "transaction"
