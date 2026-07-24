"""iter-96 + iter-97 HTTP endpoint tests — autopilot autonomy stack.

Covers regime probabilities, failure taxonomy, learning records/speeds,
safety invariants, governance policy/propose/reject, trend-score, and
operational_mode PUT/GET on /bot/config.

SAFETY: uses admin cookie session. Every mutation that touches the LIVE
bot_configs (risk_pct, operational_mode) is immediately reversed inside
the same test. Governance proposals are created only to be REJECTED —
never approved for aggressive changes.
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import base_url  # noqa: E402

API = f"{base_url()}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie not set after login"
    s.headers.update({"x-csrf-token": csrf})
    return s


# ---------------------------------------------------------- iter-96
def test_regime_probabilities(sess):
    r = sess.get(f"{API}/risk/regime", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    reg = body.get("regime") or {}
    probs = reg.get("probabilities") or {}
    classes = probs.get("classes") or {}
    assert len(classes) == 8, f"expected 8 regime classes, got {list(classes)}"
    total = sum(classes.values())
    assert abs(total - 1.0) < 0.05, f"probs must sum ~1.0, got {total}"
    assert probs.get("top") in classes
    assert 0.0 <= float(probs.get("top_p", 0)) <= 1.0
    assert 0.0 <= float(probs.get("uncertainty", 0)) <= 1.0


def test_failure_summary(sess):
    r = sess.get(f"{API}/learning/failure-summary?days=30", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 30
    assert "classified_losses" in body
    cats = body.get("categories") or []
    if cats:
        row = cats[0]
        for k in ("category", "count", "pnl", "share_pct", "route_fix_to"):
            assert k in row, f"missing {k} in category row"
    assert isinstance(body.get("principle"), str) and body["principle"]


def test_learning_records(sess):
    r = sess.get(f"{API}/learning/records?days=30", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "records" in body and "count" in body
    assert isinstance(body["records"], list)
    # If any losing record exists, it must carry a failure block
    for rec in body["records"]:
        assert "trade_id" in rec
        if rec.get("realized_r") is not None and rec["realized_r"] < 0:
            assert rec.get("failure"), f"loss missing failure block: {rec.get('trade_id')}"


def test_governance_policy(sess):
    r = sess.get(f"{API}/governance/policy", timeout=30)
    assert r.status_code == 200, r.text
    b = r.json()
    for k in ("principle", "safer_direction", "safer_bool", "forbidden_fields"):
        assert k in b, f"missing {k}"
    assert isinstance(b["forbidden_fields"], list) and b["forbidden_fields"]


def test_governance_propose_aggressive_then_reject(sess):
    # Read current risk_pct so we know what to compare against
    cfg_r = sess.get(f"{API}/bot/config", timeout=30)
    assert cfg_r.status_code == 200, cfg_r.text
    cfg = cfg_r.json() or {}
    cur_risk = float(cfg.get("risk_pct") or 0.5)
    higher = round(cur_risk + 0.25, 4)

    # Propose HIGHER (aggressive) → must be pending, config untouched
    prop = sess.post(f"{API}/governance/propose",
                     json={"field": "risk_pct", "new_value": higher,
                           "evidence": "iter97 http test — will reject"},
                     timeout=30)
    assert prop.status_code == 200, prop.text
    d = prop.json()
    assert d["status"] == "pending", f"expected pending, got {d}"
    change_id = d["id"]

    # Confirm config unchanged
    cfg_after = sess.get(f"{API}/bot/config", timeout=30).json()
    assert float(cfg_after.get("risk_pct") or 0.5) == cur_risk, \
        f"risk_pct mutated by proposal! was {cur_risk}, now {cfg_after.get('risk_pct')}"

    # Filter list by pending
    lst = sess.get(f"{API}/governance/changes?status=pending", timeout=30)
    assert lst.status_code == 200
    ids = [c["id"] for c in (lst.json().get("changes") or [])]
    assert change_id in ids

    # Reject it
    rej = sess.post(f"{API}/governance/changes/{change_id}/reject", timeout=30)
    assert rej.status_code == 200, rej.text
    j = rej.json()
    assert j.get("ok") and j.get("status") == "rejected"

    # Double-resolve refused
    again = sess.post(f"{API}/governance/changes/{change_id}/reject", timeout=30)
    assert again.status_code == 200
    assert again.json().get("ok") is False

    # Verify config still unchanged (safety)
    cfg_final = sess.get(f"{API}/bot/config", timeout=30).json()
    assert float(cfg_final.get("risk_pct") or 0.5) == cur_risk


# ---------------------------------------------------------- iter-97
def test_learning_speeds(sess):
    r = sess.get(f"{API}/learning/speeds", timeout=30)
    assert r.status_code == 200, r.text
    speeds = r.json().get("speeds") or []
    tiers = {s["tier"] for s in speeds}
    assert tiers == {"fast", "medium", "slow"}
    for s in speeds:
        assert s.get("levers") and isinstance(s["levers"], list)
        assert s.get("bounded_by")
        assert isinstance(s.get("live"), dict)


def test_safety_invariants(sess):
    r = sess.get(f"{API}/learning/safety-invariants", timeout=30)
    assert r.status_code == 200, r.text
    b = r.json()
    inv = b.get("invariants") or []
    assert len(inv) == 9, f"expected 9 invariants, got {len(inv)}"
    for row in inv:
        assert row.get("behavior") and row.get("prevented_by")
        assert row.get("status") == "enforced"
    assert "pending_governance_approvals" in b


def test_trend_score_xauusd(sess):
    r = sess.get(f"{API}/bot/trend-score?symbol=XAUUSD", timeout=30)
    assert r.status_code == 200, r.text
    b = r.json()
    if not b.get("ready"):
        pytest.skip(f"trend-score not ready: {b.get('reason')}")
    q = b.get("quality") or {}
    assert q.get("direction") in ("UP", "DOWN", "FLAT", "NEUTRAL", None) or isinstance(q.get("direction"), str)
    assert 0 <= float(q.get("score", 0)) <= 100
    comps = q.get("components") or {}
    for k in ("structure_alignment", "momentum_persistence", "volatility_support",
              "cross_timeframe", "spread_suitability"):
        assert k in comps, f"missing component {k}"
    ex = b.get("exhaustion") or {}
    assert "score" in ex and "signals" in ex
    assert isinstance(b.get("exhausted"), bool)


def test_bot_config_operational_mode_default(sess):
    r = sess.get(f"{API}/bot/config", timeout=30)
    assert r.status_code == 200
    cfg = r.json()
    assert "operational_mode" in cfg
    assert cfg["operational_mode"] in (
        "observe", "shadow", "demo_autopilot", "supervised_live",
        "autonomous_live", "defensive", "panic")


def test_bot_config_put_operational_mode_roundtrip_and_restore(sess):
    # Read current
    cfg = sess.get(f"{API}/bot/config", timeout=30).json()
    original = cfg.get("operational_mode") or "autonomous_live"
    try:
        # Invalid mode → 422
        bad = sess.put(f"{API}/bot/config",
                       json={"operational_mode": "yolo"}, timeout=30)
        assert bad.status_code == 422, f"expected 422 for invalid mode, got {bad.status_code} {bad.text}"

        # Valid mode set to 'observe'
        ok = sess.put(f"{API}/bot/config",
                      json={"operational_mode": "observe"}, timeout=30)
        assert ok.status_code == 200, ok.text
        after = sess.get(f"{API}/bot/config", timeout=30).json()
        assert after.get("operational_mode") == "observe"
    finally:
        # ALWAYS restore to original (live bot!)
        restore = sess.put(f"{API}/bot/config",
                           json={"operational_mode": original}, timeout=30)
        assert restore.status_code == 200, restore.text
        final = sess.get(f"{API}/bot/config", timeout=30).json()
        assert final.get("operational_mode") == original, \
            f"FAILED TO RESTORE operational_mode! original={original}, current={final.get('operational_mode')}"


# ---------------------------------------------------------- regression
@pytest.mark.parametrize("path", [
    "/performance/verified",
    "/performance/evidence",
    "/broker-intel",
])
def test_regression_endpoints(sess, path):
    r = sess.get(f"{API}{path}", timeout=45)
    assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:300]}"
