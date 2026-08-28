"""v62.6 — PAMM Risk Truth & Production Gate (pure unit tests).
Physically isolated: no DB, no network, no running backend."""
import inspect

import pytest

pytestmark = pytest.mark.unit


class TestEffectiveEnvelopeKeys:
    def _profile(self):
        return {"max_risk_per_trade": 0.5, "max_daily_loss": 2.0,
                "max_weekly_loss": 5.0, "max_drawdown": 10.0,
                "max_open_positions": 4, "max_symbol_exposure": 1.0,
                "max_factor_exposure": 2.0, "max_consecutive_losses": 4,
                "allowed_symbols": None,
                "spread_limits": {"max_spread_pips": 2.0},
                "slippage_limits": {"max_slippage_pips": 1.0}}

    def test_envelope_includes_every_v626_limit(self):
        from modules.pamm.risk_profiles import effective_envelope
        env = effective_envelope(self._profile(), None)
        assert env["max_weekly_loss_pct"] == 5.0
        assert env["max_drawdown_pct"] == 10.0
        assert env["max_factor_exposure_lots"] == 2.0
        assert env["max_spread_pips"] == 2.0
        assert env["max_slippage_pips"] == 1.0

    def test_program_limits_still_strictest(self):
        from modules.pamm.risk_profiles import effective_envelope
        env = effective_envelope(
            self._profile(),
            {"weekly_loss_pct": {"enabled": True, "threshold": 3.0},
             "max_drawdown_pct": {"enabled": True, "threshold": 8.0}})
        assert env["max_weekly_loss_pct"] == 3.0
        assert env["max_drawdown_pct"] == 8.0


class TestEnvelopeViolationsV626:
    def _env(self):
        from modules.pamm.risk_profiles import effective_envelope
        return effective_envelope(
            {"max_weekly_loss": 5.0, "max_drawdown": 10.0,
             "max_factor_exposure": 2.0,
             "spread_limits": {"max_spread_pips": 2.0},
             "slippage_limits": {"max_slippage_pips": 1.0}}, None)

    def test_weekly_loss_cap(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"weekly_loss_pct": 5.0})
        assert v and v[0]["reason"] == "weekly_loss_cap_reached"

    def test_drawdown_cap(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"drawdown_pct": 11.2})
        assert v and v[0]["reason"] == "drawdown_cap_reached"

    def test_spread_cap(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"spread_pips": 2.4})
        assert v and v[0]["reason"] == "spread_cap_exceeded"
        assert envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                   {"spread_pips": 1.9}) == []

    def test_pre_trade_slippage_on_measured_evidence(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"recent_slippage_pips": 1.5})
        assert v and v[0]["reason"] == "expected_slippage_exceeded"

    def test_factor_exposure_uses_both_symbol_factors(self):
        from modules.pamm.strategy_guard import envelope_violations
        t = {"factor_lots": {"USD": 1.9, "XAU": 0.1}}
        v = envelope_violations(self._env(), {"symbol": "XAUUSD",
                                              "lot_size": 0.2}, t)
        assert v and v[0]["reason"] == "factor_exposure_exceeded"
        assert v[0]["detail"]["factor"] == "XAU" or \
            v[0]["detail"]["factor"] == "USD"

    def test_missing_telemetry_never_fabricates(self):
        from modules.pamm.strategy_guard import envelope_violations
        assert envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                   {}) == []


class TestRequiredTelemetrySpec:
    def test_required_and_optional_are_disjoint_and_complete(self):
        from modules.pamm.strategy_guard import (OPTIONAL_TELEMETRY,
                                                 REQUIRED_TELEMETRY)
        assert set(REQUIRED_TELEMETRY) == {
            "open_positions", "open_risk_pct_sum", "daily_loss_pct",
            "weekly_loss_pct", "drawdown_pct", "spread_pips"}
        assert not set(REQUIRED_TELEMETRY) & set(OPTIONAL_TELEMETRY)

    def test_missing_required_detection(self):
        from modules.pamm.strategy_guard import missing_required_telemetry
        full = {"open_positions": 0, "open_risk_pct_sum": 0.0,
                "daily_loss_pct": 0.0, "weekly_loss_pct": 0.0,
                "drawdown_pct": 0.0, "spread_pips": 1.2}
        assert missing_required_telemetry(full) == []
        gap = dict(full, spread_pips=None, drawdown_pct=None)
        assert set(missing_required_telemetry(gap)) == {
            "spread_pips", "drawdown_pct"}
        assert missing_required_telemetry({}) != []


class TestStrategySpecificFreshness:
    def test_nitro_demands_fresher_evidence_than_sniper(self):
        from modules.pamm.strategy_guard import telemetry_freshness
        sniper = telemetry_freshness("sniper")
        nitro = telemetry_freshness("nitro_scalper")
        assert nitro["spread_s"] < sniper["spread_s"]
        assert nitro["position_truth_s"] <= sniper["position_truth_s"]
        assert nitro["latency_sensitivity"] == "MAXIMUM"

    def test_unknown_strategy_gets_safe_default(self):
        from modules.pamm.strategy_guard import telemetry_freshness
        f = telemetry_freshness(None)
        assert f["spread_s"] == 300 and f["position_truth_s"] == 900


class TestCanaryOpenRiskRename:
    def test_constant_renamed_and_reason_accurate(self):
        from modules.pamm import strategy_guard as g
        assert g.CANARY_MAX_OPEN_RISK_PCT == 5.0
        assert not hasattr(g, "CANARY_MAX_CAPITAL_PCT")
        cv = g.canary_violation(4.9, 0.4)
        assert cv["reason"] == "canary_open_risk_cap_exceeded"

    def test_campaign_criteria_renamed(self):
        from strategies.certification_campaign import (
            CANARY_MAX_OPEN_RISK_PCT, STAGE_CRITERIA)
        assert STAGE_CRITERIA["CANARY"]["max_open_risk_pct"] == \
            CANARY_MAX_OPEN_RISK_PCT
        assert "max_capital_pct" not in STAGE_CRITERIA["CANARY"]


class TestHelpers:
    def test_symbol_factors(self):
        from modules.pamm.strategy_guard import symbol_factors
        assert symbol_factors("XAUUSD") == ["XAU", "USD"]
        assert symbol_factors("BTCUSD") == ["BTC", "USD"]
        assert symbol_factors("") == []

    def test_age_seconds(self):
        from datetime import datetime, timezone

        from modules.pamm.strategy_guard import _age_s
        now = datetime.now(timezone.utc).isoformat()
        assert _age_s(now) < 5
        assert _age_s(None) is None
        assert _age_s("not-a-date") is None


class TestGateWiring:
    def test_risk_unknown_and_fresh_position_truth_wired(self):
        from modules.pamm import strategy_guard as g
        src = inspect.getsource(g._authorize)
        assert "risk_unknown" in src
        assert "position_truth_stale" in src
        assert "position_truth_missing" in src
        assert "missing_required_telemetry" in src
        assert 'env_name == "LIVE"' in src

    def test_decision_snapshot_persisted_and_stamped(self):
        from modules.pamm import strategy_guard as g
        src = inspect.getsource(g.authorize_pamm_strategy_execution)
        assert "pamm_risk_decisions" in src
        assert "risk_snapshot_id" in src
        assert '"envelope"' in src and '"telemetry"' in src

    def test_trade_stamp_carries_slippage_cap_and_snapshot(self):
        import execution
        src = inspect.getsource(execution.stamp_pamm_identity)
        assert "pamm_max_slippage_pips" in src
        assert "pamm_risk_snapshot_id" in src

    def test_bridge_enforces_pamm_slippage_post_trade(self):
        import pathlib
        src = pathlib.Path(
            __file__).parents[3].joinpath(
            "routes/bridge_routes.py").read_text()
        assert "pamm_slippage_veto" in src
        assert "pamm_slippage_violations" in src
        assert "pamm_max_slippage_pips" in src

    def test_authority_stamps_snapshot_on_intent(self):
        import pathlib
        src = pathlib.Path(__file__).parents[3].joinpath(
            "execution_authority.py").read_text()
        assert "payload.risk_snapshot_id" in src
