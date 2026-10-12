"""main130 (M1–M3 + load_latest wrap) and main131 (P1-01, P1-02, P2-01, P2-02, P2-04, P1-06, P0-02) fixes."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NOW = datetime.now(timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _acc(**kw):
    base = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "A", "broker": "OnEquity", "account_number": "1001",
            "trading_enabled": True, "account_type": "demo"}
    base.update(kw)
    return base


# ── M1 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_m1_reconcile_counts_only_enabled_non_paper_accounts_toward_the_cap():
    sys.path.insert(0, os.path.join(ROOT, "backend", "ops"))
    import production_reconcile as pr
    accs = [_acc(trading_enabled=True), _acc(trading_enabled=False), _acc(trading_enabled=True, mode="paper"),
            _acc(user_id="u2", trading_enabled=True), _acc(user_id="u2", trading_enabled=True)]
    per_user = pr.per_user_cap_counts(accs)
    assert per_user == {"u1": 1, "u2": 2}
    cap = 1
    assert per_user["u1"] <= cap                      # 1 enabled + 1 disabled under a cap of 1 PASSES
    assert per_user["u2"] > cap
    src = _read("backend", "ops", "production_reconcile.py")
    assert "per_user = per_user_cap_counts(accs)" in src and 'x.get("trading_enabled") is True and x.get("mode") != "paper"' in src


# ── M2 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_m2_cap_is_ranked_by_age_so_a_second_account_never_closes_the_first(monkeypatch):
    import rules_policy as rp
    db = FakeDb()
    a = _acc(label="A")
    b = _acc(label="B", _id=ObjectId())                 # created after A → larger ObjectId
    assert b["_id"] > a["_id"]
    _run(db.accounts.insert_one(dict(a)))
    _run(db.accounts.insert_one(dict(b)))
    assert _run(db.accounts.count_documents(rp.cap_count_query(a))) == 1      # A sees only itself
    assert _run(db.accounts.count_documents(rp.cap_count_query(b))) == 2      # B sees the older enabled A + itself
    pol = {"policy_version": "rules-v1", "demo_only": True, "max_accounts_per_user": 1, "allowed_brokers": ["*"], "expired": False}
    monkeypatch.setattr("broker_env.attested_environment", lambda acc: "DEMO")
    monkeypatch.setattr("broker_env.broker_reports_real", lambda acc: False)
    audits = []

    async def _audit(db_, entry, **k):
        audits.append(entry)
    monkeypatch.setattr("audit_chain.append_chained", _audit)

    async def _active(db_):
        return pol
    monkeypatch.setattr(rp, "active", _active)
    da = _run(rp.sync_admission(db, a))
    db_b = _run(rp.sync_admission(db, b))
    assert da["state"] == "admitted"                 # A admitted
    assert db_b["state"] == "blocked" and "more than 1" in db_b["reason"]      # B blocked
    assert _run(rp.sync_admission(db, _run(db.accounts.find_one({"_id": a["_id"]}))))["state"] == "admitted"   # A STAYS admitted
    # disabling B stops it counting for anyone younger; disabling A would free B
    _run(db.accounts.update_one({"_id": a["_id"]}, {"$set": {"trading_enabled": False}}))
    assert _run(db.accounts.count_documents(rp.cap_count_query(b))) == 1


# ── M3 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_m3_use_reported_login_global_uniqueness_and_trust_reset():
    src = _read("backend", "routes", "wizard_routes.py").split("async def use_reported_login")[1]
    assert '"user_id": {"$ne": user["id"]}' in src and '"last_heartbeat": {"$gte": active_cutoff}' in src and '"code": "account_in_use"' in src
    assert '"$unset": {"ea_identity": "", "verified_identity": "", "environment_attestation": "", "admission": ""}' in src
    assert '"cleared": ["environment_attestation", "admission"]' in src and "account_login_adopted_from_ea" in src
    assert src.index('"code": "account_in_use"') < src.index("update_one")        # 409 BEFORE any write


def test_m3_runtime_cross_user_409_and_trust_cleared(monkeypatch):
    from fastapi import HTTPException
    import routes.wizard_routes as wz
    db = FakeDb()
    monkeypatch.setattr(wz, "get_db", lambda: db)
    audits = []

    async def _audit(db_, entry, **k):
        audits.append(entry)
    monkeypatch.setattr("audit_chain.append_chained", _audit)
    user = {"id": "u1", "email": "u1@x"}
    mine = _acc(account_number="1001", ea_identity={"reason": "MT5 login mismatch: EA reports 2002"},
                environment_attestation={"environment": "DEMO"}, admission={"state": "admitted"})
    other = _acc(user_id="u2", account_number="2002", last_heartbeat=NOW.isoformat())
    _run(db.accounts.insert_one(dict(mine)))
    _run(db.accounts.insert_one(dict(other)))
    with pytest.raises(HTTPException) as ei:
        _run(wz.use_reported_login(str(mine["_id"]), None, user=user))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "account_in_use"
    # the other user's EA went silent for > 24 h → the login may be adopted; trust is reset and audited
    _run(db.accounts.update_one({"_id": other["_id"]}, {"$set": {"last_heartbeat": (NOW - timedelta(days=2)).isoformat()}}))
    out = _run(wz.use_reported_login(str(mine["_id"]), None, user=user))
    assert out["ok"] and out["login"] == "2002"
    doc = _run(db.accounts.find_one({"_id": mine["_id"]}))
    assert doc["account_number"] == "2002" and "environment_attestation" not in doc and "admission" not in doc and "verified_identity" not in doc
    assert audits and audits[-1]["action"] == "account_login_adopted_from_ea" and audits[-1]["meta"]["cleared"] == ["environment_attestation", "admission"]


# ── load_latest C1 wrap ───────────────────────────────────────────────────────────────────────────────
def test_load_latest_skips_a_malformed_candidate_instead_of_500(monkeypatch):
    import rules_policy as rp

    async def _cands():
        return [{"name": "bad.json", "policy": ["not", "an", "object"]}, {"name": "worse.json", "policy": {"issued_at": object()}},
                {"name": "x.json", "policy": {}}]
    monkeypatch.setattr(rp, "fetch_candidates_from_github", _cands)
    out = _run(rp.load_latest(FakeDb(), actor="t"))
    assert out["activated"] is False and out["reason"] == "no valid signed policy found"
    names = {r["name"] for r in out["rejected"]}
    assert {"bad.json", "worse.json", "x.json"} <= names


# ── P1-01 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p1_01_normal_delete_refusals_and_no_force_parameter():
    from account_deletion import delete_refusal, STALE_S
    fresh = {"last_heartbeat": NOW.isoformat()}
    stale = {"last_heartbeat": (NOW - timedelta(seconds=STALE_S + 60)).isoformat()}
    assert delete_refusal(fresh, non_terminal=1, unknown=0, ever_traded=True)["code"] == "trades_attached"
    assert delete_refusal(fresh, non_terminal=0, unknown=1, ever_traded=True)["code"] == "unknown_execution_state"
    assert delete_refusal(stale, non_terminal=0, unknown=0, ever_traded=True)["code"] == "stale_broker_truth"
    assert delete_refusal({}, non_terminal=0, unknown=0, ever_traded=True)["code"] == "stale_broker_truth"     # traded, never heartbeated
    assert delete_refusal({}, non_terminal=0, unknown=0, ever_traded=False) is None                            # never connected, never traded
    assert delete_refusal(fresh, non_terminal=0, unknown=0, ever_traded=True) is None
    src = _read("backend", "routes", "account_routes.py").split('@router.delete("/{account_id}")')[1].split("@router.")[0]
    assert "force: bool" not in src and "?force" not in src and "status_code=409" in src and '"status": "unknown"' in src


def test_p1_01_admin_force_delete_is_gated_and_audited(monkeypatch):
    from fastapi import HTTPException
    import routes.admin_routes as ar
    db = FakeDb()
    monkeypatch.setattr(ar, "get_db", lambda: db)
    monkeypatch.setattr(ar, "_admin_only", lambda u: None)
    stepped, audits, purged, recon_calls = [], [], [], []

    async def _step(db_, user, request, action):
        stepped.append(action)
    monkeypatch.setattr("step_up.require_step_up", _step)

    async def _audit(db_, entry, **k):
        audits.append(entry)
    monkeypatch.setattr("audit_chain.append_chained", _audit)

    async def _recon(uid, *, force=False):
        recon_calls.append((uid, force))
        return {"closed": 0}
    monkeypatch.setattr("trade_reconciler.reconcile_user", _recon)

    async def _purge(db_, acc, *, deleted_by):
        purged.append(deleted_by)
        await db_.accounts.delete_one({"_id": acc["_id"]})
        return {"pairing_tokens_removed": 0}
    monkeypatch.setattr("account_deletion.purge_account", _purge)
    acc = _acc(last_heartbeat=(NOW - timedelta(hours=3)).isoformat())
    _run(db.accounts.insert_one(dict(acc)))
    _run(db.trades.insert_one({"account_id": str(acc["_id"]), "user_id": "u1", "status": "open"}))
    admin = {"id": "adm", "email": "admin@x", "role": "admin"}
    with pytest.raises(HTTPException) as ei:
        _run(ar.admin_force_delete_account(str(acc["_id"]), {"disposition": "nope", "reason": "long enough reason"}, None, user=admin))
    assert ei.value.status_code == 422 and ei.value.detail["code"] == "disposition_required"
    with pytest.raises(HTTPException) as ei:
        _run(ar.admin_force_delete_account(str(acc["_id"]), {"disposition": "no_positions", "reason": "short"}, None, user=admin))
    assert ei.value.status_code == 422 and ei.value.detail["code"] == "reason_required"
    with pytest.raises(HTTPException) as ei:                                   # open trade remains → "no_positions" is a lie
        _run(ar.admin_force_delete_account(str(acc["_id"]), {"disposition": "no_positions", "reason": "EA offline for days, verified"}, None, user=admin))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "disposition_mismatch"
    assert recon_calls and stepped == ["authority_relax"] * 3 and not purged
    out = _run(ar.admin_force_delete_account(str(acc["_id"]), {"disposition": "positions_closed_on_broker", "reason": "EA offline for days, closed on MT5 by hand"}, None, user=admin))
    assert out["ok"] and purged == ["admin:admin@x"] and _run(db.accounts.find_one({"_id": acc["_id"]})) is None
    t = _run(db.trades.find_one({"account_id": str(acc["_id"])}))
    assert t["status"] == "closed" and t["close_reason"] == "admin_force_delete:positions_closed_on_broker" and t["pnl_unknown"] is True
    assert audits[-1]["action"] == "account_force_deleted" and audits[-1]["meta"]["disposition"] == "positions_closed_on_broker" and audits[-1]["meta"]["reconciliation"] == {"closed": 0}


# ── P1-02 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p1_02_release_signed_row_never_uses_ok():
    from release_truth import release_signed_verdict
    preview = {"ok": True, "verified": False, "enforced": False}          # preview-style: ok because enforcement is off
    att_none = {"enforced": False, "attestation_present": False, "ok": True}
    r = release_signed_verdict(preview, att_none)
    assert r["ok"] is False and "EA record verified" in r["detail"] and "EA proof enforced" in r["detail"]
    good_att = {"attestation_present": True, "promotion_decision": "APPROVED", "sha_matches_running": True, "tag": "v1.60.19", "commit": "a" * 40}
    assert release_signed_verdict({"ok": True, "verified": True, "enforced": False}, good_att)["ok"] is False
    assert release_signed_verdict({"ok": True, "verified": True, "enforced": True}, {**good_att, "sha_matches_running": False})["ok"] is False
    assert release_signed_verdict({"ok": True, "verified": True, "enforced": True}, {**good_att, "promotion_decision": "REJECTED"})["ok"] is False
    ok = release_signed_verdict({"ok": False, "verified": True, "enforced": True}, good_att)
    assert ok["ok"] is True and "v1.60.19" in ok["detail"]
    src = _read("backend", "routes", "ops_routes.py").split("@router.get(\"/ops/go-live-checklist\")")[1]
    assert 'ea.get("ok"' not in src and "release_signed_verdict(ea, checks.get(\"release_attestation\"))" in src


# ── P2-01 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p2_01_pattern_gate_fail_closed_when_enforced_fail_open_when_observing():
    import chart_patterns as cp
    not_ready = {"ready": False, "reason": "insufficient intraday bars"}
    obs = cp.gate_decision("BUY", not_ready, False)
    assert obs["skip"] is False and obs["data_unavailable"] is True and obs["alert"] is False and "unavailable" in obs["detail"]
    enf = cp.gate_decision("BUY", not_ready, True)
    assert enf["skip"] is True and enf["alert"] is True and "fail closed" in enf["reason"]
    err_obs = cp.gate_decision("BUY", {}, False, error="ValueError")
    assert err_obs["skip"] is False and err_obs["data_unavailable"] is True
    err_enf = cp.gate_decision("BUY", {}, True, error="ValueError")
    assert err_enf["skip"] is True and err_enf["alert"] is True and "ValueError" in err_enf["reason"]
    ready_clear = {"ready": True, "atr": 1.0, "close": 100.0, "zones": [], "patterns": []}
    assert cp.gate_decision("BUY", ready_clear, True) == {"skip": False, "reason": None, "would_veto": None, "data_unavailable": False, "alert": False, "detail": None}
    blk = _read("backend", "bot_runner.py").split("Spec B — chart-pattern layer")[1].split("# iter-129")[0]
    assert 'raise_alert(db, "pattern_gate_fail_closed", "high"' in blk and 'dedup_key=f"pattern_gate_fail_closed:{cfg.get(\'account_id\')}:{sym}"' in blk
    assert '"data_unavailable": gd["data_unavailable"]' in blk


# ── P2-02 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p2_02_track_record_provenance_fields_and_labels():
    src = _read("backend", "routes", "trade_routes.py").split('@router.get("/predictions")')[1].split("@router.")[0]
    for k in ('"label": "Prediction calibration — not broker performance"', '"source": "M15 candles (EA stream)"', '"as_of"', '"horizon_hours"',
              '"same_bar_rule": "SL counted first', '"decided"', '"excluded_no_data"', '"pending"'):
        assert k in src, k
    fe = _read("frontend", "src", "components", "TrackRecord.jsx")
    for tid in ("track-provenance-label", "track-source", "track-as-of", "track-horizon", "track-same-bar-rule", "track-counts"):
        assert f'data-testid="{tid}"' in fe, tid
    assert "Prediction calibration — not broker performance" in fe
    import prediction_track as pt
    assert pt.HORIZON_BARS * pt.BAR_SEC // 3600 == 24


# ── P2-04 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p2_04_release_id_in_health_and_footer():
    from release_truth import release_id
    r = release_id(policy_version="demo-rules-v1")
    for k in ("tag", "commit", "short_commit", "attested", "ea_version", "policy_version", "model_manifest", "id"):
        assert k in r, k
    assert r["policy_version"] == "demo-rules-v1" and "policy demo-rules-v1" in r["id"]
    assert r["model_manifest"]["version"] == 2 and r["model_manifest"]["sha256"]
    assert r["ea_version"] and f"EA {r['ea_version']}" in r["id"]
    assert '"release": release_id(' in _read("backend", "server.py").split('@api_router.get("/health")')[1].split("@api_router.")[0]
    fe = _read("frontend", "src", "components", "SiteFooter.jsx")
    assert 'data-testid="footer-release-id"' in fe and 'api.get("/health")' in fe


# ── P1-06 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p1_06_one_current_finding_per_risk_and_closed_register_has_evidence():
    open_ = json.load(open(os.path.join(ROOT, "docs", "open_findings.json")))
    closed = json.load(open(os.path.join(ROOT, "docs", "closed_findings.json")))["closed"]
    risks = [f["risk"] for f in open_ if f.get("risk")]
    assert len(risks) == len(set(risks)) and len(closed) >= 19
    open_ids = {f["id"] for f in open_}
    for c in closed:
        assert c["status"] == "superseded" and c["superseded_by"] in open_ids and c["closure_evidence"] and c["closed_at"]
        assert c["id"] not in open_ids
    for legacy in ("P1-07-r9", "P1-09-r10", "P0-03-r10", "P1-04-r14", "P0-02-r18-handshake"):
        assert legacy not in open_ids
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_open_findings as cof
    assert cof.main() == 0


# ── P0-02 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_p0_02_model_manifest_resign_workflow_uses_the_external_signer_and_refuses_v1():
    wf = _read(".github", "workflows", "model-manifest-resign.yml")
    assert "workflow_dispatch" in wf and "RELEASE_SIGNER: external" in wf and "python -m model_manifest resign --commit" in wf
    assert "python -m model_manifest verify --build" in wf and 'stoic-release-ed25519-v1' in wf and "git push origin HEAD:" in wf and "--force" not in wf
    assert "signer_probe.py" in wf and "contents: write" in wf


# ── Security audit #22 P3 fixes ───────────────────────────────────────────────────────────────────────
def test_sec022_pattern_gate_toggle_uses_mfa_admin_gate_and_predictions_rate_limited():
    bot = _read("backend", "routes", "bot_routes.py").split('"pattern_gate_enabled" in update')[1][:700]
    assert "require_admin(user)" in bot and "pattern_gate_enabled_changed" in bot
    tr = _read("backend", "routes", "trade_routes.py").split('@router.get("/predictions")')[1].split("@router.")[0]
    assert 'rate_limit(db, "trade_predictions", f"user:{user[\'id\']}", 60, 60' in tr
    assert os.path.exists(os.path.join(ROOT, "docs", "SECURITY_AUDIT_22.md"))
