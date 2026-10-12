"""main127 review (Oct 11 2026) — H1..H4, M1..M6 + the "still open" items that are code-fixable
(auto-attest re-check, operator approval bound to identity, rules policy stands in for the expectation).
H1/M3 live in test_phase3_rules_policy.py, H3/M4/M5/M6 in test_phase1_onboarding.py."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── H2 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_h2_start_resolves_scope_before_readiness_and_stop_without_id_stops_all():
    src = _read("backend", "routes", "bot_routes.py")
    start = src.split('@router.post("/start")')[1].split("@router.")[0]
    assert start.index('account_id = await _resolve_account_scope(db, user["id"], account_id)') < start.index("_activation_readiness(db, owns)")
    stop = src.split('@router.post("/stop")')[1].split("@router.")[0]
    assert 'update_many({"user_id": user["id"], "active": True}' in stop and '"stopped_account_ids"' in stop
    assert "_resolve_account_scope" not in stop                      # no guessed single scope on stop


def test_h2_stop_all_runtime():
    from routes.bot_routes import stop_bot
    import routes.bot_routes as br
    db = FakeDb()
    br.get_db = lambda: db
    a1, a2 = ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": a1, "user_id": "u1"}))
    _run(db.accounts.insert_one({"_id": a2, "user_id": "u1"}))
    for aid in (a1, a2):
        _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(aid), "active": True}))
    _run(db.bot_configs.insert_one({"user_id": "u2", "account_id": str(ObjectId()), "active": True}))
    out = _run(stop_bot(account_id=None, user={"id": "u1"}))
    assert set(out["stopped_account_ids"]) == {str(a1), str(a2)} and out["stopped"] == 2
    assert all(not c["active"] for c in db.bot_configs.rows if c["user_id"] == "u1")
    assert [c["active"] for c in db.bot_configs.rows if c["user_id"] == "u2"] == [True]   # other users untouched
    out1 = _run(stop_bot(account_id=str(a1), user={"id": "u1"}))
    assert out1["stopped_account_ids"] == [str(a1)]


# ── H4 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_h4_update_sh_never_dies_silently_and_precheck_accepts_ui_releases():
    sh = _read("deploy", "update.sh")
    blk = sh.split("release_precheck.py")[0]
    assert "grep -E '^GITHUB_REPO=' .env 2>/dev/null | cut -d= -f2- || true" in blk
    assert "grep -E '^GITHUB_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true" in blk
    assert "cannot determine the GitHub repo" in sh
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import release_precheck as rpc
    assert "event=push" not in _read("scripts", "release_precheck.py")
    runs = {"workflow_runs": [
        {"head_branch": "v1.60.18", "event": "release", "status": "completed", "conclusion": "success", "html_url": "u1", "head_sha": "s1"},
        {"head_branch": "v1.60.18", "event": "push", "status": "completed", "conclusion": "failure", "html_url": "u0", "head_sha": "s1"},
        {"head_branch": "main", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "html_url": "x", "head_sha": "s2"}]}

    class _R:
        def __init__(self, data): self._d = data
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps(self._d).encode()
    rpc.urllib.request.urlopen = lambda req, timeout=30: _R(runs)
    r = rpc.release_run("o/r", "v1.60.18", None)
    assert r["found"] and r["conclusion"] == "success" and r["event"] == "release"       # a UI release counts
    assert rpc.release_run("o/r", "v9.9.9", None) == {"found": False}
    def _boom(req, timeout=30): raise OSError("dns")
    rpc.urllib.request.urlopen = _boom
    rpc.tag_on_origin = lambda tag: True
    assert rpc.main(["--repo", "o/r", "--tag", "v1.60.18"]) == 4                       # network error → 4, no traceback


# ── M1 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_m1_production_without_installation_id_fails_closed(monkeypatch):
    import rules_policy as rp
    import inventory_projection as ip
    import app_env
    monkeypatch.setattr(ip, "installation_id", lambda: "")
    monkeypatch.setattr(app_env, "is_production", lambda: True)
    errs = rp.problems({"schema": rp.SCHEMA, "policy_version": "demo-rules-v1"})
    assert any("STOIC_INSTALLATION_ID is not set" in e for e in errs)
    monkeypatch.setattr(app_env, "is_production", lambda: False)
    errs = rp.problems({"schema": rp.SCHEMA, "policy_version": "demo-rules-v1"})
    assert not any("STOIC_INSTALLATION_ID" in e for e in errs)


# ── M2 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_m2_risk_caps_compare_against_the_risk_the_bot_uses_and_are_enforced_at_runtime():
    import rules_policy as rp
    # saved configs carry risk_level / adaptive cap — never a literal risk_per_trade_pct
    assert rp.effective_max_risk_pct({"risk_level": "low", "adaptive_sizing_enabled": False}) == 0.5
    assert rp.effective_max_risk_pct({"risk_level": "high", "adaptive_sizing_enabled": False}) == 2.5
    assert rp.effective_max_risk_pct({"risk_level": "low"}) == 2.0                  # adaptive may scale up to the cap
    assert rp.effective_max_risk_pct({"risk_level": "low", "adaptive_risk_cap_pct": 0.8}) == 0.8
    assert rp.effective_max_risk_pct({}) == 2.0
    pol = {"max_accounts_per_user": 5, "allowed_brokers": ["*"], "risk_caps": {"max_risk_pct": 1.0}}
    acc = {"_id": ObjectId(), "mode": "live", "broker": "OnEquity", "broker_environment": "DEMO", "account_trade_mode": "demo",
           "ea_identity": {"authoritative": True, "trade_mode": "demo", "installation_id": "i", "ea_binary_accepted": True}}
    import broker_env
    orig = broker_env.attested_environment
    broker_env.attested_environment = lambda a: "DEMO"
    try:
        d = rp.evaluate_admission(acc, pol, user_account_count=1, bot_cfg={"risk_level": "high", "adaptive_sizing_enabled": False})
        assert d["state"] == "blocked" and "above the policy cap" in d["reason"] and d["checks"]["effective_max_risk_pct"] == 2.5
        d = rp.evaluate_admission(acc, pol, user_account_count=1, bot_cfg={"risk_level": "low", "adaptive_risk_cap_pct": 1.0})
        assert d["state"] == "admitted" and d["checks"]["risk_within_cap"] is True
    finally:
        broker_env.attested_environment = orig
    # runtime: sizing clamp in bot_runner + daily-loss cap in circuit_breakers
    br = _read("backend", "bot_runner.py")
    assert 'from rules_policy import risk_caps as _policy_caps' in br and 'signal["policy_risk_clamp"]' in br
    cb = _read("backend", "circuit_breakers.py")
    assert "daily_limit_pct = min(daily_limit_pct, float(policy_daily_cap))" in cb and "daily_enabled = True" in cb
    assert '"policy_daily_cap_pct": policy_daily_cap' in cb


def test_m2_daily_loss_cap_runtime(monkeypatch):
    import circuit_breakers as cb
    import rules_policy as rp
    db = FakeDb()

    async def _caps(db_): return {"max_daily_loss_pct": 1.0}
    monkeypatch.setattr(rp, "risk_caps", _caps)
    today = datetime.now(timezone.utc).isoformat()
    _run(db.trades.insert_one({"user_id": "u1", "account_id": "a1", "status": "closed", "pnl": -150.0, "closed_at": today, "origin": "auto"}))
    cfg = {"user_id": "u1", "account_id": "a1", "daily_drawdown_pct": 5.0, "daily_drawdown_enabled": False, "risk_level": "high"}
    res = _run(cb.check_and_trip(db, "u1", cfg, [{"equity": 10000.0}]))
    assert res["limit_pct"] == 1.0 and res["policy_daily_cap_pct"] == 1.0 and res["daily_enabled"] is True
    assert res["tripped"] is True and res["kind"] == "daily"


# ── still open: auto-attest re-check + operator approval bound to identity ────────────────────────────
def test_auto_attest_grant_is_rechecked_every_heartbeat():
    import auto_attest as aa
    import broker_env
    att = {"approved_by": aa.AUTO_APPROVER, "identity_hash": "old"}
    acc = {"_id": ObjectId(), "mode": "live", "environment_attestation": att, "ea_identity": {"authoritative": True, "installation_id": "i", "trade_mode": "demo"},
           "ea_binary_sha256": "ab", "ea_binary_sha256_method": "installer_attested", "broker_environment": "DEMO", "account_trade_mode": "demo"}
    orig = (broker_env.demo_proof, aa.demo_proof, aa.attestation_identity, aa.broker_reports_demo, aa.broker_environment)
    aa.demo_proof = lambda a, now=None: {"ok": True, "proof_id": "p", "checks": {}, "heartbeat_age_s": 1, "reported_server": "s"}
    aa.broker_reports_demo = lambda a: True
    aa.broker_environment = lambda a: "DEMO"
    try:
        aa.attestation_identity = lambda a: "old"
        d = aa.evaluate(acc, accepted_hashes=["ab"])
        assert d["grant"] is False and d["void"] is False                      # all good, already granted → nothing
        aa.attestation_identity = lambda a: "moved"
        d = aa.evaluate(acc, accepted_hashes=["ab"])
        assert d["void"] is True and d["checks"]["identity_moved"] is True     # identity moved → voided
        aa.attestation_identity = lambda a: "old"
        d = aa.evaluate(acc, accepted_hashes=["other"])
        assert d["void"] is True and d["checks"]["ea_hash_signed"] is False    # binary no longer a signed release → voided
    finally:
        broker_env.demo_proof, aa.demo_proof, aa.attestation_identity, aa.broker_reports_demo, aa.broker_environment = orig


def test_operator_approval_is_bound_to_identity():
    src = _read("backend", "rules_policy.py")
    op = src.split("async def operator_approve")[1]
    assert '"identity_hash": attestation_identity(acc)' in op
    sync = src.split("async def sync_admission")[1].split("async def operator_approve")[0]
    assert 'cur.get("identity_hash") == attestation_identity(acc)' in sync and "re-approval needed" in sync
    ta = _read("backend", "trading_authority.py")
    assert "a signed RULES policy stands in for the fixed expectation" in ta
