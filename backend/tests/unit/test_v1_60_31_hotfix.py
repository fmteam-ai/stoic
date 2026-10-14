"""STOIC v1.60.31 hotfix — /scalp/status 500 on None features + six same-release items."""
import asyncio
import os
import sys
from datetime import datetime, timezone
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


def _runner(monkeypatch, symbol="EURUSD"):
    import scalp.engine as eng
    from scalp import live_gate as lg
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": "u1", "broker": "X", "account_type": "demo"}
    monkeypatch.setattr(lg, "shadow_stats", AsyncMock(return_value={"labeled": 0, "net_pips_sum": 0, "gross_win_pips": 0, "gross_loss_pips": 0}))
    with patch.object(eng.ScalpRunner, "restore_risk", AsyncMock()), patch("scalp.model.load_persisted", AsyncMock()):
        return _run(eng.apply_config(db, acc, symbol, True, "shadow"))


# ── hotfix · status() with < 5 minutes of ticks ────────────────────────────────────────────────────────────────
def test_scalp_status_survives_none_features_before_five_minutes_of_ticks(monkeypatch):
    from scalp.features import snapshot
    from scalp.state import TickEvent
    r = _runner(monkeypatch)
    t0 = 1_700_000_000_000
    for i in range(25):                       # 25 ticks over 50 s — the 5-minute window is not populated yet
        ms = t0 + i * 2000
        r.state.update(TickEvent("EURUSD", ms, ms, 1.10000 + i * 0.00001, 1.10007 + i * 0.00001))
    feats = snapshot(r.state)
    assert feats is not None and feats["range_5m_pips"] is None and feats["ret_5m"] is None
    st = r.status()                            # used to raise TypeError: NoneType doesn't define __round__
    f = st["features"]
    assert f["range_5m_pips"] is None and f["break_up_pips"] is None and f["twap_dev_pips"] is None
    assert isinstance(f["spread_pips"], float) and f["spread_pips"] == round(feats["spread_pips"], 3)
    assert st["requested_mode"] == "shadow"


def test_scalp_status_has_no_bare_round_over_features():
    src = _src("backend", "scalp", "engine.py")
    assert "{k: round(v, 3) for k, v in feats.items()" not in src
    assert "isinstance(v, (int, float)) and not isinstance(v, bool) else v" in src


# ── item 1 · FRED keyed API + no empty-snapshot caching ────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, json_body=None, text=""):
        self.status_code = status
        self._json = json_body
        self.text = text

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://api.stlouisfed.org/fred/series/observations?api_key=SECRET")
            raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._json


class _Client:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        _Client.calls.append((url, dict(params or {})))
        if "api.stlouisfed.org" in url:
            if params.get("api_key") == "bad":
                return _Resp(400, {})
            return _Resp(200, {"observations": [{"date": "2026-09-01", "value": "4.33"}, {"date": "2026-10-13", "value": "."},
                                                {"date": "2026-10-12", "value": "4.10"}]})
        return _Resp(200, text="DATE,VALUE\n2026-09-01,4.33\n2026-10-12,4.10\n")


def test_fred_uses_the_keyed_api_when_a_key_is_set_and_never_leaks_it(monkeypatch):
    import macro.fred as fred
    monkeypatch.setattr(fred.httpx, "AsyncClient", _Client)
    _Client.calls.clear()
    monkeypatch.setenv("FRED_API_KEY", "a" * 32)
    rows = _run(fred.fetch_series("DFII10", lookback_days=100))
    assert rows == [("2026-09-01", 4.33), ("2026-10-12", 4.10)]      # "." dropped, sorted ascending
    url, params = _Client.calls[-1]
    assert url == fred.API_ENDPOINT and params["series_id"] == "DFII10" and params["file_type"] == "json"
    assert "observation_start" in params and fred.transport() == "fred_api"
    # keyless fallback
    monkeypatch.delenv("FRED_API_KEY")
    rows = _run(fred.fetch_series("VIXCLS"))
    assert rows[-1] == ("2026-10-12", 4.10) and _Client.calls[-1][0].startswith("https://fred.stlouisfed.org/graph/fredgraph.csv")
    # a rejected key raises FredError whose text carries NO url / key
    monkeypatch.setenv("FRED_API_KEY", "bad")
    with pytest.raises(fred.FredError) as ei:
        _run(fred.fetch_series("DFF"))
    assert "HTTP 400" in str(ei.value) and "SECRET" not in str(ei.value) and "api_key" not in str(ei.value)


def test_fred_snapshot_never_caches_an_empty_fetch(monkeypatch):
    import macro.fred as fred
    db = FakeDb()
    monkeypatch.setattr(fred, "get_db", lambda: db)
    monkeypatch.setattr(fred, "fetch_series", AsyncMock(side_effect=fred.FredError("HTTP 503 from the keyed FRED API for DFF")))
    out = _run(fred.get_macro_snapshot())
    assert out["stale"] is True and out["series"] == {} and "HTTP 503" in out["error"]
    assert _run(db.fred_cache.find_one({"_id": "snapshot"})) is None           # no 6-hour empty snapshot
    neg = _run(db.fred_cache.find_one({"_id": "snapshot_fail"}))
    assert neg and neg["retry_after"] > neg["failed_at"]
    # inside the backoff window FRED is not called again
    fred.fetch_series.reset_mock()
    out2 = _run(fred.get_macro_snapshot())
    assert out2["stale"] is True and fred.fetch_series.await_count == 0
    # TIPS fallback goes through the same transport
    assert 'fetch_series("DFII10", lookback_days=120)' in _src("backend", "macro", "tips.py")


# ── item 2 · FRED_API_KEY in Admin → Integrations ──────────────────────────────────────────────────────────────
def test_fred_api_key_is_an_admin_integration(monkeypatch):
    import integrations_settings as integ
    assert integ.REGISTRY["FRED_API_KEY"][0] == "fred" and integ.REGISTRY["FRED_API_KEY"][1] is True
    assert integ.PROVIDERS["fred"] and "FRED_API_KEY" in integ.VALIDATORS
    assert '"fred"' in _src("frontend", "src", "pages", "AdminIntegrations.jsx")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    res = _run(integ.test_provider(FakeDb(), "fred", {"id": "u1", "email": "a@b"}))
    assert res["ok"] is False and "not configured" in res["detail"]
    monkeypatch.setenv("FRED_API_KEY", "b" * 32)
    with patch("macro.fred.fetch_series", AsyncMock(return_value=[("2026-10-12", 4.1)])):
        res = _run(integ.test_provider(FakeDb(), "fred", {"id": "u1", "email": "a@b"}))
    assert res["ok"] is True and "2026-10-12" in res["detail"]


# ── item 3 · gold-specific scalp thresholds and geometry ───────────────────────────────────────────────────────
def test_xauusd_scalp_uses_gold_scale_thresholds_and_geometry():
    from scalp import setup
    from scalp.forecast import make, geometry_for
    from scalp.instruments import approved
    from scalp.state import ScalpState
    p = setup.params_for("XAUUSD")
    assert p["max_spread_pips"] == 35.0 and p["momentum_min_pips"] == 30.0 and p["break_min_pips"] == 5.0
    assert setup.params_for("EURUSD") == setup.DEFAULTS
    assert setup.params_for("XAUUSD", {"late_move_frac": 0.5})["late_move_frac"] == 0.5     # presets still apply
    # a realistic gold tape: 22-pip spread, 90-pip 5-minute range, 60-pip momentum, 8-pip fresh break
    feats = {"spread_pips": 22.0, "spread_pctl": 0.4, "ret_5m": 60.0, "range_5m_pips": 90.0, "break_up_pips": 8.0,
             "break_dn_pips": None, "ret_1m": 12.0, "twap_dev_pips": 10.0, "vol_long": 12.0, "vol_short": 9.0, "accel": 0.5}
    assert setup.detect(feats, None, symbol="EURUSD") is None            # EURUSD thresholds can never fire on gold
    cand = setup.detect(feats, None, symbol="XAUUSD")
    assert cand and cand["direction"] == "BUY"
    fc = make(feats, cand, ScalpState(0.01), approved("XAUUSD"))
    g = geometry_for("XAUUSD")
    assert g["stop_min"] <= fc.stop_pips <= g["stop_max"] and fc.target_pips >= 60.0
    assert fc.target_pips > 2 * feats["spread_pips"]                        # target is never inside the spread
    # EURUSD geometry unchanged
    efe = {"spread_pips": 0.6, "vol_short": 0.8, "spread_pctl": 0.4, "range_5m_pips": 6.0, "accel": 0.1}
    ec = {"impulse_pips": 8.0, "pullback_frac": 0.0, "accel_pips": 0.1, "direction": "BUY", "setup": "range_break_momentum_v2"}
    efc = make(efe, ec, ScalpState(0.0001), approved("EURUSD"))
    assert efc.stop_pips == 3.6 and efc.target_pips >= 5.0


# ── item 4 · held / deferred rows expire ───────────────────────────────────────────────────────────────────────
def test_held_and_deferred_decisions_carry_a_retention_expiry():
    import trade_decisions as td
    db = FakeDb()
    cfg = {"_id": ObjectId(), "user_id": "u1", "account_id": "a1"}
    _run(td.record_decision(db, user_id="u1", symbol="XAUUSD", status="held", stage="engine", reason="no setup", cfg=cfg))
    _run(td.record_decision(db, user_id="u1", symbol="XAUUSD", status="deferred", stage="execution_optimizer", reason="off-hours", cfg=cfg))
    _run(td.record_decision(db, user_id="u1", symbol="XAUUSD", status="rejected", stage="news_gate", reason="news", cfg=cfg))
    rows = db.trade_decisions.rows
    held, deferred, rejected = rows
    assert isinstance(held["expires_at"], datetime) and isinstance(deferred["expires_at"], datetime)
    assert "expires_at" not in rejected                                      # the ledger truth never expires
    days = (held["expires_at"] - datetime.now(timezone.utc)).days
    assert td.HOLD_RETENTION_DAYS - 1 <= days <= td.HOLD_RETENTION_DAYS
    assert "ensure_decision_indexes" in _src("backend", "seed.py")
    assert 'create_index("expires_at", expireAfterSeconds=0)' in _src("backend", "trade_decisions.py")


# ── item 5 · readiness scope: one stale EA blocks ONE account ──────────────────────────────────────────────────
def test_readiness_scopes_a_single_stale_terminal_to_its_account(monkeypatch):
    import trading_readiness as tr
    rows = [
        {"account_id": "a1", "label": "RoboForex demo", "account_enabled": True, "bot_enabled": True,
         "effective_state": "BLOCKED", "position_truth": "STALE", "heartbeat_age_seconds": 7200},
        {"account_id": "a2", "label": "IC Markets live", "account_enabled": True, "bot_enabled": True,
         "effective_state": "RUNNING", "position_truth": "FRESH", "heartbeat_age_seconds": 5},
    ]
    dec = {"blockers": [], "state": "READY", "decision_id": "dec_x", "dominant_code": "READY"}
    db = FakeDb()
    with patch("state_contract.contract", AsyncMock(return_value={"accounts": rows})), \
         patch("execution_truth.unresolved_executions", AsyncMock(return_value=[])), \
         patch("execution_truth.position_mismatches", AsyncMock(return_value=[])), \
         patch("canonical_decision.decide_user", AsyncMock(return_value=dec)):
        out = _run(tr.readiness(db, "u1"))
    assert out["level"] == "BLOCKED" and out["scope"] == "accounts"
    assert out["blocked_accounts"] == [{"account_id": "a1", "label": "RoboForex demo"}] and out["clear_accounts"] == 1
    # both terminals stale → the whole user is blocked, no partial wording
    rows[1].update({"position_truth": "STALE", "effective_state": "BLOCKED", "heartbeat_age_seconds": 900})
    with patch("state_contract.contract", AsyncMock(return_value={"accounts": rows})), \
         patch("execution_truth.unresolved_executions", AsyncMock(return_value=[])), \
         patch("execution_truth.position_mismatches", AsyncMock(return_value=[])), \
         patch("canonical_decision.decide_user", AsyncMock(return_value=dec)):
        out = _run(tr.readiness(db, "u1"))
    assert out["scope"] == "platform" and out["clear_accounts"] == 0
    strip = _src("frontend", "src", "components", "TradingReadinessStrip.jsx")
    assert 'data.scope === "accounts"' in strip and "trading-readiness-scope" in strip and "trading continues there" in strip


# ── item 6 · Loss Lab: attribution agrees with the taxonomy, $0 is not "saving money" ──────────────────────────
def test_attribution_does_not_blame_the_signal_for_an_operational_failure():
    from outcome_attribution import _weights
    from failure_classifier import classify_failure
    trade = {"pnl": -40.0, "pnl_estimated": True, "entry_price": 1.1, "stop_loss": 1.09, "symbol": "EURUSD"}
    assert classify_failure(trade)["category"] == "operational_failure"
    base = {"slippage_ratio": 0.0, "fill_delay_s": 0.0, "dispatch_retries": 0, "intent_degraded": False, "ghost_or_external": False,
            "broker_reject_text": False, "broker_incident_overlap": False, "news_hits": 0, "authority_reduced": False,
            "concurrent_losers": 0, "session_shift": False}
    clean = _weights({**base, "operational_failure": False}, -1.0)
    ops = _weights({**base, "operational_failure": True}, -1.0)
    assert clean["ALPHA_ERROR"] > 0.5 and "ALPHA_ERROR" not in ops
    assert ops["INFRASTRUCTURE_ERROR"] >= clean["ALPHA_ERROR"]
    assert abs(sum(ops.values()) - sum(clean.values())) < 0.01


def test_verdict_lesson_is_honest_about_a_zero_dollar_ledger():
    from verdict_tracking import _lesson
    zero = {"resolved": 3, "saved_usd": 0.0, "opportunity_cost_usd": 0.0, "net_benefit_usd": 0.0}
    assert "$0" in _lesson(zero) and "SAVING" not in _lesson(zero)
    assert "SAVING" in _lesson({"resolved": 3, "saved_usd": 12.0, "opportunity_cost_usd": 2.0, "net_benefit_usd": 10.0})
    assert "COSTING" in _lesson({"resolved": 3, "saved_usd": 1.0, "opportunity_cost_usd": 5.0, "net_benefit_usd": -4.0})
    assert "No resolved verdicts" in _lesson({"resolved": 0, "saved_usd": 0.0, "opportunity_cost_usd": 0.0, "net_benefit_usd": 0.0})
