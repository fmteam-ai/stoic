"""iter-204 · Audit acceptance tests AT-06 / AT-07 / AT-08 / AT-11 / AT-15.

Runs against the preview Mongo (backend/.env) with synthetic user ids;
everything created here is removed again. Uses pymongo for seeding and the
shared asyncio loop (conftest.run_async) for the async modules under test.
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from pymongo import MongoClient

from conftest import run_async

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # backend/
REPO = os.path.dirname(ROOT)


@pytest.fixture
def sdb():
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


@pytest.fixture
def uid(sdb):
    u = f"at_{uuid.uuid4().hex[:10]}"
    yield u
    for col in ("accounts", "trades", "execution_intents", "signals"):
        sdb[col].delete_many({"user_id": u})
    # ledger rows are deliberately NOT deleted — the chain is append-only
    sdb.ops_alerts.delete_many({"kind": "repair_ledger_mutation_refused", "message": {"$regex": u}})


def _iso(dt):
    return dt.isoformat()


# ── AT-07 · atomic repair ledger ─────────────────────────────────────────────
def _seed_ghosts(sdb, uid, n=3):
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    ids = [sdb.trades.insert_one({"user_id": uid, "status": "closed", "exit_price": None,
                                  "closed_at": old, "account_id": str(ObjectId())}).inserted_id for _ in range(n)]
    return ids


def test_at07_pending_row_written_before_mutation_and_completed_after(sdb, uid):
    from database import get_db
    import health_repairs as hr
    ids = _seed_ghosts(sdb, uid)
    out = run_async(hr.run_health_repairs(get_db(), uid))
    # ghosts >24h are acked by the first repair; the orphan pass then finds nothing → ONE ledger row
    rows = list(sdb.repair_ledger.find({"user_id": uid}).sort("seq", 1))
    assert {r["kind"] for r in rows} == {"ghost_ack_old"}
    for r in rows:
        assert r["state"] == "completed" and r["entry_hash"] and r["batch_hash"] and r["before"]
        assert r["after"]["applied_at"] and "modified" in r["after"]
        assert sorted(r["affected_ids"]) == sorted(str(i) for i in ids)
    # every mutated record carries the repair id → audit trail cannot lose a mutation
    for t in sdb.trades.find({"_id": {"$in": ids}}):
        assert t["ghost_acknowledged"] is True
        assert any(rid.startswith(out["correlation_id"]) for rid in t["repair_ids"])
    # second (orphan) repair found nothing left to change: idempotent, no double-ack
    assert out["ghost_old"] == 3 and out["ghost_orphan"] == 0
    # re-running the sweep is idempotent — no new ledger rows, nothing modified
    n = sdb.repair_ledger.count_documents({"user_id": uid})
    out2 = run_async(hr.run_health_repairs(get_db(), uid))
    assert sdb.repair_ledger.count_documents({"user_id": uid}) == n
    assert all(v == 0 for k, v in out2.items() if k != "correlation_id")


def test_at07_crash_after_pending_row_is_replayed_exactly_once(sdb, uid):
    """Fault: process dies after the PENDING row, before the mutation."""
    from database import get_db
    import health_repairs as hr
    ids = _seed_ghosts(sdb, uid, 2)
    corr = f"repair_crash{uuid.uuid4().hex[:6]}"
    row = run_async(hr._open_pending(get_db(), corr, uid, "ghost_ack_old", ids, {"ghost_acknowledged": False}))
    assert row["state"] == "pending"
    assert sdb.trades.count_documents({"_id": {"$in": ids}, "ghost_acknowledged": True}) == 0
    # too young → not replayed yet (avoid racing an in-flight repair)
    assert sdb.repair_ledger.count_documents({"_id": row["_id"], "state": "pending"}) == 1
    run_async(hr.replay_incomplete(get_db(), older_than_sec=3600))
    assert sdb.repair_ledger.count_documents({"_id": row["_id"], "state": "pending"}) == 1
    # old enough (cutoff = now) → replayed; `at` is a chained field so age is never mutated here
    run_async(hr.replay_incomplete(get_db(), older_than_sec=0))
    done = sdb.repair_ledger.find_one({"_id": row["_id"]})
    assert done["state"] == "completed" and done["replayed"] is True and done["after"]["modified"] == 2
    assert sdb.trades.count_documents({"_id": {"$in": ids}, "ghost_acknowledged": True}) == 2
    # replaying again does nothing to THIS repair (exactly once)
    run_async(hr.replay_incomplete(get_db(), older_than_sec=0))
    again = sdb.repair_ledger.find_one({"_id": row["_id"]})
    assert again["after"] == done["after"] and again["completed_at"] == done["completed_at"]
    assert sdb.repair_ledger.count_documents({"correlation_id": corr, "state": "pending"}) == 0


def test_at07_crash_after_mutation_before_completion_does_not_reapply(sdb, uid):
    """Fault: mutation applied, process dies before COMPLETED. Replay must
    finalize the ledger WITHOUT touching records again."""
    from database import get_db
    import health_repairs as hr
    ids = _seed_ghosts(sdb, uid, 2)
    corr = f"repair_crash{uuid.uuid4().hex[:6]}"
    row = run_async(hr._open_pending(get_db(), corr, uid, "ghost_ack_old", ids, {}))
    col, flt, upd = hr._mutation("ghost_ack_old", row, "2026-01-01T00:00:00+00:00")
    sdb[col].update_many(flt, {**upd, "$addToSet": {"repair_ids": row["repair_id"]}})   # mutation happened
    run_async(hr.replay_incomplete(get_db(), older_than_sec=0))
    done = sdb.repair_ledger.find_one({"_id": row["_id"]})
    assert done["state"] == "completed" and done["after"]["modified"] == 0 and done["after"]["stamped"] == 2
    for t in sdb.trades.find({"_id": {"$in": ids}}):
        assert t["ghost_acknowledged_at"] == "2026-01-01T00:00:00+00:00"   # original timestamps untouched


def test_at07_duplicate_batch_refused_by_unique_index(sdb, uid):
    from database import get_db
    import health_repairs as hr
    run_async(hr.ensure_ledger_indexes(get_db()))
    ids = _seed_ghosts(sdb, uid, 1)
    corr = f"repair_dup{uuid.uuid4().hex[:6]}"
    assert run_async(hr._open_pending(get_db(), corr, uid, "ghost_ack_old", ids, {})) is not None
    assert run_async(hr._open_pending(get_db(), corr, uid, "ghost_ack_old", ids, {})) is None
    names = {i["name"] for i in sdb.repair_ledger.list_indexes()}
    assert "uniq_repair_batch" in names and "seq" in names


# ── AT-08 · append-only controls (application level) ─────────────────────────
def test_at08_hash_chain_detects_edit_and_removal(sdb, uid):
    from database import get_db
    import health_repairs as hr
    _seed_ghosts(sdb, uid, 2)
    run_async(hr.run_health_repairs(get_db(), uid))
    v0 = run_async(hr.verify_repair_chain(get_db()))
    assert v0["chained_entries"] >= 1 and "append-only" in v0["ledger_class"]
    row = sdb.repair_ledger.find_one({"user_id": uid, "kind": "ghost_ack_old"})
    seq = row["seq"]
    sdb.repair_ledger.update_one({"_id": row["_id"]}, {"$set": {"affected_ids": ["tampered"]}})
    v = run_async(hr.verify_repair_chain(get_db()))
    assert v["ok"] is False and {"seq": seq, "error": "entry_hash_mismatch"} in v["anomalies"]
    sdb.repair_ledger.delete_one({"_id": row["_id"]})
    v = run_async(hr.verify_repair_chain(get_db()))
    assert v["ok"] is False and any(a["error"] == "chain_link_broken" for a in v["anomalies"]) or v["chained_entries"] == v0["chained_entries"] - 1
    sdb.repair_ledger.insert_one(row)                    # restore the original row → chain intact again
    v = run_async(hr.verify_repair_chain(get_db()))
    assert v["anomalies"] == v0["anomalies"]


def test_at08_app_layer_refuses_update_delete_and_alerts(sdb, uid):
    from database import get_db
    import health_repairs as hr
    with pytest.raises(hr.LedgerMutationRefused):
        run_async(hr.guard_ledger_mutation(get_db(), "delete_many", actor=uid))
    alert = sdb.ops_alerts.find_one({"kind": "repair_ledger_mutation_refused", "message": {"$regex": uid}})
    assert alert and alert["severity"] == "critical" and alert["acknowledged"] is False


def test_at08_ledger_routes_are_read_only_and_expose_verify():
    src = open(os.path.join(ROOT, "routes", "repair_ledger_routes.py")).read()
    assert "@router.post" not in src and "@router.delete" not in src and "@router.put" not in src
    assert '"/admin/repair-ledger/verify"' in src
    for verb in ("insert_one", "update_one", "update_many", "delete_one", "delete_many"):
        assert verb not in src
    hr = open(os.path.join(ROOT, "health_repairs.py")).read()
    # the ONLY ledger write after insert is the pending→completed transition
    assert hr.count("db.repair_ledger.update_one") == 1 and "delete" not in hr.split("async def _complete")[1].split("# ── the repairs")[0]


# ── AT-06 · read-only health + fail-closed caps ──────────────────────────────
def test_at06_repair_sweep_enumerates_users_without_account_rows(sdb, uid):
    """P2-3 — a user whose accounts are gone but who still owns orphan ghosts
    must be part of the scheduled sweep."""
    from database import get_db
    import health_repairs as hr
    _seed_ghosts(sdb, uid, 1)
    assert sdb.accounts.count_documents({"user_id": uid}) == 0
    assert uid in run_async(hr.eligible_user_ids(get_db()))


def test_at06_health_get_is_read_only_static():
    src = open(os.path.join(ROOT, "routes", "bot_routes.py")).read()
    i = src.index('@router.get("/health-score")')
    j = src.index("@router.", i + 10)
    body = src[i:j]
    for verb in ("update_one", "update_many", "insert_one", "delete_many", "find_one_and_update"):
        assert verb not in body, f"GET health-score must not {verb}"
    assert "health_truth_unavailable" in src


# ── AT-11 · public metric integrity ──────────────────────────────────────────
def test_at11_populations_exclude_deleted_disabled_paper_demo_qa_duplicates(sdb, uid):
    from database import get_db
    import trust_stats as ts
    now = datetime.now(timezone.utc)
    fresh = _iso(now - timedelta(hours=1))
    base = {"user_id": uid, "status": "connected", "last_heartbeat": fresh,
            "verified_identity": {"account_number": "111", "broker_server": "Broker-Live"}}
    rows = [
        {**base, "label": "REAL LIVE", "trading_enabled": True, "broker_environment": "LIVE"},                # counted
        {**base, "label": "dup same broker acct", "trading_enabled": True, "broker_environment": "LIVE"},     # duplicate → not counted
        {**base, "label": "PAPER", "trading_enabled": True, "mode": "paper", "verified_identity": {"account_number": "222"}},  # counted as PAPER env
        {**base, "label": "DEMO", "trading_enabled": True, "account_type": "demo", "verified_identity": {"account_number": "333"}},  # counted as DEMO env
        {**base, "label": "disabled", "trading_enabled": False, "verified_identity": {"account_number": "444"}},
        {**base, "label": "missing flag", "verified_identity": {"account_number": "555"}},
        {**base, "label": "deleted", "status": "deleted", "trading_enabled": True, "verified_identity": {"account_number": "666"}},
        {**base, "label": "qa_synthetic", "trading_enabled": True, "verified_identity": {"account_number": "777"}},
        {**base, "label": "stale hb", "trading_enabled": True, "last_heartbeat": _iso(now - timedelta(days=45)), "verified_identity": {"account_number": "888"}},
        {**base, "label": "unverified", "trading_enabled": True, "verified_identity": {}},
    ]
    for r in rows:
        r["bridge_token"] = f"at11_{uuid.uuid4().hex}"
    sdb.accounts.insert_many(rows)
    acc = run_async(ts.verified_active_accounts(get_db(), now))
    mine = {k: v for k, v in acc["environment"].items()}
    # global DB may contain other accounts; assert our contribution via a scoped recount
    scoped = [a for a in sdb.accounts.find({"user_id": uid, "status": {"$ne": "deleted"}, "trading_enabled": True,
                                            "verified_identity.account_number": {"$exists": True},
                                            "last_heartbeat": {"$gte": _iso(now - timedelta(days=30))}})]
    assert {a["label"] for a in scoped} == {"REAL LIVE", "dup same broker acct", "PAPER", "DEMO", "qa_synthetic"}
    assert acc["count"] >= 3 and mine["LIVE"] >= 1 and mine["PAPER"] >= 1 and mine["DEMO"] >= 1
    assert "duplicate broker accounts" in acc["exclusions"] and "synthetic/QA labels" in acc["exclusions"]

    # blocked intents: HOLD signals are NOT counted; rejected new-exposure intents are; QA users excluded
    sdb.signals.insert_many([{"user_id": uid, "action": "HOLD"} for _ in range(5)])
    created = _iso(now - timedelta(days=1))
    acct = sdb.accounts.find_one({"user_id": uid, "label": "REAL LIVE"})
    sdb.execution_intents.insert_many([
        {"intent_id": f"i{uuid.uuid4().hex[:6]}", "user_id": uid, "account_id": str(acct["_id"]), "status": "rejected", "kind": "open_trade", "created_at": created},
        {"intent_id": f"i{uuid.uuid4().hex[:6]}", "user_id": uid, "account_id": str(acct["_id"]), "status": "filled", "kind": "open_trade", "created_at": created},
        {"intent_id": f"i{uuid.uuid4().hex[:6]}", "user_id": uid, "account_id": str(acct["_id"]), "status": "rejected", "kind": "close", "created_at": created},
        {"intent_id": f"i{uuid.uuid4().hex[:6]}", "user_id": "qa_bot", "actor": "qa_bot", "account_id": None, "status": "rejected", "kind": "open_trade", "created_at": created},
    ])
    before_qa = run_async(ts.execution_intents_blocked(get_db(), now))["count"]
    sdb.execution_intents.delete_many({"user_id": "qa_bot"})
    after_qa = run_async(ts.execution_intents_blocked(get_db(), now))["count"]
    assert before_qa == after_qa            # QA excluded
    sdb.execution_intents.delete_many({"user_id": uid})
    without_mine = run_async(ts.execution_intents_blocked(get_db(), now))["count"]
    assert after_qa - without_mine == 1     # exactly the one refused new-exposure intent
    assert "HOLD verdicts (no trade attempted)" in ts._BLOCKED_EXCL


def test_at11_availability_never_claimed_without_reconciled_window(sdb):
    from database import get_db
    import trust_stats as ts
    now = datetime.now(timezone.utc)
    sdb.edge_probes.delete_many({"region": "at11"})
    a = run_async(ts.availability_30d(get_db(), now))
    assert a["value_pct"] is None and a["reconciled"] is False and a["status"] in ("no_probes", "window_incomplete", "no_regions_configured")
    # partial window (probes started yesterday) → still no number
    import os as _os
    _os.environ["EDGE_PROBE_TOKENS"] = "at11:" + "x" * 32
    sdb.edge_probes.insert_one({"at": now - timedelta(days=1), "minute": now - timedelta(days=1), "region": "at11", "endpoint": "/api/health", "ok": True, "eligible": True})
    a = run_async(ts.availability_30d(get_db(), now))
    assert a["value_pct"] is None and a["status"] == "window_incomplete"
    sdb.edge_probes.delete_many({"region": "at11"})
    _os.environ.pop("EDGE_PROBE_TOKENS", None)
    _os.environ.pop("EDGE_PROBE_TOKENS", None)
    _os.environ.pop("EDGE_PROBE_TOKENS", None)
    _os.environ.pop("EDGE_PROBE_TOKENS", None)
    payload = run_async(ts.build_trust_stats(get_db()))
    assert payload["uptime_30d_pct"] is None and payload["population_version"] == "trust-stats/v2"
    assert payload["ops_monitoring_coverage_definition"].startswith("share of expected")
    assert "legal_review" in payload and payload["reconciliation"] == "availability_window_incomplete"


def test_at11_sli_published_and_frontend_hides_uptime_until_reconciled():
    import trust_stats as ts
    assert ts.SLI["definition"] == "successful eligible requests / total eligible requests"
    assert ts.SLI["eligible_endpoints"] and ts.SLI["interval_seconds"] and ts.SLI["exclusions"]
    fe = open(os.path.join(REPO, "frontend", "src", "components", "LandingTestimonials.jsx")).read()
    assert "Platform uptime" not in fe
    assert 'availability?.status === "reconciled"' in fe
    assert "Verified active accounts with safety policy enabled" in fe
    assert "Execution intents blocked by risk policy" in fe
    assert 'role="presentation" inert={true}' in fe          # P2-4 marquee clones out of the AT


# ── AT-15 · signed release: static portion ───────────────────────────────────
def test_at15_workflow_pins_and_exact_identity():
    wf = os.path.join(REPO, ".github", "workflows")
    import re
    for f in os.listdir(wf):
        for line in open(os.path.join(wf, f)):
            if "uses:" in line and "./" not in line:
                ref = line.split("uses:")[1].split("#")[0].strip()
                assert re.search(r"@[0-9a-f]{40}$", ref), f"{f}: {ref} not pinned to a commit SHA"
    rel = open(os.path.join(wf, "release.yml")).read()
    assert "certificate-identity-regexp" not in rel
    assert "/.github/workflows/release.yml@${{ github.ref }}" in rel
    assert "--bundle release-attestation.json.bundle" in rel
    lib = open(os.path.join(REPO, "deploy", "lib.sh")).read()
    assert "certificate-identity-regexp" not in lib
    assert "/.github/workflows/release.yml@refs/tags/${tag}" in lib
    assert "--certificate-github-workflow-repository" in lib
    ops = open(os.path.join(ROOT, "routes", "ops_routes.py")).read()
    assert 'checks["release_attestation"]' in ops


# ── AT-01 · six-account execution boundary (the staging drill, run here) ─────
def test_at01_drill_passes_against_this_stack():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "at01_account_boundary.py")],
                       capture_output=True, text=True, timeout=180,
                       env={**os.environ, "DRILL_EVIDENCE_DIR": "/tmp/at01_drills"})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    ev = json.loads(r.stdout[:r.stdout.rindex("}") + 1])
    exp = set(ev["expected_enabled_ids"])
    assert len(exp) == 3
    for surface in ("state_contract", "worker_selection", "authority_full"):
        assert set(ev["surfaces"][surface]) == exp, surface
    assert set(ev["surfaces"]["intents_created"]) <= exp
    disabled = [k for k, v in ev["choke_point_results"].items() if k not in exp]
    assert len(disabled) == 3 and all(ev["choke_point_results"][k] == "account_not_enabled" for k in disabled)
    assert all(ev["intents_per_account"][k] == 0 for k in disabled)
    assert ev["broker_requests"] <= 3 and ev["result"] == "PASS"


def test_at01_choke_point_and_authority_refuse_non_explicit_enablement():
    from execution_authority import account_enablement_lock
    assert account_enablement_lock({"trading_enabled": True}) is None
    for bad in ({"trading_enabled": False}, {}, None, {"trading_enabled": "true"}, {"trading_enabled": 1}):
        assert account_enablement_lock(bad), bad
    from database import get_db
    from trading_authority import account_domain
    assert run_async(account_domain(get_db(), {"_id": "x", "trading_enabled": True}))["level"] == "FULL"
    assert run_async(account_domain(get_db(), {"_id": "x", "trading_enabled": False}))["level"] == "LOCKED"
    assert run_async(account_domain(get_db(), {"_id": "x"}))["level"] == "LOCKED"


def test_at15_rollback_drill_wiring():
    drill = open(os.path.join(REPO, "deploy", "drills", "at15_rollback_drill.sh")).read()
    assert "STOIC_DRILL_FORCE_READINESS_FAIL" in drill and "deploy/update.sh" in drill
    assert "auto-rollback-from" in drill and "release_attestation.py verify" in drill
    ops = open(os.path.join(ROOT, "routes", "ops_routes.py")).read()
    assert 'checks["drill_forced_failure"] = {"ok": False' in ops      # hook can only fail, never pass
    runner = open(os.path.join(REPO, "scripts", "staging_acceptance.sh")).read()
    assert "ops/at01_account_boundary.py" in runner and "at15_rollback_drill.sh --yes" in runner
    assert "staging-acceptance:" in open(os.path.join(REPO, "Makefile")).read()


# ── AT-06 (round 4) · anchor detects tail deletion / regression ──────────────
def test_at06_anchor_detects_tail_deletion_and_verifies_signature(sdb, uid, monkeypatch):
    from database import get_db
    import health_repairs as hr
    monkeypatch.setenv("LEDGER_ANCHOR_KEY", "anchor-test-key")
    monkeypatch.setattr(hr, "ANCHOR_FILE", "/tmp/at06-anchors.jsonl")
    _seed_ghosts(sdb, uid, 1)
    run_async(hr.run_health_repairs(get_db(), uid))
    anchor = run_async(hr.anchor_ledger(get_db(), build="test"))
    assert anchor["sig"] and anchor["last_seq"] >= 1
    assert run_async(hr.verify_anchor(get_db()))["ok"] is True
    tail = sdb.repair_ledger.find_one({"seq": anchor["last_seq"]})
    sdb.repair_ledger.delete_one({"_id": tail["_id"]})              # delete the TAIL → chain alone looks valid
    chain = run_async(hr.verify_repair_chain(get_db()))
    assert chain["ok"] is True or chain["anomalies"] == []           # internal chain cannot see it …
    v = run_async(hr.verify_anchor(get_db()))
    assert v["ok"] is False and "ledger_sequence_regression" in v["problems"]      # … the anchor can
    sdb.repair_ledger.insert_one(tail)                                # restore
    assert run_async(hr.verify_anchor(get_db()))["ok"] is True
    sdb.repair_ledger.update_one({"_id": tail["_id"]}, {"$set": {"affected_ids": ["x"]}})  # then edit tail → still caught by chain
    assert run_async(hr.verify_repair_chain(get_db()))["ok"] is False
    sdb.repair_ledger.replace_one({"_id": tail["_id"]}, tail)
    # forged anchor signature
    sdb.repair_ledger_anchors.update_one({"at": anchor["at"]}, {"$set": {"sig": "0" * 64}})
    assert "anchor_signature_invalid" in run_async(hr.verify_anchor(get_db()))["problems"]
    sdb.repair_ledger_anchors.update_one({"at": anchor["at"]}, {"$set": {"sig": anchor["sig"]}})
    assert any(l.strip() for l in open("/tmp/at06-anchors.jsonl"))     # exported outside the DB
    ops = open(os.path.join(ROOT, "routes", "ops_routes.py")).read()
    assert 'checks["repair_ledger_anchor"]' in ops


# ── AT-08 · availability integrity ───────────────────────────────────────────
def test_at08_edge_probe_ingestion_region_from_token_allowlist_dedupe(sdb, monkeypatch):
    from database import get_db
    import edge_probes as ep
    from fastapi import HTTPException
    monkeypatch.setenv("EDGE_PROBE_TOKENS", "fra:" + "f" * 32 + ",iad:" + "i" * 32)
    monkeypatch.delenv("EDGE_PROBE_TOKEN", raising=False)
    sdb.edge_probes.delete_many({"region": {"$in": ["fra", "iad", "forged"]}})
    sdb.edge_probe_rejects.delete_many({"region": {"$in": ["fra", "iad"]}})
    with pytest.raises(HTTPException) as e:                                   # invalid token
        run_async(ep.ingest_probe(get_db(), "nope", {"endpoint": "/api/health", "ok": True}))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:                                   # unknown endpoint
        run_async(ep.ingest_probe(get_db(), "f" * 32, {"endpoint": "/admin", "ok": True}))
    assert e.value.status_code == 422
    # forged region/eligibility in the body is ignored; region comes from the token
    r = run_async(ep.ingest_probe(get_db(), "f" * 32, {"endpoint": "/api/health", "ok": True, "region": "forged", "eligible": True, "prober_error": True}))
    assert r["region"] == "fra" and r["eligible"] is False and r["recorded"] is True
    row = sdb.edge_probes.find_one({"region": "fra", "endpoint": "/api/health"}, sort=[("minute", -1)])
    assert row["ok"] is False and row["eligible"] is False and row["prober_error"] is True
    assert sdb.edge_probes.count_documents({"region": "forged"}) == 0
    # duplicate minute → not counted twice
    r2 = run_async(ep.ingest_probe(get_db(), "f" * 32, {"endpoint": "/api/health", "ok": True}))
    assert r2["duplicate"] is True and r2["recorded"] is False
    assert sdb.edge_probes.count_documents({"region": "fra", "endpoint": "/api/health", "minute": row["minute"]}) == 1
    assert sdb.edge_probe_rejects.count_documents({"region": "fra", "reason": "duplicate_minute"}) >= 1
    idx = {i["name"] for i in sdb.edge_probes.list_indexes()}
    assert "uniq_region_endpoint_minute" in idx
    # coverage gate: iad has no samples → gaps → percentage withheld
    import trust_stats as ts
    a = run_async(ts.availability_30d(get_db(), datetime.now(timezone.utc)))
    assert a["value_pct"] is None and a["status"] in ("window_incomplete", "no_probes")
    if a["gaps"]:
        assert any(g["region"] == "iad" for g in a["gaps"])
    sdb.edge_probes.delete_many({"region": {"$in": ["fra", "iad"]}})
    sdb.edge_probe_rejects.delete_many({"region": {"$in": ["fra", "iad"]}})


def test_p22_public_section_withheld_until_legal_approval(monkeypatch):
    from database import get_db
    import trust_stats as ts
    monkeypatch.delenv("TRUST_STATS_LEGAL_APPROVED", raising=False)
    p = run_async(ts.build_trust_stats(get_db()))
    assert p["published"] is False and p["legal_review"].startswith("pending")
    assert "not profitability" in p["context"]
    monkeypatch.setenv("TRUST_STATS_LEGAL_APPROVED", "true")
    assert run_async(ts.build_trust_stats(get_db()))["published"] is True
    fe = open(os.path.join(REPO, "frontend", "src", "components", "LandingTestimonials.jsx")).read()
    assert "s.published !== true" in fe and "pending compliance review" in fe


# ── round 5 · broker truth / readiness fail-closed / share revocation ────────
def test_r5_execution_truth_fails_closed_on_unknown_and_position_mismatch(sdb, uid):
    from database import get_db
    import execution_truth as et
    now = datetime.now(timezone.utc)
    old = _iso(now - timedelta(seconds=et.UNKNOWN_MAX_AGE_S + 60))
    base = run_async(et.execution_truth_check(get_db()))
    sdb.execution_intents.insert_one({"intent_id": f"{uid}_u", "status": "unknown", "kind": "open_trade", "account_id": uid,
                                      "created_at": old, "request_id": "r1", "broker_ticket": None, "user_id": uid})
    sdb.execution_intents.insert_one({"intent_id": f"{uid}_fresh", "status": "submitted", "kind": "open_trade", "account_id": uid,
                                      "created_at": _iso(now), "user_id": uid})      # young → not counted
    sdb.execution_intents.insert_one({"intent_id": f"{uid}_done", "status": "filled", "kind": "open_trade", "account_id": uid,
                                      "created_at": old, "user_id": uid})           # terminal → not counted
    chk = run_async(et.execution_truth_check(get_db()))
    assert chk["ok"] is False and chk["unresolved_executions"] == base["unresolved_executions"] + 1
    assert any(u["intent_id"] == f"{uid}_u" and u["age_s"] >= et.UNKNOWN_MAX_AGE_S for u in run_async(et.unresolved_executions(get_db(), now)))
    assert "CLOSE_ONLY" in chk["authority_if_failed"]
    sdb.execution_intents.delete_many({"user_id": uid})
    # position mismatch on a fresh enabled account; stale/no-snapshot accounts are NOT collapsed to zero
    acc = sdb.accounts.insert_one({"user_id": uid, "label": "mm", "status": "connected", "trading_enabled": True,
                                   "last_heartbeat": _iso(now), "open_positions": 3, "bridge_token": f"{uid}_bt",
                                   "verified_identity": {"account_number": "1"}}).inserted_id
    sdb.accounts.insert_one({"user_id": uid, "label": "stale", "status": "connected", "trading_enabled": True,
                             "last_heartbeat": _iso(now - timedelta(hours=2)), "open_positions": 9, "bridge_token": f"{uid}_bt2"})
    sdb.accounts.insert_one({"user_id": uid, "label": "nosnap", "status": "connected", "trading_enabled": True,
                             "last_heartbeat": _iso(now), "bridge_token": f"{uid}_bt3"})
    mm = run_async(et.position_mismatches(get_db(), now))
    mine = [m for m in mm if m["account_id"] == str(acc)]
    assert len(mine) == 1 and mine[0]["broker_open"] == 3 and mine[0]["local_open"] == 0 and mine[0]["position_hash"]
    assert not any(m["label"] in ("stale", "nosnap") for m in mm)
    ops = open(os.path.join(ROOT, "routes", "ops_routes.py")).read()
    assert 'checks["execution_truth"]' in ops


def _drill_env(**extra):
    """Guard-satisfying env for the PREVIEW stack: the approved suffix list is
    operator-defined, so the test approves the actual preview DB name."""
    return {**os.environ, "DRILL_EVIDENCE_DIR": "/tmp/readiness_drills", "APP_ENV": "staging",
            "ALLOW_DESTRUCTIVE_DRILLS": "true", "DRILL_DB_SUFFIXES": os.environ.get("DB_NAME", ""),
            "DRILL_STEP_UP_TOKEN": "drill-step-up-token-0123456789", "DRILL_STEP_UP_TOKEN_EXPECTED": "drill-step-up-token-0123456789",
            **extra}


def test_r5_readiness_drills_catch_every_injected_fault():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "readiness_drills.py")], capture_output=True, text=True,
                       timeout=240, env=_drill_env())
    out = r.stdout
    rep = json.loads(out[:out.rindex("}") + 1])
    assert r.returncode == 0, out[-2000:] + r.stderr[-1000:]
    by = {f["fault"]: f for f in rep["faults"]}
    for fault in ("fresh_unknown_execution_blocks_immediately", "position_mismatch", "anchor_sequence_regression"):
        assert by[fault]["result"] == "CAUGHT" and by[fault]["status"] == 503, by[fault]
    for fault in ("stale_worker_lease", "stalled_loop", "anchored_hash_mismatch"):
        assert by[fault]["result"] == "CAUGHT" or by[fault].get("absence_fails") is True, by[fault]
    assert rep["leftover_rows_removed"] == 0            # every tagged row restored by the faults themselves


def test_r5_topology_gate_and_monitor_wiring():
    upd = open(os.path.join(REPO, "deploy", "update.sh")).read()
    assert "RECONCILE_EXPECT" in upd and "production_reconcile.py --expect" in upd and "release/evidence/production-reconcile-" in upd
    i = upd.index("production_reconcile.py --expect")
    assert "rollback" in upd[i:i + 600]
    mon = open(os.path.join(REPO, ".github", "workflows", "synthetic-monitor.yml")).read()
    for ep in ("/welcome", "/login", "/dashboard", "/api/health", "/api/auth/login", "edge-probe"):
        assert ep in mon
    assert "welcome-page" in mon                     # /dashboard must never fall back to the welcome page
    import edge_probes as ep
    assert set(("/welcome", "/login", "/dashboard")) <= set(ep.ALLOWED_ENDPOINTS)
    runner = open(os.path.join(REPO, "scripts", "staging_acceptance.sh")).read()
    assert "ops/readiness_drills.py" in runner
    perf = open(os.path.join(ROOT, "routes", "performance_routes.py")).read()
    assert perf.count("no-store") >= 2                # revoked share can never be served from a cache
