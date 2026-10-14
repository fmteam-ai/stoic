"""v1.60.35 — (1) Release-gate card renders evaluate() verbatim · (2) single regen command, CI still fails on mismatch ·
(3) stop audit trail, deduped · (4) EA channel promotion: readiness gate, step-up, audit, one-click rollback."""
import asyncio
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


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── (1) ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_release_gate_card_renders_evaluate_verbatim_no_client_side_verdict():
    card = _read("frontend", "src", "components", "ReleaseGateCard.jsx")
    assert 'api.get("/admin/release-gate")' in card and "d.failures" in card and "d?.ok === true" in card
    # no re-derivation: the card never compares digests / commits itself
    assert "split(\"@\")" not in card.replace("String(h).split(\"@\").pop()", "") and "locked_backend_digest ===" not in card
    route = _read("backend", "routes", "admin_routes.py")
    i = route.index('@router.get("/admin/release-gate")')
    assert "r = evaluate()" in route[i:i + 1200] and 'return {"enforced": enforced, **r, "accounts": accounts}' in route[i:i + 3000]
    page = _read("frontend", "src", "pages", "BotHealth.jsx")
    assert 'user?.role === "admin" && <ReleaseGateCard />' in page and 'user?.role === "admin" && <EaChannelCard />' in page


# ── (2) ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_regen_command_exists_and_ci_still_verifies_without_refreezing():
    regen = _read("scripts", "regen_release_artifacts.py")
    assert "generate_test_manifest.py" in regen and "freeze_rc_lock.py" in regen and "generate_release_summary.py" in regen
    assert 'os.environ.get("CI")' in regen                                   # refuses to run in CI
    ci = _read(".github", "workflows", "ci.yml")
    assert "regen_release_artifacts" not in ci and "freeze_rc_lock.py" not in ci   # CI only verifies
    assert "generate_test_manifest.py --check" in ci
    assert os.path.exists(os.path.join(ROOT, "backend", "tests", "unit", "test_rc_lock_manifest_bound.py"))
    for gen in ("generate_test_manifest.py", "generate_release_summary.py"):
        assert "freeze_rc_lock" not in _read("scripts", gen)              # generators never silently re-freeze


# ── (3) ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _trade(**kw):
    t = {"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "SELL", "engine": None, "scope": "hf_scalp_fast",
         "entry_price": 4000.0, "stop_loss": 3999.75, "sl_pips": 400, "tp_pips": [100, 200, 200], "lot_size": 0.05,
         "original_lot_size": 0.05, "mt5_ticket": 1, "status": "open", "mode": "demo", "spread_pips": 2.5,
         "be_target_sl": 3999.75, "scalp_exit_rule": "breakeven_0_5r",
         "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    t.update(kw)
    return t


def test_refused_stop_loosen_is_recorded_once_per_trade_and_requested_sl():
    import trade_manager as tm
    db = FakeDb()
    t = _trade()
    db.trades.rows.append(t)
    quote = AsyncMock(return_value={"price": 4000.0 - 0.1 * 120})
    with patch.object(tm, "get_db", lambda: db), patch.object(tm, "get_quote", quote), patch.object(tm, "ws_manager") as ws, \
            patch("adaptive_exits.manage_exits", AsyncMock()):
        ws.broadcast = AsyncMock()
        for _ in range(3):                                   # three manager cycles → ONE audit entry
            _run(tm._manage_one_trade(db.trades.rows[0], {"let_winners_run": True}))
    refusals = db.trades.rows[0]["sl_loosen_refusals"]
    assert len(refusals) == 1
    r = refusals[0]
    assert r["source"] == "tp1_let_winners_run" and r["requested_sl"] == 4000.0 and r["current_sl"] == 3999.75 and r["action"] == "SELL"
    # a DIFFERENT requested SL from another source is a new entry
    with patch.object(tm, "get_db", lambda: db):
        assert _run(tm.record_sl_loosen_refusal(db.trades.rows[0], 4000.3, "tier1_combo_followup")) is True
        assert _run(tm.record_sl_loosen_refusal(db.trades.rows[0], 4000.3, "tier1_combo_followup")) is False
    assert len(db.trades.rows[0]["sl_loosen_refusals"]) == 2


def test_stop_guard_trail_reaches_the_loss_lab():
    assert '"sl_loosen_refusals": trade.get("sl_loosen_refusals") or []' in _read("backend", "loss_postmortem.py")
    lab = _read("frontend", "src", "pages", "LossLab.jsx")
    assert "pm.sl_loosen_refusals" in lab and "STOP GUARD" in lab
    br = _read("backend", "routes", "bridge_routes.py")
    assert 'record_sl_loosen_refusal(trade, requested, "tier1_combo_followup")' in br


# ── (4) ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _channel_db(hours_ago=48, pinned=False):
    db = FakeDb()
    db.platform_state.rows.append({"_id": "release_state", "stable": {"stoic-ea": "a" * 64},
                                   "candidate": {"stoic-ea": "b" * 64},
                                   "candidate_since": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
                                   "pinned": pinned})
    return db


SIGNED = {"version": "1.64", "ex5_sha256": "c" * 64}


def test_promotion_readiness_requires_soak_unpinned_and_signed_ex5():
    import release_channels as rc
    with patch("ea_capabilities._signed_release_record", return_value=SIGNED), \
            patch("deployment_health._trusted_user_ids", AsyncMock(return_value=set())):
        ok = _run(rc.promotion_readiness(_channel_db()))
        assert ok["eligible"] and ok["can_rollback"] is False and {c["id"] for c in ok["checks"]} == {"candidate", "unpinned", "soak", "no_failures", "ea_signed"}
        young = _run(rc.promotion_readiness(_channel_db(hours_ago=2)))
        assert not young["eligible"] and young["blocking"] == ["soak window 24 h elapsed"]
        pinned = _run(rc.promotion_readiness(_channel_db(pinned=True)))
        assert not pinned["eligible"] and "channel not pinned (a rollback pins it)" in pinned["blocking"]
    with patch("ea_capabilities._signed_release_record", return_value=None), \
            patch("deployment_health._trusted_user_ids", AsyncMock(return_value=set())):
        unsigned = _run(rc.promotion_readiness(_channel_db()))
        assert not unsigned["eligible"] and unsigned["blocking"] == ["signed EX5 release record for the shipped EA"]


def test_promote_is_refused_server_side_until_eligible_then_audited_and_rollbackable():
    import release_channels as rc
    db = _channel_db(hours_ago=2)
    with patch("ea_capabilities._signed_release_record", return_value=SIGNED), \
            patch("deployment_health._trusted_user_ids", AsyncMock(return_value=set())), \
            patch("deployment_health.start_watch", AsyncMock()):
        with pytest.raises(ValueError, match="not eligible.*soak window"):
            _run(rc.promote(db, actor="admin@x"))
        db.platform_state.rows[0]["candidate_since"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        st = _run(rc.promote(db, actor="admin@x"))
        assert st["stable"] == {"stoic-ea": "b" * 64} and st["previous_stable"] == {"stoic-ea": "a" * 64} and st["candidate"] is None
        ev = [h for h in db.release_history.rows if h["event"] == "promoted"][0]["detail"]
        assert ev["by"] == "admin@x" and ev["from"] == {"stoic-ea": "a" * 64} and ev["to"] == {"stoic-ea": "b" * 64}
        # one-click rollback to the previous stable, which pins the channel
        with patch.object(rc, "_snapshot_to_store", lambda s: True):
            back = _run(rc.rollback(db, actor="admin@x"))
        assert back["stable"] == {"stoic-ea": "a" * 64} and back["pinned"] is True
        assert _run(rc.promotion_readiness(db))["can_rollback"] is True


def test_promote_and_rollback_routes_are_admin_step_up_and_write_who_when_from_to():
    from step_up import STEP_UP_ACTIONS
    assert {"release_promote", "release_rollback"} <= set(STEP_UP_ACTIONS)
    src = _read("backend", "routes", "ops_routes.py")
    for action, fn in (("release_promote", "promote"), ("release_rollback", "rollback")):
        i = src.index(f'_ops_admin_step_up(request, "{action}")')
        block = src[i:i + 900]
        assert f"await _channel_audit(request, actor, \"{action}_done\", before, st)" in block
    assert '"from": before.get("stable"), "to": after.get("stable")' in src and "request.state.ops_actor_id = u[\"id\"]" in src
    assert '"readiness": await promotion_readiness(db)' in src
    card = _read("frontend", "src", "components", "EaChannelCard.jsx")
    assert "disabled={!rd.eligible || busy !== null}" in card and 'api.post(`/ops/releases/${kind}`)' in card
    assert "disabled={!rd.can_rollback || busy !== null}" in card and 'data-testid="ea-channel-rollback"' in card
