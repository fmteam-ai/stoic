"""v62.4 — PAMM Safety Closure (offline suite): fail-closed governance,
explicit provenance, manual override path, canary envelope at execution,
certification identity binding, effective risk envelope."""
import pytest


@pytest.mark.unit
class TestEffectiveEnvelope:
    def _program_limits(self):
        return {"daily_loss_pct": {"enabled": True, "threshold": 5.0},
                "weekly_loss_pct": {"enabled": True, "threshold": 10.0},
                "max_drawdown_pct": {"enabled": False, "threshold": 20.0}}

    def test_strictest_wins_never_averages(self):
        from modules.pamm.risk_profiles import (DEFAULT_PROFILES,
                                                effective_envelope)
        controlled = DEFAULT_PROFILES[1]
        env = effective_envelope(controlled, self._program_limits())
        assert env["max_daily_loss_pct"] == 2.0  # profile 2.0 < program 5.0
        assert env["max_weekly_loss_pct"] == 5.0

    def test_program_limit_wins_when_stricter(self):
        from modules.pamm.risk_profiles import (DEFAULT_PROFILES,
                                                effective_envelope)
        growth = DEFAULT_PROFILES[2]  # daily 3.0
        env = effective_envelope(
            growth, {"daily_loss_pct": {"enabled": True, "threshold": 1.5}})
        assert env["max_daily_loss_pct"] == 1.5

    def test_disabled_program_limits_ignored(self):
        from modules.pamm.risk_profiles import (DEFAULT_PROFILES,
                                                effective_envelope)
        env = effective_envelope(DEFAULT_PROFILES[0],
                                 self._program_limits())
        assert env["max_drawdown_pct"] == 5.0  # profile only (prog disabled)

    def test_all_profile_axes_present(self):
        from modules.pamm.risk_profiles import (DEFAULT_PROFILES,
                                                effective_envelope)
        env = effective_envelope(DEFAULT_PROFILES[0], {})
        for k in ("max_risk_per_trade", "max_open_positions",
                  "max_symbol_exposure_lots", "max_consecutive_losses",
                  "allowed_symbols", "max_spread_pips",
                  "max_slippage_pips"):
            assert k in env


@pytest.mark.unit
class TestEnvelopeViolations:
    def _env(self, **over):
        base = {"max_risk_per_trade": 0.5, "max_daily_loss_pct": 2.0,
                "max_open_positions": 4, "max_symbol_exposure_lots": 1.0,
                "max_consecutive_losses": 4, "allowed_symbols": None}
        base.update(over)
        return base

    def test_allowed_symbols_rejects_other_symbol(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(allowed_symbols=["XAUUSD"]),
                                {"symbol": "EURUSD"}, {})
        assert v and v[0]["reason"] == "symbol_not_allowed"

    def test_allowed_symbols_accepts_listed(self):
        from modules.pamm.strategy_guard import envelope_violations
        assert envelope_violations(self._env(allowed_symbols=["XAUUSD"]),
                                   {"symbol": "xauusd"}, {}) == []

    def test_daily_loss_cap_reached_rejects(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"daily_loss_pct": 2.0})
        assert v and v[0]["reason"] == "daily_loss_cap_reached"

    def test_consecutive_losses_rejects(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                {"consecutive_losses": 4})
        assert v and v[0]["reason"] == "max_consecutive_losses_reached"

    def test_symbol_exposure_includes_requested_lots(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(
            self._env(), {"symbol": "XAUUSD", "lot_size": 0.5},
            {"symbol_open_lots": 0.6})
        assert v and v[0]["reason"] == "symbol_exposure_exceeded"

    def test_risk_cap(self):
        from modules.pamm.strategy_guard import envelope_violations
        v = envelope_violations(self._env(), {"symbol": "X",
                                              "risk_pct": 1.0}, {})
        assert v and v[0]["reason"] == "risk_cap_exceeded"

    def test_missing_telemetry_never_fabricates(self):
        from modules.pamm.strategy_guard import envelope_violations
        assert envelope_violations(self._env(), {"symbol": "XAUUSD"},
                                   {}) == []


@pytest.mark.unit
class TestCanaryEnvelope:
    def test_would_exceed_cap_rejects(self):
        # open 4.9% + new 0.4% = 5.3% > 5.0% cap → REJECT
        from modules.pamm.strategy_guard import canary_violation
        cv = canary_violation(4.9, 0.4)
        assert cv and cv["reason"] == "canary_cap_exceeded"
        assert cv["detail"]["proposed_total"] == 5.3

    def test_within_cap_allows(self):
        from modules.pamm.strategy_guard import canary_violation
        assert canary_violation(4.9, 0.05) is None

    def test_missing_risk_pct_fails_closed(self):
        from modules.pamm.strategy_guard import canary_violation
        cv = canary_violation(0.0, None)
        assert cv and cv["reason"] == "canary_requires_risk_pct"


@pytest.mark.unit
class TestCertificationIdentityBinding:
    def _a(self):
        return {"strategy_id": "sniper", "strategy_version": "1.0.0",
                "risk_profile_id": "controlled"}

    def test_broker_server_move_changes_identity(self):
        from modules.pamm.strategy_guard import current_identity_hash
        acc_a = {"broker": "ICM", "broker_server": "ICM-Live-A",
                 "broker_environment": "LIVE"}
        acc_b = {**acc_a, "broker_server": "ICM-Live-B"}
        assert (current_identity_hash("p1", self._a(), acc_a)
                != current_identity_hash("p1", self._a(), acc_b))

    def test_risk_profile_change_changes_identity(self):
        from modules.pamm.strategy_guard import current_identity_hash
        acc = {"broker": "ICM", "broker_server": "ICM-Live-A",
               "broker_environment": "LIVE"}
        a2 = {**self._a(), "risk_profile_id": "growth"}
        assert (current_identity_hash("p1", self._a(), acc)
                != current_identity_hash("p1", a2, acc))

    def test_identity_stable_for_same_facts(self):
        from modules.pamm.strategy_guard import current_identity_hash
        acc = {"broker": "ICM", "broker_server": "ICM-Live-A",
               "broker_environment": "LIVE"}
        assert (current_identity_hash("p1", self._a(), acc)
                == current_identity_hash("p1", self._a(), acc))


@pytest.mark.unit
class TestFailClosedAndProvenanceWiring:
    def test_guard_requires_provenance_and_fails_closed(self):
        import inspect

        from modules.pamm import strategy_guard
        src = inspect.getsource(
            strategy_guard.authorize_pamm_strategy_execution)
        assert "strategy_provenance_missing" in src
        assert "governed_program_requires_assignment" in src
        assert "canary_identity_drift" in src
        assert "certification_identity_drift" in src
        # governance check happens before the LEGACY early-return
        assert src.index("governance_mode") < src.index("legacy_mode")

    def test_assignment_makes_governance_sticky(self):
        import inspect

        from modules.pamm import strategy_assignment as sa
        assert '"strategy_governance": "STRATEGY"' in \
            inspect.getsource(sa.assign)
        assert "GOVERNED_NO_ACTIVE" in inspect.getsource(sa.get_assignment)

    def test_assignment_history_helper_exists(self):
        from modules.pamm.strategy_assignment import get_assignment_history
        assert callable(get_assignment_history)

    def test_manual_route_uses_override_origin(self):
        import inspect

        from routes import trade_routes
        src = inspect.getsource(trade_routes.execute_manual_trade)
        assert "require_step_up" in src
        assert "manual_override" in src
        assert "pamm_manual_override" in src

    def test_material_patch_resets_campaign(self):
        import inspect

        from modules.pamm import strategy_assignment as sa
        src = inspect.getsource(sa.patch)
        assert "revoke_campaign" in src

    def test_override_stamp_never_attributes_strategy(self):
        import execution
        doc = {}
        execution.stamp_pamm_identity(doc, {"_pamm_identity": {
            "pamm_program_id": "p1", "manual_override": True}})
        assert doc == {"pamm_program_id": "p1",
                       "pamm_manual_override": True}
        assert "pamm_strategy_id" not in doc

    def test_ownership_classifies_manual_override(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_manual_override": True, "pamm_program_id": "p1"}
        a = {"status": "ACTIVE", "strategy_id": "sniper",
             "strategy_version": "1.0.0"}
        assert _classify(t, "p1", a) == "MANUAL_OVERRIDE"
        t2 = {"pamm_manual_override": True, "pamm_program_id": "OTHER"}
        assert _classify(t2, "p1", a) == "PAMM_OWNER_MISMATCH"
