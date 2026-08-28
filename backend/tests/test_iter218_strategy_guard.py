"""v62.3 — Execution-plane enforcement: strategy guard, eligibility
policies, weight pinning, material-change governance (offline suite)."""
import pytest


@pytest.mark.unit
class TestExecutionEligibilityPolicies:
    def test_every_registered_strategy_has_a_policy_entry(self):
        from strategies.execution_eligibility import POLICIES
        from strategies.registry import REGISTRY
        assert set(POLICIES) == set(REGISTRY)

    def test_not_everything_is_nitro(self):
        from strategies.execution_eligibility import POLICIES
        labels = {sid: (p or {}).get("label") for sid, p in POLICIES.items()}
        assert labels["scalper"] == "MODERATE"
        assert labels["fast_scalp"] == "HIGH"
        assert labels["nitro_scalper"] == "EXTREME"
        assert POLICIES["sniper"] is None

    def test_thresholds_strictly_increase(self):
        from strategies.execution_eligibility import POLICIES
        assert (POLICIES["scalper"]["min_score"]
                < POLICIES["fast_scalp"]["min_score"]
                < POLICIES["nitro_scalper"]["min_score"])
        assert (POLICIES["scalper"]["hard_floor"]
                < POLICIES["fast_scalp"]["hard_floor"]
                < POLICIES["nitro_scalper"]["hard_floor"])

    def test_same_evidence_different_verdicts(self):
        # score 78 with decent components: scalper ELIGIBLE,
        # fast_scalp ELIGIBLE at 75+, nitro INELIGIBLE (<80)
        from strategies.execution_eligibility import apply_policy
        comps = {k: 78 for k in
                 ("spread_quality", "latency_quality", "broker_quality",
                  "infrastructure_health", "slippage_quality", "liquidity",
                  "market_quality", "regime_compatibility")}
        assert apply_policy("scalper", 78, comps)["status"] == "ELIGIBLE"
        assert apply_policy("fast_scalp", 78, comps)["status"] == "ELIGIBLE"
        assert apply_policy("nitro_scalper", 78,
                            comps)["status"] == "INELIGIBLE"

    def test_nitro_reduced_band(self):
        from strategies.execution_eligibility import apply_policy
        comps = {k: 85 for k in
                 ("spread_quality", "latency_quality", "broker_quality",
                  "infrastructure_health")}
        assert apply_policy("nitro_scalper", 85,
                            comps)["status"] == "REDUCED"

    def test_hard_floor_disqualifies_regardless_of_score(self):
        from strategies.execution_eligibility import apply_policy
        comps = {"spread_quality": 20, "latency_quality": 95,
                 "broker_quality": 95, "infrastructure_health": 95}
        r = apply_policy("scalper", 95, comps)
        assert r["status"] == "INELIGIBLE"
        assert "hard_floor" in r["reason"]

    def test_sniper_always_eligible(self):
        from strategies.execution_eligibility import apply_policy
        assert apply_policy("sniper", 0, {})["status"] == "ELIGIBLE"


@pytest.mark.unit
class TestSingleModeWeights:
    def test_weights_valid_rule(self):
        from modules.pamm.strategy_assignment import weights_valid
        assert weights_valid(0.0, 1.0, 1.0) is True
        assert weights_valid(0.2, 0.5, 0.8) is True
        assert weights_valid(0.5, 0.4, 0.8) is False  # min > target
        assert weights_valid(0.0, 0.9, 0.8) is False  # target > max
        assert weights_valid(0.0, 1.0, 1.5) is False  # max > 1
        assert weights_valid(-0.1, 0.5, 1.0) is False
        assert weights_valid("x", 1, 1) is False

    def test_single_pins_weights(self):
        from modules.pamm.strategy_assignment import SINGLE_WEIGHTS
        assert SINGLE_WEIGHTS == (0.0, 1.0, 1.0)


@pytest.mark.unit
class TestCanonicalIntentPammLineage:
    def test_payload_carries_pamm_identity_fields(self):
        from execution_intents import canonical_payload
        p = canonical_payload(
            account_id="a", broker_account_number="1",
            broker_server="s", strategy_id="scalper",
            strategy_version="1.0.0", symbol="XAUUSD", side="BUY",
            requested_volume=0.1, stop_loss=None, take_profit=None,
            risk_snapshot_id="", signal_id="", fencing_epoch=0, nonce="",
            pamm_program_id="pgm_x", assignment_id="psa_x",
            strategy_hash="h", risk_profile_id="controlled",
            certification_id="cert_x")
        for k in ("pamm_program_id", "assignment_id", "strategy_hash",
                  "risk_profile_id", "certification_id"):
            assert k in p
        assert p["pamm_program_id"] == "pgm_x"

    def test_pamm_fields_default_empty_for_non_pamm(self):
        from execution_intents import canonical_payload
        p = canonical_payload(
            account_id="a", broker_account_number="1", broker_server="s",
            strategy_id="manual", strategy_version="", symbol="EURUSD",
            side="SELL", requested_volume=0.1, stop_loss=None,
            take_profit=None, risk_snapshot_id="", signal_id="",
            fencing_epoch=0, nonce="")
        assert p["pamm_program_id"] == ""
        assert p["assignment_id"] == ""


@pytest.mark.unit
class TestStrategyOwnershipClassification:
    def _assignment(self):
        return {"status": "ACTIVE", "strategy_id": "scalper",
                "strategy_version": "1.0.0"}

    def test_owned(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_program_id": "p1", "pamm_strategy_id": "scalper",
             "pamm_strategy_version": "1.0.0"}
        assert _classify(t, "p1", self._assignment()) == "OWNED"

    def test_untagged_on_legacy_program_is_fine(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        assert _classify({}, "p1", None) == "LEGACY_UNTAGGED"

    def test_untagged_with_active_assignment_is_unknown_owner(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        assert _classify({}, "p1",
                         self._assignment()) == "PAMM_OWNER_UNKNOWN"

    def test_wrong_program(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_program_id": "OTHER"}
        assert _classify(t, "p1",
                         self._assignment()) == "PAMM_OWNER_MISMATCH"

    def test_strategy_owner_unknown(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_program_id": "p1", "pamm_strategy_id": "sniper"}
        assert _classify(t, "p1",
                         self._assignment()) == "STRATEGY_OWNER_UNKNOWN"

    def test_version_mismatch(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_program_id": "p1", "pamm_strategy_id": "scalper",
             "pamm_strategy_version": "0.9.0"}
        assert _classify(t, "p1",
                         self._assignment()) == "STRATEGY_VERSION_MISMATCH"

    def test_tagged_but_no_assignment(self):
        from modules.pamm.reconciliation.strategy_ownership import _classify
        t = {"pamm_program_id": "p1", "pamm_strategy_id": "scalper"}
        assert _classify(t, "p1", None) == "STRATEGY_OWNER_UNKNOWN"


@pytest.mark.unit
class TestGuardWiring:
    def test_guard_module_exports(self):
        from modules.pamm.strategy_guard import (
            authorize_pamm_strategy_execution, resolve_program)
        assert callable(authorize_pamm_strategy_execution)
        assert callable(resolve_program)

    def test_authority_binds_guard(self):
        import inspect

        import execution_authority
        src = inspect.getsource(execution_authority.submit_intent)
        assert "authorize_pamm_strategy_execution" in src
        assert "pamm_strategy_guard" in src
        # guard must run BEFORE the Global Trading Authority
        assert (src.index("authorize_pamm_strategy_execution")
                < src.index("enforce_new_trade"))

    def test_execution_stamps_pamm_identity(self):
        import inspect

        import execution
        assert callable(execution.stamp_pamm_identity)
        doc, sig = {}, {"_pamm_identity": {
            "pamm_program_id": "p1", "assignment_id": "psa1",
            "strategy_id": "scalper", "strategy_version": "1.0.0",
            "strategy_hash": "h", "risk_profile_id": "controlled",
            "certification_id": "c1"}}
        execution.stamp_pamm_identity(doc, sig)
        assert doc["pamm_program_id"] == "p1"
        assert doc["pamm_strategy_id"] == "scalper"
        assert doc["pamm_certification_id"] == "c1"
        execution.stamp_pamm_identity(d2 := {}, {})
        assert d2 == {}
        src = inspect.getsource(execution)
        assert src.count("stamp_pamm_identity(trade_doc, signal)") == 2

    def test_strictest_limit_never_averages(self):
        from modules.pamm.risk_profiles import strictest_limit
        assert strictest_limit(0.40, 0.30, 0.20) == 0.20
        assert strictest_limit(None, 0.5) == 0.5
        assert strictest_limit(None, None) is None

    def test_ownership_route_in_bola_matrix(self):
        from security_matrix import BOLA_MATRIX
        assert ("GET", "/api/pamm/programs/{program_id}/"
                "strategy-ownership") in BOLA_MATRIX
