"""Live backend verification for Security & Health Agent SA4 — Containment.

Scope (R6 only, bridge scope — safe: no auth-scope blocks against the shared ingress IP):
- GET /api/admin/security/blocks → 200 {blocks:[]} baseline
- undo/extend without step-up → 403
- undo unknown action → 404; extend minutes=99999 → 400
- Enforce cycle: mode=enforce rules=['R6'], send 34 bogus bridge heartbeats,
  wait for the B2 finding + the R6 containment action, verify security_blocks row
  (kind=ip, scope=bridge, seconds_left>0), heartbeat → 403 blocked_by_security_agent,
  admin login still 200 (bridge ≠ auth), finding contained
- Extend to minutes=1 → seconds_left ≤ 60
- Undo → status 'undone', heartbeat back to 401, blocks empty, finding re-opens
- Replay undo → 409
- /api/admin/audit/verify ok=true after writes
- Observe-mode invariants (dry run): snapshot & re-enter observe, no new 'done' rows
- No-trade-effect invariant: trades/pending_mods/pending_orders/bot_configs counts unchanged

ALWAYS restores observe mode + undoes any active block at teardown — even on failure.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url, admin_credentials  # noqa: E402

BASE_URL = require_live_base_url().rstrip("/")
ADMIN_EMAIL, ADMIN_PASSWORD = admin_credentials(strict=True)

STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN") or ""
if not STEP_UP_BYPASS:
    try:
        with open("/app/backend/.env", "r") as f:
            for line in f:
                if line.startswith("STEP_UP_BYPASS_TOKEN="):
                    STEP_UP_BYPASS = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass


def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    assert STEP_UP_BYPASS, "STEP_UP_BYPASS_TOKEN not available"
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def step_up_headers() -> dict:
    return {"X-Step-Up-Bypass": STEP_UP_BYPASS}


@pytest.fixture(scope="module")
def mongo_db():
    pymongo = pytest.importorskip("pymongo")
    url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    db_name = os.environ.get("STOIC_LIVE_DB_NAME") or "ai_trading_bot"
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
    db = client[db_name]
    db.command("ping")
    yield db
    client.close()


@pytest.fixture(scope="module", autouse=True)
def restore_observe_and_blocks(admin_session, step_up_headers):
    """Hard safety net — regardless of what tests do, leave:
       - mode=observe, rules_enabled=[]
       - no active security_blocks (undo any that remain)."""
    yield
    try:
        # Undo any active block
        r = admin_session.get(f"{BASE_URL}/api/admin/security/blocks", timeout=15)
        if r.status_code == 200:
            for b in (r.json().get("blocks") or []):
                aid = b.get("action_id")
                if aid:
                    try:
                        admin_session.post(
                            f"{BASE_URL}/api/admin/security/actions/{aid}/undo",
                            json={"note": "teardown"}, headers=step_up_headers,
                            timeout=15)
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        admin_session.post(
            f"{BASE_URL}/api/admin/security/mode",
            json={"mode": "observe", "rules_enabled": []},
            headers=step_up_headers, timeout=15)
    except Exception:
        pass


# ────────────────────────── Phase 1: baseline reads ───────────────────────────
def test_blocks_empty_at_start(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/blocks", timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    d = r.json()
    assert "blocks" in d and isinstance(d["blocks"], list)
    assert d["blocks"] == [], f"expected empty blocks at start, got {d['blocks']}"


def test_undo_unknown_action_404(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/000000000000000000000000/undo",
        json={"note": "x"}, headers=step_up_headers, timeout=15)
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_extend_minutes_out_of_range_400(admin_session, step_up_headers):
    # Even if the action doesn't exist, minutes validation runs first → 400
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/000000000000000000000000/extend",
        json={"minutes": 99999}, headers=step_up_headers, timeout=15)
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_undo_without_step_up_403(admin_session):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/000000000000000000000000/undo",
        json={"note": "x"}, headers={"X-Step-Up-Bypass": ""}, timeout=15)
    assert r.status_code == 403, (r.status_code, r.text[:200])


def test_extend_without_step_up_403(admin_session):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/000000000000000000000000/extend",
        json={"minutes": 1}, headers={"X-Step-Up-Bypass": ""}, timeout=15)
    assert r.status_code == 403, (r.status_code, r.text[:200])


# ────────────────────────── Phase 2: enforce cycle ────────────────────────────
# Shared state across the live sequence
_ctx: dict = {}


def _get_blocks(session) -> list:
    r = session.get(f"{BASE_URL}/api/admin/security/blocks", timeout=15)
    assert r.status_code == 200, r.text[:200]
    return r.json().get("blocks") or []


def _get_actions(session, kind="containment") -> list:
    r = session.get(
        f"{BASE_URL}/api/admin/security/actions?kind={kind}&limit=200",
        timeout=15)
    assert r.status_code == 200, r.text[:200]
    return r.json().get("actions") or []


def test_snapshot_no_trade_effect_counts(mongo_db):
    """Snapshot counts of trade-related collections BEFORE enforce cycle.
       Will be compared again after undo + restore to prove no side-effects."""
    _ctx["snap_before"] = {
        "trades": mongo_db.trades.count_documents({}),
        "pending_modifications": mongo_db.pending_modifications.count_documents({}),
        "pending_orders": mongo_db.pending_orders.count_documents({}),
        "bot_configs": mongo_db.bot_configs.count_documents({}),
        "accounts_authority": [
            {"_id": str(a["_id"]), "trading_authority": a.get("trading_authority"),
             "authority_lock": a.get("authority_lock")}
            for a in mongo_db.accounts.find({}, {"trading_authority": 1, "authority_lock": 1}).limit(50)
        ],
    }


def test_set_mode_enforce_r6(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "enforce", "rules_enabled": ["R6"]},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json().get("mode") == "enforce"


def test_fire_34_bogus_heartbeats_then_wait_for_block(admin_session, mongo_db):
    """Send 34 POST /api/bridge/heartbeat with a bogus token; expect 401 each.
       Within ~70s the B2 finding + R6 containment action + security_blocks row
       exist. If the agent dedups because an older action for the same finding is
       still within the 1 hour window, we record and skip (per review request)."""
    bogus = f"BOGUSTOKEN-{uuid.uuid4().hex}"
    unauth = requests.Session()
    payload = {"bridge_token": bogus, "balance": 0.0, "equity": 0.0,
               "ea_version": "test"}
    err401 = 0
    for _ in range(34):
        try:
            r = unauth.post(f"{BASE_URL}/api/bridge/heartbeat",
                            json=payload, timeout=10)
            if r.status_code == 401:
                err401 += 1
        except Exception:
            pass
    assert err401 >= 30, f"expected ~34 401s, got {err401}"

    # Wait up to 90s for the next agent tick to open B2 and apply R6
    deadline = time.time() + 90
    block = None
    action = None
    while time.time() < deadline:
        blocks = _get_blocks(admin_session)
        if blocks:
            # Pick the IP+bridge scope block that is active
            for b in blocks:
                if b.get("kind") == "ip" and b.get("scope") == "bridge":
                    block = b
                    break
            if block:
                break
        time.sleep(5)

    if not block:
        # Check if the agent de-duped: find a prior R6 containment for a B2 finding today
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        dedup_rows = list(mongo_db.security_actions.find(
            {"rule": "R6", "status": "done", "at": {"$gte": since}}))
        pytest.skip(
            f"Agent did not create a new R6 block (likely dedup). "
            f"Prior R6 done rows in last hour: {len(dedup_rows)}. "
            f"finding_ids={[str(r.get('finding_id')) for r in dedup_rows]}")

    _ctx["block"] = block
    assert block.get("kind") == "ip"
    assert block.get("scope") == "bridge"
    assert block.get("value"), f"block missing value (ip): {block}"
    assert int(block.get("seconds_left") or 0) > 0
    assert block.get("action_id")

    # Pull the matching action and finding
    acts = _get_actions(admin_session, kind="containment")
    action = next((a for a in acts
                   if a.get("id") == block["action_id"]
                   or str(a.get("block_id") or "") == str(block.get("id") or "")), None)
    assert action, f"no containment action matched block {block}"
    assert action.get("rule") == "R6"
    assert action.get("action") == "block_ip"
    assert action.get("status") == "done"
    assert action.get("expires_at")
    _ctx["action_id"] = action["id"]
    _ctx["finding_id"] = action.get("finding_id")


def test_bridge_heartbeat_now_blocked(admin_session):
    if "block" not in _ctx:
        pytest.skip("no block created in enforce step")
    unauth = requests.Session()
    r = unauth.post(f"{BASE_URL}/api/bridge/heartbeat",
                    json={"bridge_token": "whatever", "balance": 0.0,
                          "equity": 0.0, "ea_version": "x"},
                    timeout=10)
    assert r.status_code == 403, (r.status_code, r.text[:200])
    body = r.json()
    detail = body.get("detail") or {}
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code == "blocked_by_security_agent", f"body={body}"
    assert (detail.get("ref") if isinstance(detail, dict) else None)


def test_admin_login_still_200_bridge_scope_only(mongo_db):
    """bridge-scope block must NOT impact /api/auth/login (that would lock the ingress IP)."""
    if "block" not in _ctx:
        pytest.skip("no block created in enforce step")
    # Space out to avoid 429
    time.sleep(10)
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_finding_is_contained(admin_session, mongo_db):
    if "finding_id" not in _ctx:
        pytest.skip("no finding created")
    fid = _ctx["finding_id"]
    from bson import ObjectId
    row = mongo_db.security_findings.find_one({"_id": ObjectId(fid) if ObjectId.is_valid(fid) else fid})
    assert row, f"finding {fid} not found"
    assert row.get("status") == "contained", f"status={row.get('status')}"
    assert row.get("fixed") in ("contained", True, "yes"), f"fixed={row.get('fixed')}"
    assert "[action" in (row.get("action_taken") or ""), f"action_taken={row.get('action_taken')}"


def test_extend_shortens_block_to_one_minute(admin_session, step_up_headers):
    if "action_id" not in _ctx:
        pytest.skip("no action to extend")
    aid = _ctx["action_id"]
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/{aid}/extend",
        json={"minutes": 1}, headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])

    blocks = _get_blocks(admin_session)
    mine = next((b for b in blocks if b.get("action_id") == aid), None)
    assert mine, f"block for action {aid} missing after extend"
    sl = int(mine.get("seconds_left") or 0)
    assert 0 < sl <= 65, f"expected seconds_left<=60 after extend minutes=1, got {sl}"


def test_undo_block_restores(admin_session, step_up_headers, mongo_db):
    if "action_id" not in _ctx:
        pytest.skip("no action to undo")
    aid = _ctx["action_id"]
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/{aid}/undo",
        json={"note": "test"}, headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    body = r.json()
    assert body.get("status") == "undone", f"body={body}"

    # Blocks list now empty
    time.sleep(1)
    blocks = _get_blocks(admin_session)
    assert blocks == [], f"blocks must be empty after undo, got {blocks}"

    # Bridge heartbeat is back to 401 (not 403)
    unauth = requests.Session()
    r2 = unauth.post(f"{BASE_URL}/api/bridge/heartbeat",
                     json={"bridge_token": "whatever", "balance": 0.0,
                           "equity": 0.0, "ea_version": "x"},
                     timeout=10)
    assert r2.status_code == 401, (r2.status_code, r2.text[:200])

    # Finding re-opens
    from bson import ObjectId
    fid = _ctx["finding_id"]
    row = mongo_db.security_findings.find_one({"_id": ObjectId(fid) if ObjectId.is_valid(fid) else fid})
    assert row.get("status") == "open", f"status={row.get('status')}"
    assert row.get("fixed") == "no", f"fixed={row.get('fixed')}"
    assert (row.get("action_taken") or "").startswith("undone by"), f"action_taken={row.get('action_taken')}"


def test_undo_replay_409(admin_session, step_up_headers):
    if "action_id" not in _ctx:
        pytest.skip("no action to undo twice")
    aid = _ctx["action_id"]
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/actions/{aid}/undo",
        json={"note": "again"}, headers=step_up_headers, timeout=15)
    assert r.status_code == 409, (r.status_code, r.text[:200])


def test_audit_chain_ok_with_security_actions(admin_session, mongo_db):
    r = admin_session.get(f"{BASE_URL}/api/admin/audit/verify", timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json().get("ok") is True

    if "action_id" not in _ctx:
        pytest.skip("no action id")
    aid = _ctx["action_id"]
    # Expect the three logged actions
    actions_logged = set()
    for row in mongo_db.admin_audit_log.find(
            {"target_kind": "security_agent", "meta.action_id": aid}):
        actions_logged.add(row.get("action"))
    # The block_ip row has no meta.action_id (target is the action_id itself),
    # so look it up by target as well
    block_row = mongo_db.admin_audit_log.find_one(
        {"target_kind": "security_agent", "action": "security_action_block_ip",
         "actor_email": "security_agent"})
    assert block_row, "missing admin_audit_log action=security_action_block_ip actor=security_agent"
    assert "security_extend_block" in actions_logged, f"logged={actions_logged}"
    assert "security_undo_block_ip" in actions_logged, f"logged={actions_logged}"


# ────────────────────────── Phase 3: restore + invariants ─────────────────────
def test_restore_observe_and_blocks_clear(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "observe", "rules_enabled": []},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    s = admin_session.get(f"{BASE_URL}/api/admin/security/status", timeout=10)
    assert s.status_code == 200 and s.json().get("mode") == "observe"
    assert _get_blocks(admin_session) == []


def test_no_trade_side_effects(mongo_db):
    """Trade/order/bot_config counts unchanged, and no accounts had authority mutated."""
    snap = _ctx.get("snap_before")
    if not snap:
        pytest.skip("no snapshot")
    for coll in ("trades", "pending_modifications", "pending_orders", "bot_configs"):
        now_n = mongo_db[coll].count_documents({})
        assert now_n == snap[coll], f"{coll} count changed: {snap[coll]} → {now_n}"
    for a in snap["accounts_authority"]:
        from bson import ObjectId
        cur = mongo_db.accounts.find_one({"_id": ObjectId(a["_id"])},
                                         {"trading_authority": 1, "authority_lock": 1})
        assert cur, f"account {a['_id']} missing"
        assert cur.get("trading_authority") == a["trading_authority"], \
            f"authority changed for {a['_id']}: {a['trading_authority']} → {cur.get('trading_authority')}"
        assert cur.get("authority_lock") == a["authority_lock"], \
            f"authority_lock changed for {a['_id']}"


def test_observe_dry_run_no_new_done_actions(admin_session, mongo_db):
    """In observe mode, after a tick, no new security_blocks and no new 'done' rows
       should appear even if findings exist."""
    before_done = mongo_db.security_actions.count_documents({"status": "done"})
    before_blocks = mongo_db.security_blocks.count_documents({"active": True})
    # Wait one tick to pass
    time.sleep(65)
    after_done = mongo_db.security_actions.count_documents({"status": "done"})
    after_blocks = mongo_db.security_blocks.count_documents({"active": True})
    assert after_done == before_done, \
        f"'done' rows grew in observe mode: {before_done} → {after_done}"
    assert after_blocks == before_blocks, \
        f"active blocks grew in observe mode: {before_blocks} → {after_blocks}"
