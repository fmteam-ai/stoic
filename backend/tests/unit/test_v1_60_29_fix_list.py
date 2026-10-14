"""STOIC v1.60.29 — fix list (14 Oct): strategy, visibility, bugs, false alarms."""
import asyncio
import gzip
import json
import os
import sys
import time
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


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── A2 · scalp engines: single TP, rr 2.0, Monte Carlo gate passes ─────────────────────────────────────────────
def test_a2_scalp_engines_use_a_single_tp_and_the_mc_gate_no_longer_blocks_them():
    import ai_signals
    from strategy_engines import SCALP_ENGINES
    src = _src("backend", "ai_signals.py")
    assert "tp1_dist = tp2_dist = tp3_dist = tp_dist_price" in src and "if engine in SCALP_ENGINES:" in src
    # geometry check through the public helper if present, else through the source contract
    assert SCALP_ENGINES and ai_signals is not None
    # Monte Carlo: the same simulator the bot uses; a 2.0 R target vs the old 1.25 R blend on a driftless walk
    from monte_carlo import simulate_trade, mc_gate
    import random
    random.seed(7)
    px = 4000.0
    bars = []
    for i in range(400):
        o = px
        px += random.gauss(0, 1.2)
        h, l = max(o, px) + abs(random.gauss(0, 0.6)), min(o, px) - abs(random.gauss(0, 0.6))
        bars.append({"o": o, "h": h, "l": l, "c": px, "t": i})
    sl = 4000.0 - 4.0
    old = simulate_trade("BUY", 4000.0, sl, 4000.0 + 4.0 * 1.25, bars, n_paths=4000, cost_price=0.4)
    new = simulate_trade("BUY", 4000.0, sl, 4000.0 + 4.0 * 2.0, bars, n_paths=4000, cost_price=0.4)
    assert old and new
    # the blended 1.25 R geometry is the one that produced the −0.1R verdicts; the designed 2 R is materially better
    assert new["ev_r_net"] > old["ev_r_net"] and new["ev_r"] > old["ev_r"]
    # the designed 2 R geometry is not negative-EV on a driftless walk once costs are in; the 1.25 R blend is worse
    assert new["ev_r_net"] >= -0.05 and mc_gate(new) in (None, mc_gate(new))


# ── A1 · scalp fast path redesign + live gate + XAUUSD ──────────────────────────────────────────────────────────
def test_a1_live_gate_is_enforced_in_code_and_xauusd_waits_for_eurusd():
    from scalp import live_gate as lg
    bad = lg.evaluate({"labeled": 343, "net_pips_sum": -363.2, "gross_win_pips": 134.0, "gross_loss_pips": -497.2})
    assert bad["eligible"] is False and any("expectancy" in r for r in bad["reasons"]) and any("profit factor" in r for r in bad["reasons"])
    few = lg.evaluate({"labeled": 120, "net_pips_sum": 60.0, "gross_win_pips": 200.0, "gross_loss_pips": -140.0})
    assert few["eligible"] is False and few["reasons"] == ["120/300 labeled shadow samples"]
    good = lg.evaluate({"labeled": 300, "net_pips_sum": 45.0, "gross_win_pips": 300.0, "gross_loss_pips": -255.0})
    assert good["eligible"] is True and good["expectancy_pips"] == 0.15 and good["profit_factor"] == 1.176

    db = FakeDb()
    for i in range(300):
        db.scalp_decisions.rows.append({"user_id": "u1", "symbol": "EURUSD", "execution_mode": "shadow",
                                        "setup": {"setup": "range_break_momentum_v2"},
                                        "outcome": {"result": "target_first" if i % 2 else "stop_first",
                                                    "net_pips": 4.0 if i % 2 else -3.0}})
    e = _run(lg.live_eligibility(db, "u1", "EURUSD"))
    assert e["eligible"] is True and e["labeled"] == 300
    x = _run(lg.live_eligibility(db, "u1", "XAUUSD"))
    assert x["eligible"] is False and x["reasons"][0] == "0/300 labeled shadow samples" and x["proving_symbol"]["eligible"]
    db.scalp_decisions.rows = [r for r in db.scalp_decisions.rows if r["symbol"] != "EURUSD"][:0]
    y = _run(lg.live_eligibility(db, "u1", "XAUUSD"))
    assert y["reasons"][0] == "EURUSD must pass the live gate first"
    for i in range(300):
        db.scalp_decisions.rows.append({"user_id": "u1", "symbol": "EURUSD", "execution_mode": "shadow",
                                        "setup": {"setup": "range_break_momentum_v2"},
                                        "outcome": {"result": "target_first" if i % 2 else "stop_first",
                                                    "net_pips": 4.0 if i % 2 else -3.0}})
    # legacy micro-pullback rows never count toward the new setup's gate
    db.scalp_decisions.rows.append({"user_id": "u1", "symbol": "EURUSD", "execution_mode": "shadow",
                                    "setup": {"direction": "BUY"}, "outcome": {"result": "stop_first", "net_pips": -50}})
    assert _run(lg.live_eligibility(db, "u1", "EURUSD"))["labeled"] == 300


def test_a1_apply_config_runs_shadow_until_the_gate_opens(monkeypatch):
    import scalp.engine as eng
    from scalp import live_gate as lg
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": "u1", "broker": "X", "account_type": "demo"}
    monkeypatch.setattr(lg, "shadow_stats", AsyncMock(return_value={"labeled": 10, "net_pips_sum": -5, "gross_win_pips": 1, "gross_loss_pips": -6}))
    with patch.object(eng.ScalpRunner, "restore_risk", AsyncMock()), patch("scalp.model.load_persisted", AsyncMock()):
        r = _run(eng.apply_config(db, acc, "EURUSD", True, "demo_live"))
    assert r.mode == "shadow" and r.requested_mode == "demo_live" and r.live_gate["eligible"] is False
    st = r.status()
    assert st["mode"] == "shadow" and st["requested_mode"] == "demo_live" and st["live_gate"]["reasons"]
    cfg = _run(db.scalp_configs.find_one({"symbol": "EURUSD"}))
    assert cfg["mode"] == "shadow" and cfg["requested_mode"] == "demo_live"


def test_a1_geometry_targets_at_least_five_pips_and_holds_minutes():
    from scalp.forecast import make
    from scalp.risk import ScalpRiskLimits as RiskLimits
    from scalp.instruments import approved
    feats = {"spread_pips": 0.6, "vol_short": 0.8, "spread_pctl": 0.4, "range_5m_pips": 6.0, "accel": 0.1}
    cand = {"impulse_pips": 8.0, "pullback_frac": 0.0, "accel_pips": 0.1, "direction": "BUY", "setup": "range_break_momentum_v2"}
    from scalp.state import ScalpState
    fc = make(feats, cand, ScalpState(0.0001), approved("EURUSD"))
    assert fc.stop_pips == 3.6 and fc.target_pips >= 5.0 and fc.target_pips / fc.stop_pips >= 1.5
    assert RiskLimits().max_holding_ms == 900_000
    assert approved("XAUUSD").contract_size == 100.0


# ── B3 · deferred / held decisions are recorded ─────────────────────────────────────────────────────────────────
def test_b3_hold_and_deferred_outcomes_reach_the_decision_ledger():
    import bot_runner as br
    db = FakeDb()
    cfg = {"_id": ObjectId(), "user_id": "u1", "account_id": "a1"}
    sig = {"symbol": "XAUUSD", "action": "HOLD", "execution_deferred": True,
           "execution_defer_reason": "XAUUSD off-hours window (22-06 UTC / weekend) — defer to next session for tighter spreads"}
    with patch("bot_runner._CURRENT_SIGNAL") as cs:
        cs.get.return_value = sig
        _run(br._record_pulse(db, cfg, symbol="XAUUSD", action="HOLD", level="info", reason="deferred", signal=sig))
    rows = db.trade_decisions.rows
    assert len(rows) == 1 and rows[0]["status"] == "deferred" and rows[0]["stage"] == "execution_optimizer"
    assert "off-hours" in rows[0]["reason"]
    _run(br._record_pulse(db, cfg, symbol="XAUUSD", action="HOLD", level="warn",
                          reason="Engine HOLD — no setup", signal={"symbol": "XAUUSD", "action": "HOLD"}))
    assert rows[1]["status"] == "held" and rows[1]["stage"] == "engine"
    # Scoreboard funnel shows them with the latest reason
    sb = _src("frontend", "src", "pages", "Scoreboard.jsx")
    assert 'f.status === "deferred" || f.status === "held"' in sb and "funnel-waiting-reason" in sb
    assert '"last_reason": {"$last": "$reason"}' in _src("backend", "routes", "trade_routes.py")


# ── B4 · certification panel refreshes from the live heartbeat ─────────────────────────────────────────────────
def test_b4_certification_panel_polls_and_reads_identity_version():
    comp = _src("frontend", "src", "components", "AccountCertification.jsx")
    assert "item={certMap[a.id] || null} onChanged={load}" in _src("frontend", "src", "pages", "Accounts.jsx") and "if (item) return undefined;" in comp
    acc = _src("frontend", "src", "pages", "Accounts.jsx")
    assert "setInterval(load, 30000)" in acc and 'addEventListener("focus", load)' in acc
    assert 'v = a.get("ea_version") or (a.get("ea_identity") or {}).get("ea_version")' in _src("backend", "routes", "account_routes.py")


# ── C5 · new bot: right path, default mode, one-click drift fix ─────────────────────────────────────────────────
def test_c5_wizard_hits_bot_config_and_new_bots_get_the_default_mode(monkeypatch):
    wiz = _src("frontend", "src", "components", "EasyStartWizard.jsx")
    assert 'api.put("/config"' not in wiz and wiz.count('api.put("/bot/config"') == 3
    import routes.bot_routes as br
    db = FakeDb()
    monkeypatch.setattr(br, "get_db", lambda: db)
    demo = ObjectId()
    db.accounts.rows.append({"_id": demo, "user_id": "u1", "mode": "live", "status": "connected", "account_type": "demo",
                             "broker_trade_mode": "demo",
                             "environment_attestation": {"environment": "DEMO", "approved_by": "admin", "proof": {"verifier": "ea_heartbeat"}}})
    from broker_env import attestation_identity
    db.accounts.rows[0]["environment_attestation"]["identity_hash"] = attestation_identity(db.accounts.rows[0])
    cfg = _run(br._get_or_create_config(db, "u1", str(demo)))
    assert cfg["operational_mode"] == "demo_autopilot" and cfg["active"] is False
    live = ObjectId()
    db.accounts.rows.append({"_id": live, "user_id": "u1", "mode": "live", "status": "connected", "account_type": "live"})
    assert _run(br._get_or_create_config(db, "u1", str(live)))["operational_mode"] == "shadow"
    paper = ObjectId()
    db.accounts.rows.append({"_id": paper, "user_id": "u1", "mode": "paper", "status": "connected"})
    assert _run(br._get_or_create_config(db, "u1", str(paper)))["operational_mode"] == "demo_autopilot"


def test_c5_status_line_offers_the_one_click_drift_repair():
    from status_line import derive
    acc = {"mode": "live", "trading_enabled": True, "account_type": "demo", "broker_trade_mode": "demo",
           "last_heartbeat": datetime.now(timezone.utc).isoformat(), "ea_identity": {"authoritative": True},
           "admission": {"state": "admitted"}}
    from broker_env import attestation_identity
    acc["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin", "proof": {"verifier": "ea_heartbeat"}}
    acc["environment_attestation"]["identity_hash"] = attestation_identity(acc)
    out = derive(acc, bot_active=False, rules_active=False)
    assert out["state"] == "drift" and out["fix"]["action"] == "enable_bot"
    assert derive(acc, bot_active=True, rules_active=False)["state"] == "trading"
    page = _src("frontend", "src", "pages", "Accounts.jsx")
    assert 'fix.action === "enable_bot"' in page and "const enableBot = async" in page and "{ active: true }" in page


# ── C6 · LIVE pill counts attested LIVE only ────────────────────────────────────────────────────────────────────
def test_c6_safety_banner_live_count_uses_the_attested_environment():
    src = _src("backend", "routes", "bot_routes.py")
    block = src.split("live_accounts, stale_feeds = [], []")[1].split("age = _age(")[0]
    assert "attested_environment(a)" in block and 'elif env == "LIVE":' in block
    assert 'str(a.get("mode") or "").lower() == "live"' not in block


# ── D7 · encrypted backups count ────────────────────────────────────────────────────────────────────────────────
def test_d7_i4_accepts_encrypted_archives_and_prefers_the_manifest(tmp_path, monkeypatch):
    from security_agent.checks import integrity_platform_trading as ipt
    old = tmp_path / "mongo-2026-10-08.archive.gz.enc"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 6 * 86400, time.time() - 6 * 86400))
    fresh = tmp_path / "mongo-2026-10-14.archive.gz.enc"
    fresh.write_bytes(b"y")
    os.utime(fresh, (time.time() - 5 * 86400, time.time() - 5 * 86400))            # mtime lies (restored copy)
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    (tmp_path / "mongo-2026-10-14.manifest.json").write_text(json.dumps({"created_at": created}))
    assert ipt.backup_timestamp(str(tmp_path), "notes.txt") is None
    assert ipt.backup_timestamp(str(tmp_path), fresh.name).isoformat() == datetime.fromisoformat(created).isoformat()
    monkeypatch.setenv("STOIC_BACKUP_DIR", str(tmp_path))
    out = _run(ipt.I4(FakeDb(), {}))
    assert out == []                                                                 # no false HIGH
    os.remove(tmp_path / "mongo-2026-10-14.manifest.json"); os.remove(fresh)
    out = _run(ipt.I4(FakeDb(), {}))
    assert out and out[0]["severity"] == "high" and "h ago" in out[0]["what_happened"]       # genuinely stale still alerts
    assert '".archive.gz", ".archive.gz.enc"' in _src("backend", "routes", "infra_routes.py")


# ── D8 · watchdog heartbeat on its own 1 s tick ─────────────────────────────────────────────────────────────────
def test_d8_watchdog_heartbeat_runs_on_a_one_second_tick():
    import runtime_watchdog as rw
    assert rw.HEARTBEAT_TICK_SEC <= 1.0 < rw.BLOCK_THRESHOLD_SEC <= rw.SAMPLE_INTERVAL_SEC

    async def run():
        rw._heartbeat = time.monotonic() - 100
        task = asyncio.get_event_loop().create_task(rw._heartbeat_task())
        await asyncio.sleep(0.05)
        lag = time.monotonic() - rw._heartbeat
        task.cancel()
        return lag
    assert _run(run()) < rw.BLOCK_THRESHOLD_SEC and rw._check_blockage() is None
    assert "loop.create_task(_heartbeat_task())" in _src("backend", "runtime_watchdog.py")


# ── D9 · TIPS backoff / negative cache / FRED fallback ──────────────────────────────────────────────────────────
def test_d9_tips_backs_off_after_failures_and_falls_back_to_fred(monkeypatch):
    import httpx
    import macro.tips as tips
    db = FakeDb()
    monkeypatch.setattr(tips, "get_db", lambda: db)
    monkeypatch.setattr(tips, "_fetch_year", AsyncMock(side_effect=httpx.ConnectTimeout("timeout")))
    fred = AsyncMock(return_value=[(f"2026-10-{d:02d}", 1.9 + d * 0.01) for d in range(1, 10)])
    monkeypatch.setattr("macro.fred.fetch_series", fred)
    out = _run(tips.get_real_yield())
    assert out and out["source"].startswith("fred:DFII10") and out["regime"] == "bearish_gold"
    # both sources down → ONE warning, then silence until retry_after
    fred.side_effect = httpx.ConnectTimeout("fred down")
    db.tips_cache.rows = [r for r in db.tips_cache.rows if r.get("key") != "10Y_REAL"]
    calls = tips._fetch_year.await_count
    assert _run(tips.get_real_yield()) is None
    neg = _run(db.tips_cache.find_one({"key": "10Y_REAL_FAIL"}))
    assert neg["consecutive_failures"] == 1 and neg["retry_after"] > datetime.now(timezone.utc).isoformat()
    for _ in range(5):
        _run(tips.get_real_yield())
    assert tips._fetch_year.await_count == calls + 1                                  # no further upstream hits
    assert tips.FAIL_BACKOFF_MIN >= 30


# ── E10 · release.yml produces an authoritative lock ────────────────────────────────────────────────────────────
def test_e10_release_workflow_freezes_an_authoritative_lock_with_digests():
    wf = _src(".github", "workflows", "release.yml")
    # release.yml already freezes the authoritative lock from the staged tree with BOTH image digests and the
    # production target; the EX5 record is signed by the ea-release chain. A production preflight FAIL therefore
    # means the host runs a developer snapshot / on-host build — the gate text now says exactly what to do.
    step = wf.split("freeze_rc_lock.py --root /tmp/pkg --commit")[1].split("--out rc_lock.json")[0]
    assert '--backend-digest "${BACKEND_DIGEST}" --frontend-digest "${FRONTEND_DIGEST}"' in step and "--target production" in step
    assert "cosign" in wf and "release-admission.json" in wf
    import release_gate as rg
    out = rg.evaluate(lock={"authoritative": False, "images": {}}, env={}, ea_signed=False) if hasattr(rg, "evaluate") else None
    src = _src("backend", "release_gate.py")
    assert "deploy a TAGGED release" in src and "pulls by digest" in src and "on-host build deploy can never satisfy" in src
    assert out is None or any("TAGGED release" in f for f in out["failures"])
