"""Audit round 6 — release-blocker corrections, asserted end to end.

P0  protected routes: app · login boundary · explicit outage — never /welcome
P1  execution truth IS the canonical authority (UNKNOWN blocks immediately,
    state-specific SLAs, BSON + ISO timestamps, mismatch → CLOSE_ONLY)
P1  per-region probe cadence, one SLI per cadence class (never merged)
P1  production 6/3/3 gate mandatory · signed · scoped · synthetic-excluded
P1  readiness drills refuse outside staging BEFORE the first write
P2  monitor telemetry-delivery failures are distinct from route failures
"""
import json
import os
import subprocess
import sys
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
    u = f"r6_{uuid.uuid4().hex[:10]}"
    yield u
    for col in ("accounts", "trades", "execution_intents", "trading_readiness", "bot_configs"):
        sdb[col].delete_many({"user_id": u})
    sdb.execution_intents.delete_many({"account_id": {"$regex": f"^{u}"}})
    sdb.edge_probes.delete_many({"region": {"$in": ["r6fra", "r6gha"]}})


def _iso(dt):
    return dt.isoformat()


# ── P1 · execution truth policy ──────────────────────────────────────────────
def test_r6_sla_policy_is_split_and_unknown_is_immediate():
    import execution_truth as et
    assert et.SLA_S["unknown"] == 0 and et.UNKNOWN_MAX_AGE_S == 0
    assert et.SLA_S["submitted"] == 120 and et.SLA_S["broker_pending"] == 300
    assert set(et.NON_TERMINAL_AFTER_BROKER) == {"unknown", "submitted", "dispatched", "broker_pending", "acked", "acknowledged"}
    assert et._parse_sla("submitted=5,bogus,acked=x")["submitted"] == 5


def test_r6_unresolved_uses_last_transition_and_both_timestamp_types(sdb, uid):
    from database import get_db
    import execution_truth as et
    now = datetime.now(timezone.utc)
    acc = f"{uid}_acc"
    sdb.execution_intents.insert_many([
        # fresh UNKNOWN → counted immediately (SLA 0)
        {"intent_id": f"{uid}_unk", "status": "unknown", "account_id": acc, "created_at": _iso(now), "user_id": uid},
        # submitted 30s ago (ISO) → inside 120s SLA → not counted
        {"intent_id": f"{uid}_sub_young", "status": "submitted", "account_id": acc, "created_at": _iso(now - timedelta(seconds=30)), "user_id": uid},
        # submitted created 10 min ago but LAST TRANSITION 20s ago → not counted (age from updated_at)
        {"intent_id": f"{uid}_sub_moved", "status": "submitted", "account_id": acc, "created_at": _iso(now - timedelta(minutes=10)),
         "updated_at": now - timedelta(seconds=20), "user_id": uid},
        # broker_pending as BSON datetime 6 min ago → past 300s SLA → counted
        {"intent_id": f"{uid}_bp_old", "status": "broker_pending", "account_id": acc, "created_at": now - timedelta(minutes=6), "user_id": uid},
        # acked, transitions carry the newest time (ISO) 10 min ago → counted
        {"intent_id": f"{uid}_ack_old", "status": "acked", "account_id": acc, "created_at": _iso(now - timedelta(hours=1)),
         "transitions": [{"to": "submitted", "at": _iso(now - timedelta(minutes=50))}, {"to": "acked", "at": _iso(now - timedelta(minutes=10))}], "user_id": uid},
        # terminal → never counted
        {"intent_id": f"{uid}_done", "status": "filled", "account_id": acc, "created_at": _iso(now - timedelta(hours=1)), "user_id": uid},
        # non-terminal with NO timestamp at all → unknown age → fail closed
        {"intent_id": f"{uid}_nots", "status": "dispatched", "account_id": acc, "user_id": uid},
    ])
    rows = run_async(et.unresolved_executions(get_db(), now, account_ids=[acc]))
    got = {r["intent_id"]: r for r in rows}
    assert set(got) == {f"{uid}_unk", f"{uid}_bp_old", f"{uid}_ack_old", f"{uid}_nots"}
    assert got[f"{uid}_unk"]["sla_s"] == 0 and got[f"{uid}_bp_old"]["sla_s"] == 300
    assert got[f"{uid}_nots"]["age_s"] is None
    auth = et.authority_for(rows, [])
    assert auth["level"] == "CLOSE_ONLY" and "UNKNOWN" in auth["reason"] and "settlement SLA" in auth["reason"]
    chk = run_async(et.execution_truth_check(get_db(), account_ids=[acc]))
    assert chk["ok"] is False and chk["authority"] == "CLOSE_ONLY" and chk["unresolved_executions"] == 4


def test_r6_canonical_authority_merges_execution_truth(sdb, uid):
    """Same policy on the authority domain, the choke-point gate and user readiness."""
    from database import get_db
    from trading_authority import compute_authority, enforce_new_trade, execution_domain
    from trading_readiness import readiness
    now = datetime.now(timezone.utc)
    acc_id = sdb.accounts.insert_one({"user_id": uid, "label": "R6 live", "mode": "live", "status": "connected",
                                      "trading_enabled": True, "last_heartbeat": _iso(now), "open_positions": 0,
                                      "bridge_token": f"{uid}_bt", "verified_identity": {"account_number": "1", "broker_server": "X"}}).inserted_id
    account = sdb.accounts.find_one({"_id": acc_id})
    sdb.bot_configs.insert_one({"user_id": uid, "account_id": str(acc_id), "active": True, "operational_mode": "autonomous_live"})
    # clean → FULL for this account
    d = run_async(execution_domain(get_db(), account))
    assert d["level"] == "FULL"
    # fresh UNKNOWN on THIS account → CLOSE_ONLY immediately everywhere
    sdb.execution_intents.insert_one({"intent_id": f"{uid}_u", "status": "unknown", "account_id": str(acc_id),
                                      "created_at": _iso(now), "user_id": uid})
    d = run_async(execution_domain(get_db(), account))
    assert d["level"] == "CLOSE_ONLY" and "immediately" in d["reason"]
    snap = run_async(compute_authority(get_db(), account))
    assert snap["domains"]["execution"]["level"] == "CLOSE_ONLY" and snap["enforced_level"] == snap["level"]
    assert snap["hard_truth_fresh"] is False
    gate = run_async(enforce_new_trade(get_db(), account))
    assert gate["ok"] is False
    rd = run_async(readiness(get_db(), uid))
    codes = {r["code"]: r for r in rd["reasons"]}
    assert rd["level"] == "BLOCKED" and "RECONCILIATION_PENDING" in codes
    assert "UNKNOWN" in codes["RECONCILIATION_PENDING"]["message"]
    assert any(f"{uid}_u" in (a.get("reason") or "") for a in codes["RECONCILIATION_PENDING"]["accounts"])
    sdb.execution_intents.delete_many({"user_id": uid})
    # position mismatch (broker 2 vs local 0) → CLOSE_ONLY + visible recovery action
    sdb.accounts.update_one({"_id": acc_id}, {"$set": {"open_positions": 2}})
    account = sdb.accounts.find_one({"_id": acc_id})
    d = run_async(execution_domain(get_db(), account))
    assert d["level"] == "CLOSE_ONLY" and "mismatch" in d["reason"]
    rd = run_async(readiness(get_db(), uid))
    codes = {r["code"]: r for r in rd["reasons"]}
    rp = codes["RECONCILIATION_PENDING"]
    assert "mismatch" in rp["message"] and "broker reports 2 open, local projection 0" in rp["accounts"][0]["reason"]
    assert "reconciliation" in rp["recovery"].lower()
    # aged submitted (BSON datetime, past 120s) → also CLOSE_ONLY
    sdb.accounts.update_one({"_id": acc_id}, {"$set": {"open_positions": 0}})
    sdb.execution_intents.insert_one({"intent_id": f"{uid}_s", "status": "submitted", "account_id": str(acc_id),
                                      "created_at": now - timedelta(seconds=200), "user_id": uid})
    account = sdb.accounts.find_one({"_id": acc_id})
    d = run_async(execution_domain(get_db(), account))
    assert d["level"] == "CLOSE_ONLY" and "settlement SLA" in d["reason"]


# ── P1 · probe cadence and separate SLIs ─────────────────────────────────────
def test_r6_region_cadence_explicit_and_coverage_math(sdb, monkeypatch):
    from database import get_db
    import edge_probes as ep
    import trust_stats as ts
    monkeypatch.setenv("EDGE_PROBE_TOKENS", "r6fra:" + "a" * 20 + ",r6gha:" + "b" * 20)
    monkeypatch.setenv("EDGE_PROBE_CADENCE", "r6gha=300,r6fra=45,unknownregion=600")   # 45 rejected (not a multiple of 60)
    cad = ep.region_cadences()
    assert cad == {"r6fra": 60, "r6gha": 300}
    assert ep.region_cadence("r6gha") == 300 and ep.region_cadence("nope") == 60
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=60)
    docs = []
    for ep_ in ep.ALLOWED_ENDPOINTS:
        for i in range(60):        # fra: every minute → 100 %
            docs.append({"region": "r6fra", "endpoint": ep_, "minute": start + timedelta(minutes=i), "ok": i != 3, "eligible": True, "at": now})
        for i in range(0, 60, 5):  # gha: every 5 minutes → 12 samples = 100 % of ITS cadence
            docs.append({"region": "r6gha", "endpoint": ep_, "minute": start + timedelta(minutes=i), "ok": True, "eligible": True, "at": now})
    sdb.edge_probes.insert_many(docs)
    cov = run_async(ep.coverage(get_db(), start, now))
    by = {(s["region"], s["endpoint"]): s for s in cov["series"]}
    assert by[("r6fra", "/")]["expected"] == 60 and by[("r6fra", "/")]["coverage_pct"] == 100.0
    assert by[("r6gha", "/")]["expected"] == 12 and by[("r6gha", "/")]["cadence_s"] == 300 and by[("r6gha", "/")]["coverage_pct"] == 100.0
    assert cov["gaps"] == [] and cov["expected_by_cadence"] == {"60": 60, "300": 12}
    # trust stats: one SLI per cadence class; the 300 s series is NEVER part of value_pct
    monkeypatch.setattr(ts, "timedelta", timedelta)
    out = run_async(ts.availability_30d(get_db(), now))
    assert out["sli"]["regions_in_this_sli"] == ["r6fra"]
    sec = out["secondary_slis"]["edge_availability_30d_300s"]
    assert sec["regions"] == ["r6gha"] and sec["cadence_s"] == 300 and "NOT part of the one-minute SLI" in sec["definition"]
    assert out["status"] in ("window_incomplete", "reconciled")       # 60 min of data can't fill a 30-day window
    if out["reconciled"]:
        assert out["probes"] == 300 and out["successful"] == 295          # fra only (5 endpoints × 60 samples, one failure each)


def test_r6_ingest_returns_cadence(sdb, monkeypatch):
    from database import get_db
    import edge_probes as ep
    monkeypatch.setenv("EDGE_PROBE_TOKENS", "r6gha:" + "b" * 20)
    monkeypatch.setenv("EDGE_PROBE_CADENCE", "r6gha=300")
    out = run_async(ep.ingest_probe(get_db(), "b" * 20, {"endpoint": "/login", "ok": True, "status_code": 200, "latency_ms": 5}))
    assert out["region"] == "r6gha" and out["cadence_s"] == 300 and (out["recorded"] or out["duplicate"])
    sys.path.insert(0, os.path.join(REPO, "ops"))
    from verify_probe_ack import check
    assert check(json.dumps(out), "/login", "r6gha", 300) == []
    assert any("cadence" in p for p in check(json.dumps(out), "/login", "r6gha", 60))
    assert any("region" in p for p in check(json.dumps(out), "/login", "fra", 300))
    assert any("endpoint" in p for p in check(json.dumps(out), "/", "r6gha", 300))
    assert check("<html>", "/", "r6gha", 300)[0].startswith("non-JSON")


# ── P1 · production reconciliation gates ─────────────────────────────────────
def _reconcile(args, env):
    return subprocess.run([sys.executable, os.path.join(ROOT, "ops", "production_reconcile.py"), *args],
                          capture_output=True, text=True, timeout=120, env={**os.environ, "DRILL_EVIDENCE_DIR": "/tmp/r6", **env})


def test_r6_reconcile_refuses_in_production_without_scope_sha_key():
    r = _reconcile(["--expect", "6/3/3"], {"APP_ENV": "production", "GIT_SHA": "unknown", "LEDGER_ANCHOR_KEY": ""})
    assert r.returncode == 2, r.stdout + r.stderr
    rep = json.loads(r.stdout)
    assert rep["result"] == "REFUSED"
    joined = " ".join(rep["problems"])
    assert "scope" in joined and "GIT_SHA" in joined and "LEDGER_ANCHOR_KEY" in joined
    # malformed policy is refused even outside production
    r = _reconcile(["--expect", "six/3/3"], {"APP_ENV": "development"})
    assert r.returncode == 2 and "malformed" in r.stdout


def test_r6_reconcile_scoped_signed_and_excludes_synthetic(sdb, uid):
    now = datetime.now(timezone.utc)
    real = sdb.accounts.insert_one({"user_id": uid, "label": "Prod A", "status": "connected", "trading_enabled": True,
                                    "mode": "live", "broker_server": "Broker-Live", "last_heartbeat": _iso(now),
                                    "bridge_token": f"{uid}_bt1", "verified_identity": {"account_number": "11", "broker_server": "Broker-Live"}}).inserted_id
    sdb.accounts.insert_one({"user_id": uid, "label": "Prod B", "status": "connected", "trading_enabled": False, "mode": "live",
                             "broker_server": "Broker-Live", "bridge_token": f"{uid}_bt2"})
    sdb.accounts.insert_one({"user_id": uid, "label": "chaos_acct_9", "status": "connected", "trading_enabled": True,
                             "mode": "live", "last_heartbeat": _iso(now), "bridge_token": f"{uid}_bt3"})       # synthetic → excluded, never counted
    sdb.bot_configs.insert_one({"user_id": uid, "account_id": str(real), "enabled": True, "active": True, "strategy": "s"})
    env = {"APP_ENV": "production", "GIT_SHA": "deadbeef", "LEDGER_ANCHOR_KEY": "r6-dedicated-key"}
    r = _reconcile(["--expect", "2/1/1", "--scope-user", uid, "--expect-ids", str(real)], env)
    rep = json.loads(r.stdout)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr
    assert rep["result"] == "PASS" and rep["strict"] is True and rep["scope"] == {"user_id": uid}
    assert rep["totals"]["excluded_synthetic"] == 1 and rep["excluded"][0]["label"] == "chaos_acct_9"
    assert rep["checks_ids"] == {"enabled_ids_exact": True, "bot_ids_exact": True, "fresh_ids_exact": True}
    assert isinstance(rep["signature"], str) and len(rep["signature"]) == 64 and rep["signature_key"] == "LEDGER_ANCHOR_KEY"
    # wrong identity set → FAIL even though the counts match
    r = _reconcile(["--expect", "2/1/1", "--scope-user", uid, "--expect-ids", str(ObjectId())], env)
    rep = json.loads(r.stdout)
    assert r.returncode == 1 and rep["result"] == "FAIL" and "enabled_ids_exact" in rep["failed_checks"]
    # no key outside production → unsigned evidence is a FAIL, never a silent PASS
    # (empty values beat the script's load_dotenv, which never overrides existing env)
    r = _reconcile(["--expect", "2/1/1", "--scope-user", uid], {"APP_ENV": "development", "LEDGER_ANCHOR_KEY": "", "JWT_SECRET": ""})
    rep = json.loads(r.stdout)
    assert r.returncode == 1 and rep["signature"] is None and "evidence_unsigned" in rep["failed_checks"]
    sdb.bot_configs.delete_many({"user_id": uid})


def test_r6_update_sh_makes_topology_gate_mandatory_in_production():
    upd = open(os.path.join(REPO, "deploy", "update.sh")).read()
    for needle in ('[ "${APP_ENV_VAL}" = "production" ]', "production requires RECONCILE_EXPECT",
                   "malformed (want N/N/N)", "differs from the approved policy", "RECONCILE_SCOPE_USER_ID",
                   "LEDGER_ANCHOR_KEY", "evidence unsigned or malformed", "--strict"):
        assert needle in upd, needle
    # every refusal rolls back
    seg = upd[upd.index("APP_ENV_VAL=$(_envval APP_ENV)"):upd.index("pruning dangling images")]
    assert seg.count("rollback") >= 7


# ── P1 · readiness drill guard ───────────────────────────────────────────────
def test_r6_readiness_drill_guard_logic():
    sys.path.insert(0, os.path.join(ROOT, "ops"))
    from readiness_drills import guard
    good = {"APP_ENV": "staging", "ALLOW_DESTRUCTIVE_DRILLS": "true", "DB_NAME": "stoic_staging",
            "DRILL_STEP_UP_TOKEN": "t" * 20, "DRILL_STEP_UP_TOKEN_EXPECTED": "t" * 20}
    assert guard(good) == []
    assert any("APP_ENV" in p for p in guard({**good, "APP_ENV": "production"}))
    assert any("ALLOW_DESTRUCTIVE_DRILLS" in p for p in guard({**good, "ALLOW_DESTRUCTIVE_DRILLS": "yes"}))
    assert any("suffix" in p for p in guard({**good, "DB_NAME": "stoic_prod"}))
    assert any("token" in p for p in guard({**good, "DRILL_STEP_UP_TOKEN": "wrong"}))
    assert any("token" in p for p in guard({**good, "DRILL_STEP_UP_TOKEN_EXPECTED": "short", "DRILL_STEP_UP_TOKEN": "short"}))
    assert len(guard({"DB_NAME": "stoic"})) == 4


def test_r6_readiness_drill_refuses_production_before_first_write(sdb):
    before = {c: sdb[c].count_documents({}) for c in ("drill_journal", "execution_intents", "accounts", "repair_ledger_anchors", "worker_leases")}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "readiness_drills.py")], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "APP_ENV": "production", "ALLOW_DESTRUCTIVE_DRILLS": "true",
                            "DRILL_STEP_UP_TOKEN": "t" * 20, "DRILL_STEP_UP_TOKEN_EXPECTED": "t" * 20})
    assert r.returncode == 2 and json.loads(r.stdout)["result"] == "REFUSED"
    assert "REFUSED" in r.stderr
    after = {c: sdb[c].count_documents({}) for c in before}
    assert after == before                                     # zero writes
    assert sdb.accounts.count_documents({"drill_tag": {"$exists": True}}) == 0


def test_r6_drill_recovers_from_a_crashed_run(sdb):
    """Kill-after-injection proof: a journaled original is restored by the next run."""
    sys.path.insert(0, os.path.join(ROOT, "ops"))
    from database import get_db
    from readiness_drills import recover
    tag = f"drill_crash_{uuid.uuid4().hex[:6]}"
    lease_id = f"r6-lease-{tag}"
    sdb.worker_leases.insert_one({"_id": lease_id, "expires_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=5)), "role": "test"})
    original = sdb.worker_leases.find_one({"_id": lease_id})
    sdb.drill_journal.insert_one({"_id": f"{tag}:worker_leases:{lease_id}", "drill_tag": tag, "collection": "worker_leases", "doc": original})
    sdb.worker_leases.update_one({"_id": lease_id}, {"$set": {"expires_at": "2000-01-01T00:00:00+00:00"}})
    sdb.accounts.insert_one({"user_id": tag, "label": tag, "drill_tag": tag, "bridge_token": f"{tag}_bt"})
    n = run_async(recover(get_db()))
    assert n >= 2
    assert sdb.worker_leases.find_one({"_id": lease_id})["expires_at"] == original["expires_at"]
    assert sdb.accounts.count_documents({"drill_tag": tag}) == 0 and sdb.drill_journal.count_documents({"drill_tag": tag}) == 0
    sdb.worker_leases.delete_one({"_id": lease_id})


# ── P0 / P2 · frontend protected-route states + monitor wiring ───────────────
def test_r6_protected_routes_never_fall_back_to_welcome():
    pr = open(os.path.join(REPO, "frontend", "src", "components", "ProtectedRoute.jsx")).read()
    assert "BackendOutage" in pr and 'publicFallback = "/login"' in pr
    assert 'to="/welcome"' not in pr
    app = open(os.path.join(REPO, "frontend", "src", "App.jsx")).read()
    assert 'path="/dashboard" element={<ProtectedRoute><Dashboard />' in app
    assert 'path="/" element={<ProtectedRoute publicFallback="/welcome">' in app     # root is the marketing entrypoint only
    auth = open(os.path.join(REPO, "frontend", "src", "context", "AuthContext.jsx")).read()
    assert "status === 401 || status === 403" in auth and "setOutage" in auth and "x-request-id" in auth
    outage = open(os.path.join(REPO, "frontend", "src", "components", "BackendOutage.jsx")).read()
    for tid in ("backend-outage-screen", "backend-outage-correlation-id", "backend-outage-retry-button", "backend-outage-retry-countdown"):
        assert tid in outage
    welcome = open(os.path.join(REPO, "frontend", "src", "pages", "WelcomeTrailer.jsx")).read()
    assert 'data-testid="welcome-page"' in welcome


def test_r6_monitor_separates_cadences_and_telemetry_failures():
    mon = open(os.path.join(REPO, ".github", "workflows", "synthetic-monitor.yml")).read()
    assert "verify_probe_ack.py" in mon and "TELEMETRY FAILURE" in mon and "ROUTE FAILURE" in mon
    assert "|| true" not in mon.split("edge-probe")[1].split("login-failure")[0]
    assert "gha 300" in mon and "edge_availability_30d_300s" in mon
    assert "synthetic_browser.py" in mon and "*/15 * * * *" in mon
    assert os.path.exists(os.path.join(REPO, "ops", "synthetic_browser.py"))
    br = open(os.path.join(REPO, "ops", "synthetic_browser.py")).read()
    for needle in ("backend-outage-screen", "welcome-page", "login-form", "/dashboard"):
        assert needle in br


# ── security audit #7 · SEC-001 key separation ───────────────────────────────
def test_sec7_production_refuses_signers_sharing_jwt_secret():
    src = open(os.path.join(ROOT, "server.py")).read()
    i = src.index("security audit #7 SEC-001")
    seg = src[i:i + 900]
    assert '"ORDER_AUTH_SECRET", "LEDGER_ANCHOR_KEY"' in seg and "val == jwt_secret" in seg and "RuntimeError" in seg
    from deploy_preflight import run_preflight
    import deploy_preflight as dp
    saved = dict(os.environ)
    try:
        os.environ.update({"JWT_SECRET": "same-secret-value-0123456789", "ORDER_AUTH_SECRET": "same-secret-value-0123456789"})
        os.environ.pop("LEDGER_ANCHOR_KEY", None)
        out = run_preflight()
        by = {c["id"]: c for c in out["checks"]}
        assert by["order_auth_key"]["status"] == "fail" and "equals JWT_SECRET" in by["order_auth_key"]["current"]
        assert by["ledger_anchor_key"]["status"] == "fail"
        os.environ.update({"ORDER_AUTH_SECRET": "o" * 40, "LEDGER_ANCHOR_KEY": "l" * 40})
        by = {c["id"]: c for c in run_preflight()["checks"]}
        assert by["order_auth_key"]["status"] == "pass" and by["ledger_anchor_key"]["status"] == "pass"
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert "ORDER_AUTH_SECRET=" in open(os.path.join(ROOT, ".env.example")).read()


# ── audit round 7 P2 · exact backlog accounting under a large incident ───────
def test_r7_backlog_counts_exact_and_truncation_flagged(sdb, uid):
    from database import get_db
    import execution_truth as et
    now = datetime.now(timezone.utc)
    acc = f"{uid}_big"
    old = now - timedelta(minutes=30)
    docs = [{"intent_id": f"{uid}_s{i}", "status": "submitted", "account_id": acc, "created_at": old, "user_id": uid} for i in range(5200)]
    docs.append({"intent_id": f"{uid}_fresh_unknown", "status": "unknown", "account_id": acc, "created_at": now, "user_id": uid})
    sdb.execution_intents.insert_many(docs, ordered=False)
    b = run_async(et.unresolved_backlog(get_db(), now, account_ids=[acc]))
    assert b["total"] == 5201 and b["by_status"] == {"submitted": 5200, "unknown": 1}
    assert b["truncated"] is True and len(b["exemplars"]) == et.EXEMPLAR_LIMIT
    assert b["newest_unknown_age_s"] is not None and b["newest_unknown_age_s"] <= 5      # the recent UNKNOWN is still visible
    chk = run_async(et.execution_truth_check(get_db(), account_ids=[acc]))
    assert chk["unresolved_executions"] == 5201 and chk["unresolved_truncated"] is True and chk["authority"] == "CLOSE_ONLY"
    assert "1 execution(s) UNKNOWN" in chk["authority_reason"] and "5200 broker-accepted" in chk["authority_reason"]
    sdb.execution_intents.delete_many({"account_id": acc})


# ── audit round 7 · credentials, environment marker, evidence bundle, monitor ─
def test_r7_no_default_credential_literals_in_source():
    import subprocess as sp
    r = sp.run(["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.jsx", "--include=*.sh", "--include=*.yml",
                "--include=*.ps1", "--include=*.ts", "-E", "['\"]admin123['\"]", "backend", "frontend/src", "deploy", "scripts", "ops", ".github", "e2e/tests"],
               capture_output=True, text=True, cwd=REPO)
    hits = [l for l in r.stdout.splitlines() if "__pycache__" not in l]
    assert not hits, "default credential literals in source:\n" + "\n".join(hits)
    from live_target import is_known_default_password, admin_credentials
    assert is_known_default_password("admin" + "123") and not is_known_default_password("T" + "x" * 30)
    saved = {k: os.environ.pop(k) for k in ("TEST_ADMIN_EMAIL", "TEST_ADMIN_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD") if k in os.environ}
    try:
        with pytest.raises(RuntimeError):
            admin_credentials(strict=True)                          # pre-network refusal
        os.environ.update({"TEST_ADMIN_EMAIL": "a@b", "TEST_ADMIN_PASSWORD": "admin" + "123"})
        with pytest.raises(RuntimeError, match="KNOWN DEFAULT"):
            admin_credentials(strict=True)
    finally:
        for k in ("TEST_ADMIN_EMAIL", "TEST_ADMIN_PASSWORD"):
            os.environ.pop(k, None)
        os.environ.update(saved)
    seed_src = open(os.path.join(ROOT, "seed.py")).read()
    assert ('"admin' + '123"') not in seed_src and "_is_known_default_password" in seed_src
    ci = open(os.path.join(REPO, ".github", "workflows", "ci.yml")).read()
    assert ('ADMIN_PASSWORD="admin' + '123"') not in ci and "TEST_ADMIN_PASSWORD" in ci


def test_r7_environment_marker_signature():
    from environment_marker import mutation_guard, sign_environment, verify_environment
    key = "k" * 32
    saved = os.environ.get("LEDGER_ANCHOR_KEY")
    os.environ["LEDGER_ANCHOR_KEY"] = key
    try:
        m = sign_environment("Staging", "abc123")
        assert m["environment"] == "staging" and len(m["env_sig"]) == 64
        assert verify_environment({**m, "build_sha": "abc123"}, key)
        assert not verify_environment({**m, "build_sha": "other"}, key)
        assert not verify_environment({**m, "build_sha": "abc123"}, "wrong")
        good = {**m, "build_sha": "abc123"}
        env = {"ALLOW_X": "true", "TOK": "t" * 20, "LEDGER_ANCHOR_KEY": key, "DB_NAME": "stoic_staging"}
        assert mutation_guard("https://staging.example.test", good, env, allow_flag="ALLOW_X", token_var="TOK") == []
        p = mutation_guard("https://stoicaibot.com", {**good, "environment": "production"}, env, allow_flag="ALLOW_X", token_var="TOK")
        assert any("production hostname" in x for x in p) and any("environment='production'" in x for x in p)
    finally:
        if saved is None:
            os.environ.pop("LEDGER_ANCHOR_KEY", None)
        else:
            os.environ["LEDGER_ANCHOR_KEY"] = saved
    health = __import__("requests").get(f"{os.environ['REACT_APP_BACKEND_URL']}/api/health", timeout=15).json()
    assert health.get("environment") and health.get("env_sig")


def test_r7_prepromotion_bundle_refuses_and_fails_closed():
    env = {**os.environ, "GIT_SHA": "unknown", "LEDGER_ANCHOR_KEY": ""}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "prepromotion_evidence.py"), "--expect", "6/3/3"],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 2 and json.loads(r.stdout)["result"] == "REFUSED"
    joined = r.stdout
    assert "scope-user" in joined and "GIT_SHA" in joined and "LEDGER_ANCHOR_KEY" in joined
    # a tenant with NO enabled accounts can never PASS (fail closed), yet the bundle is signed + build-bound
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "prepromotion_evidence.py"), "--expect", "6/3/3",
                        "--scope-user", "nobody_" + uuid.uuid4().hex[:6]],
                       capture_output=True, text=True, timeout=180, env={**os.environ, "GIT_SHA": "deadbeef", "LEDGER_ANCHOR_KEY": "k" * 32,
                                                                          "DRILL_EVIDENCE_DIR": "/tmp/pp"})
    out = r.stdout[:r.stdout.rindex("}") + 1]
    d = json.loads(out)
    assert r.returncode == 1 and d["result"] == "FAIL" and d["build"] == "deadbeef" and len(d["signature"]) == 64
    for g in ("topology_reconciliation", "all_enabled_live", "zero_unresolved_executions", "platform_authority_full",
              "authority_ui_enforced_agree", "bot_health_caps_clear", "performance_reconciled_attestable"):
        assert g in d["gates"], g
    assert "all_enabled_live" in d["failed_gates"]
    upd = open(os.path.join(REPO, "deploy", "update.sh")).read()
    assert "prepromotion_evidence.py" in upd and "pre-promotion evidence FAILED" in upd


def test_r7_monitor_and_browser_synthetic_strictness():
    mon = open(os.path.join(REPO, ".github", "workflows", "synthetic-monitor.yml")).read()
    assert "github.event.schedule == '*/5 * * * *'" in mon and "github.event.schedule == '*/15 * * * *'" in mon
    br = open(os.path.join(REPO, "ops", "synthetic_browser.py")).read()
    for needle in ("AMBIGUOUS_STATE", "len(states) != 1", "_classify", "BENIGN_CONSOLE", "uncaught_exception", "csp_violation", "build_sha"):
        assert needle in br, needle
    sys.path.insert(0, os.path.join(REPO, "ops"))
    from synthetic_browser import _classify
    assert _classify("Uncaught TypeError: x is not a function") == "uncaught_exception"
    assert _classify("Refused to load the script because it violates the Content Security Policy") == "csp_violation"
    assert _classify("Failed to fetch /api/health") == "failed_request"
    assert os.path.exists(os.path.join(REPO, "scripts", "verify_release.sh"))
