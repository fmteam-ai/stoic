"""v1.60.43 — open items after v1.60.42 (see docs/RELEASE_SUMMARY.md): single_ea_version vs signed previous, registry
pull retry (no host build for an attested release), held-live state, re-anchor in the image, drill probe target,
D1/D2 publish, P5 counters from every process, loop heartbeats persisted, CLOSE_ONLY alert, shared calendar cache,
EA fleet / canary panel endpoint."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


# ── 1 · single_ea_version accepts current + signed previous ──────────────────────────────────────────────
def test_accepted_ea_versions_come_from_the_signed_record(monkeypatch):
    import ea_capabilities as ec
    monkeypatch.setattr(ec, "_signed_release_record", lambda: {"version": "1.65", "ex5_sha256": "a" * 64,
                                                                "previous": {"version": "1.64", "ex5_sha256": "b" * 64}})
    assert ec.accepted_ea_versions() == ["1.65", "1.64"]
    monkeypatch.setattr(ec, "_signed_release_record", lambda: None)
    assert ec.accepted_ea_versions() == []


def test_prepromotion_gate_logic_mixed_fleet_on_accepted_versions_passes():
    src = _read("backend", "ops", "prepromotion_evidence.py")
    assert "accepted_ea_versions" in src and 'ea_versions <= accepted' in src and 'gates["ea_versions_accepted"]' in src
    # the gate expression, evaluated as written
    en_rows = [{"ea_version": "1.64"}] * 3 + [{"ea_version": "1.65"}]
    ea_versions = {x["ea_version"] for x in en_rows}
    accepted = {"1.65", "1.64"}
    assert bool(en_rows) and all(ea_versions) and ea_versions <= accepted
    assert not ({"1.63", "1.64"} <= accepted)                                   # an unaccepted version still fails


# ── 2 · registry pull: retry with timeout, never build for an attested release ───────────────────────────
def test_pull_with_retry_and_no_build_fallback():
    lib = _read("deploy", "lib.sh")
    assert 'pull_with_retry "${d}" || return 1' in lib
    assert 'timeout -k 10 "${per}" docker pull -q "${d}"' in lib and 'PULL_ATTEMPTS:-3' in lib and 'PULL_TIMEOUT_SEC:-300' in lib
    assert "registry-fallback-build" not in lib and "registry-pull-failed" in lib
    auto = lib[lib.index("if registry_available; then"):lib.index("esac", lib.index("if registry_available; then"))]
    assert "build_with_provenance" not in auto.split("else", 2)[1]              # the pull-failed branch does not build


def test_pull_with_retry_bash_behaviour(tmp_path):
    import subprocess
    lib = _read("deploy", "lib.sh")
    fn = lib[lib.index("pull_with_retry() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/bash\necho $$ >> " + str(tmp_path / "calls") + "\nsleep 30\n")
    (fake / "docker").chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "PULL_ATTEMPTS": "2", "PULL_TIMEOUT_SEC": "1"}
    r = subprocess.run(["/bin/bash", "-c", fn + "\npull_with_retry ghcr.io/x@sha256:abc"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 1 and r.stdout.count("timed out after 1s") == 2 and "pull failed after 2 attempts" in r.stdout
    assert len(open(tmp_path / "calls").read().split()) == 2


# ── 3 · held-live state ───────────────────────────────────────────────────────────────────────────────────
def test_held_run_records_held_live_and_release_id_shows_it(tmp_path, monkeypatch):
    upd = _read("deploy", "update.sh")
    assert 'record_held_live "${1:-verification failed}"' in upd and '"deployment_state": "held_live"' in upd
    assert "held-live ref=${REF} prev=${PREV}" in upd
    assert "deploy/releases/deployment_state.json" in _read("deploy", "lib.sh").split("publish_release_truth() {")[1].split("}")[0]
    import release_truth as rt
    monkeypatch.setattr(rt, "STATE_DIR", str(tmp_path))
    (tmp_path / "release").mkdir()
    json.dump({"deployment_state": "held_live", "held": {"running_sha": "abc1234", "ref": "v1.60.42", "previous_sha": "def5678", "reason": "verification failed"}},
              open(tmp_path / "release" / "deployment_state.json", "w"))
    rid = rt.release_id()
    assert rid["deployment_state"] == "held_live" and rid["held"]["running_sha"] == "abc1234" and "HELD-LIVE" in rid["id"]
    json.dump({"deployment_state": "release_ready"}, open(tmp_path / "release" / "deployment_state.json", "w"))
    rid = rt.release_id()
    assert rid["held"] is None and "HELD-LIVE" not in rid["id"]


# ── 4 · re-anchor shipped in the image + wrapper ──────────────────────────────────────────────────────────
def test_reanchor_script_in_image_and_wrapper_documented():
    assert "COPY scripts/reanchor_audit_chain.py scripts/check_ea_fleet_compat.py scripts/relabel_deferred_shadows.py /app/scripts/" in _read("Dockerfile.backend")
    w = _read("deploy", "reanchor-audit-chain.sh")
    assert "docker compose exec -T backend python /app/scripts/reanchor_audit_chain.py" in w and 'python - "$@" < scripts/reanchor_audit_chain.py' in w
    assert os.access(os.path.join(ROOT, "deploy", "reanchor-audit-chain.sh"), os.X_OK)
    doc = _read("docs", "AUDIT_CHAIN_REANCHOR.md")
    assert "deploy/reanchor-audit-chain.sh --apply" in doc and "inside the backend image" in doc


# ── 5 · nightly drill probe target ────────────────────────────────────────────────────────────────────────
def test_unauthorized_api_probe_targets_the_backend_service(monkeypatch):
    import runtime_validation as rv
    monkeypatch.delenv("INTERNAL_API_BASE", raising=False)
    monkeypatch.delenv("BACKGROUND_WORKERS_IN_PROCESS", raising=False)
    assert rv.internal_api_base() == "http://backend:8001"
    monkeypatch.setenv("BACKGROUND_WORKERS_IN_PROCESS", "true")
    assert rv.internal_api_base() == "http://localhost:8001"
    monkeypatch.setenv("INTERNAL_API_BASE", "http://api.internal:9000/")
    assert rv.internal_api_base() == "http://api.internal:9000"
    assert 'base_url="http://localhost:8001"' not in _read("backend", "runtime_validation.py")


# ── 6 · D1/D2 publish must not skip silently on the daily job; P5 counters from every process ────────────
def test_daily_dependency_audit_requires_publish_secrets():
    wf = _read(".github", "workflows", "dependency-audit.yml")
    assert wf.count("publish_dependency_audit.py --require") == 2
    src = _read("scripts", "publish_dependency_audit.py")
    assert 'if a.require:' in src and "return 1" in src.split("if a.require:")[1][:600]


def test_p5_counts_an_empty_published_row_as_data():
    from security_agent.checks.integrity_platform_trading import P5
    db = FakeDb()
    db.security_signals.rows.append({"kind": "swallow_counters", "process": "worker-trading", "at": datetime.now(timezone.utc).isoformat(), "counters": {}})
    with patch("silent_failures.swallow_counters", lambda: {}):
        out = _run(P5(db, {}))
    assert "no_data" not in json.dumps(out, default=str)
    db2 = FakeDb()
    with patch("silent_failures.swallow_counters", lambda: {}):
        out = _run(P5(db2, {}))
    assert "no_data" in json.dumps(out, default=str)


def test_workers_persist_loop_progress_and_publish_counters():
    import workers.base as wb
    db = FakeDb()
    wb.LOOP_PROGRESS.clear()
    wb.record_progress("ops_outbox_loop", processed=1, started_at=datetime.now(timezone.utc), interval_sec=20)
    wb.record_progress("_billing_loop", processed=1, interval_sec=3600)
    wb.record_progress("deploy_watch.loop", processed=1, interval_sec=30)
    _run(wb.publish_swallow_counters(db, "worker-reconciliation"))
    row = db.security_signals.rows[0]
    assert row["kind"] == "swallow_counters" and row["process"] == "worker-reconciliation" and row["counters"] == {}
    src = _read("backend", "workers", "base.py")
    keeper = src[src.index("async def _keep_lease") if "async def _keep_lease" in src else src.index("LEASE_RENEW_SEC)"):]
    assert 'await db.loop_progress.update_one({"_id": k}, {"$set": dict(v, worker=name, host=HOLDER)}, upsert=True)' in keeper
    assert "await publish_swallow_counters(db, name)" in keeper
    wb.LOOP_PROGRESS.clear()


# ── 7 · CLOSE_ONLY alert ──────────────────────────────────────────────────────────────────────────────────
def _acc(label, enabled=True):
    return {"_id": ObjectId(), "label": label, "trading_enabled": enabled, "mode": "live", "user_id": "u1"}


def test_authority_degraded_alert_after_15_min_and_fleet_alert_immediately():
    import alerting as al
    db = FakeDb()
    a1, a2 = _acc("RoboForex"), _acc("OnEquity")
    db.accounts.rows += [a1, a2]
    now = datetime.now(timezone.utc)
    snap = {"level": "CLOSE_ONLY", "domains": {"infrastructure": {"level": "CLOSE_ONLY", "reason": "EA binary not accepted"}}, "restricted_domains": ["infrastructure"]}
    active = set()
    with patch("trading_authority.compute_authority", AsyncMock(return_value=snap)):
        raised = _run(al._authority_alerts(db, now, active))
    kinds = [a["kind"] for a in db.ops_alerts.rows]
    assert "authority_fleet_close_only" in kinds and "authority_degraded" not in kinds     # fleet-wide fires at once, per-account waits
    assert "authority:fleet" in active and raised == 1
    assert len(db.authority_watch.rows) == 2
    # 16 minutes later, still degraded → per-account critical
    for w in db.authority_watch.rows:
        w["since"] = now - timedelta(minutes=16)
    with patch("trading_authority.compute_authority", AsyncMock(return_value=snap)):
        _run(al._authority_alerts(db, now, active))
    deg = [a for a in db.ops_alerts.rows if a["kind"] == "authority_degraded"]
    assert len(deg) == 2 and all(a["severity"] == "critical" for a in deg) and "16 min" in deg[0]["message"]
    # recovery clears the watch so the next degradation starts a fresh 15 min clock
    with patch("trading_authority.compute_authority", AsyncMock(return_value={"level": "FULL", "domains": {}})):
        active2 = set()
        _run(al._authority_alerts(db, now, active2))
    assert db.authority_watch.rows == [] and active2 == set()
    assert "authority_degraded" in al.EVALUATOR_KINDS and "authority_fleet_close_only" in al.EVALUATOR_KINDS
    assert "raised += await _authority_alerts(db, now, active)" in _read("backend", "alerting.py")


def test_single_degraded_account_does_not_trigger_the_fleet_alert():
    import alerting as al
    db = FakeDb()
    a1, a2 = _acc("A"), _acc("B")
    db.accounts.rows += [a1, a2]
    now = datetime.now(timezone.utc)

    async def _auth(_db, acc):
        return {"level": "CLOSE_ONLY" if acc["_id"] == a1["_id"] else "FULL", "domains": {}}
    with patch("trading_authority.compute_authority", _auth):
        _run(al._authority_alerts(db, now, set()))
    assert not any(a["kind"] == "authority_fleet_close_only" for a in db.ops_alerts.rows) and len(db.authority_watch.rows) == 1


# ── 8 · shared calendar cache ─────────────────────────────────────────────────────────────────────────────
def test_calendar_shared_cache_is_reused_by_another_process(monkeypatch):
    import economic_calendar as ec
    import time as _t
    db = FakeDb()
    monkeypatch.setattr("database.get_db", lambda: db)
    ec._cache.update({"events": None, "expires_at": 0, "fetched_at": 0, "last_error": None})
    for n in ec.SOURCE_ORDER:
        ec._sources[n].update({"events": None, "fetched_at": 0, "last_error": None})
    ev = [{"title": "NFP", "country": "USD", "impact": "high", "when": "2026-10-17T12:30:00+00:00", "when_ts": _t.time() + 3600,
           "forecast": "", "previous": "", "source": "forexfactory"}]
    ff = AsyncMock(return_value=ev)
    fc = AsyncMock(return_value=[])
    monkeypatch.setattr(ec, "_fetch_events", ff)
    monkeypatch.setattr(ec, "_fetch_financecalendar", fc)
    monkeypatch.setattr(ec, "_persist_status", AsyncMock())
    out = _run(ec.get_events())
    assert out and ff.await_count == 1 and db.platform_state.rows and db.platform_state.rows[0]["_id"] == ec.SHARED_CACHE_ID
    # a fresh process (empty in-memory cache) after a restart: served from the shared cache, no refetch
    ec._cache.update({"events": None, "expires_at": 0, "fetched_at": 0})
    for n in ec.SOURCE_ORDER:
        ec._sources[n].update({"events": None, "fetched_at": 0, "last_error": None})
    out2 = _run(ec.get_events())
    assert ff.await_count == 1 and [e["title"] for e in out2] == ["NFP"] and ec.feed_status()["active_source"] == "forexfactory"
    # older than 1 h → refetch
    db.platform_state.rows[0]["fetched_at"] = _t.time() - 3700
    ec._cache.update({"events": None, "expires_at": 0, "fetched_at": 0})
    _run(ec.get_events())
    assert ff.await_count == 2


# ── 9 · EA fleet / canary endpoint ────────────────────────────────────────────────────────────────────────
def test_ea_fleet_endpoint_counts_terminals_per_accepted_hash_and_reports_the_canary(monkeypatch):
    import routes.ops_routes as orr
    import ea_capabilities as ec
    import types
    db = FakeDb()
    cur, prev = "a" * 64, "b" * 64
    monkeypatch.setattr(ec, "_signed_release_record", lambda: {"version": "1.65", "ex5_sha256": cur, "previous": {"version": "1.64", "ex5_sha256": prev}})
    monkeypatch.setattr(ec, "accepted_ea_sha256s", lambda: [cur, prev])
    monkeypatch.setattr(orr, "get_db", lambda: db)
    monkeypatch.setattr(orr, "_ops_actor", AsyncMock(return_value=(True, "admin")))
    now = datetime.now(timezone.utc)
    canary = {"_id": ObjectId(), "label": "RoboForex Demo", "mode": "demo", "ea_version": "1.65", "ea_binary_sha256": cur, "trading_enabled": True,
              "last_heartbeat": (now - timedelta(seconds=20)).isoformat(), "ea_cmd_stats": {"ok": 12, "failed": 0, "tp_confirmed": 3},
              "environment_attestation": {"environment": "DEMO"}}
    db.accounts.rows += [canary] + [{"_id": ObjectId(), "label": f"T{i}", "mode": "demo", "ea_version": "1.64", "ea_binary_sha256": prev, "trading_enabled": True,
                                     "last_heartbeat": now.isoformat()} for i in range(3)]
    db.accounts.rows.append({"_id": ObjectId(), "label": "rogue", "mode": "demo", "ea_version": "1.63", "ea_binary_sha256": "c" * 64, "trading_enabled": True})
    db.ea_feed_events.rows.append({"account_id": str(canary["_id"]), "at": now - timedelta(hours=1), "timeframe": "M1", "payloads": 3, "bars": 90})
    db.ea_feed_events.rows.append({"account_id": str(canary["_id"]), "at": now, "timeframe": "M1", "payloads": 2, "bars": 60})
    out = _run(orr.ea_fleet(types.SimpleNamespace()))
    by_role = {a["role"]: a for a in out["accepted"]}
    assert by_role["current"]["terminals"] == 1 and by_role["previous"]["terminals"] == 3 and by_role["previous"]["version"] == "1.64"
    assert [t["label"] for t in out["unaccepted_enabled"]] == ["rogue"]
    c = out["canary"]
    assert c["version"] == "1.65" and len(c["terminals"]) == 1 and c["soak_ok"] is True
    t = c["terminals"][0]
    assert t["m1_payloads_24h"] == 5 and t["tp_confirmed"] == 3 and t["acks_failed"] == 0 and t["heartbeat_age_sec"] <= 25
    fe = _read("frontend", "src", "components", "EaFleetCards.jsx")
    assert 'data-testid="stable-ex5-card"' in fe and 'data-testid="canary-soak-card"' in fe and '"/ops/ea-fleet"' in fe
    assert "<EaFleetCards />" in _read("frontend", "src", "pages", "BotHealth.jsx")


def test_bridge_records_m1_feed_events_and_command_stats():
    src = _read("backend", "routes", "bridge_routes.py")
    assert 'db.ea_feed_events.update_one' in src and '"$inc": {"payloads": 1' in src and '"ea_cmd_stats.tp_confirmed"' in src and "ea_cmd_stats.{'ok' if payload.success else 'failed'}" in src
    assert 'ea_feed_events.create_index("at", expireAfterSeconds=48 * 3600)' in _read("backend", "seed.py")
