"""v62.1 — PAMM Strategy Profiles foundation (offline suite)."""
import pytest


@pytest.mark.unit
class TestStrategyRegistry:
    def test_four_strategies_registered(self):
        from strategies.registry import REGISTRY
        assert set(REGISTRY) == {"sniper", "scalper", "fast_scalp",
                                 "nitro_scalper"}

    def test_all_pamm_eligible_and_enabled(self):
        from strategies.registry import pamm_eligible_strategies
        assert len(pamm_eligible_strategies()) == 4

    def test_genuinely_different_characteristics(self):
        # not aliases for risk levels — characteristics must differ
        from strategies.registry import REGISTRY
        sigs = {tuple(sorted(d.characteristics.items(),
                             key=lambda x: x[0]))
                and (d.characteristics.get("frequency"),
                     d.characteristics.get("latency_sensitivity"),
                     d.characteristics.get("holding_time"))
                for d in REGISTRY.values()}
        assert len(sigs) == 4
        gate_sets = {tuple(d.gates) for d in REGISTRY.values()}
        assert len(gate_sets) == 4

    def test_nitro_means_selectivity_not_risk(self):
        from strategies.registry import get_strategy
        n = get_strategy("nitro_scalper")
        assert n.characteristics["selectivity"] == "MAXIMUM"
        assert "NEVER maximum risk" in n.characteristics["meaning"]
        assert n.characteristics["on_condition_failure"] == "NO_NITRO"
        assert "nitro_eligibility" == n.gates[0]

    def test_magic_namespace_deterministic_and_unique(self):
        from strategies.registry import REGISTRY
        codes = [d.magic_code for d in REGISTRY.values()]
        assert len(set(codes)) == 4
        assert all(62000 < c < 62100 for c in codes)

    def test_strategy_hash_stable(self):
        from strategies.registry import get_strategy, strategy_hash
        d = get_strategy("sniper")
        assert strategy_hash(d) == strategy_hash(d)
        assert len(strategy_hash(d)) == 32


@pytest.mark.unit
class TestVersionPinning:
    def test_pin_valid_for_current(self):
        from strategies.registry import get_strategy, strategy_hash
        from strategies.versions import version_pin_valid
        d = get_strategy("scalper")
        assert version_pin_valid("scalper", d.version,
                                 strategy_hash(d))["ok"] is True

    def test_version_drift_detected(self):
        from strategies.registry import get_strategy, strategy_hash
        from strategies.versions import version_pin_valid
        d = get_strategy("scalper")
        out = version_pin_valid("scalper", "0.9.9", strategy_hash(d))
        assert out["ok"] is False and out["reason"] == "version_drift"

    def test_hash_drift_detected(self):
        from strategies.registry import get_strategy
        from strategies.versions import version_pin_valid
        d = get_strategy("scalper")
        out = version_pin_valid("scalper", d.version, "tampered")
        assert out["ok"] is False and out["reason"] == "hash_drift"


@pytest.mark.unit
class TestRiskHierarchy:
    def test_strictest_limit_never_averages(self):
        from modules.pamm.risk_profiles import strictest_limit
        # Nitro 0.40 / Portfolio 0.30 / PAMM 0.20 → 0.20
        assert strictest_limit(0.40, 0.30, 0.20) == 0.20
        assert strictest_limit(None, 0.5, None) == 0.5
        assert strictest_limit() is None

    def test_default_profiles_are_distinct(self):
        from modules.pamm.risk_profiles import DEFAULT_PROFILES
        ids = {p["risk_profile_id"] for p in DEFAULT_PROFILES}
        assert ids == {"conservative", "controlled", "growth"}
        risks = [p["max_risk_per_trade"] for p in DEFAULT_PROFILES]
        assert risks == sorted(risks)


@pytest.mark.unit
class TestNitroEligibility:
    def _full(self, v):
        from strategies.nitro.config import WEIGHTS
        return {k: v for k in WEIGHTS}

    def test_perfect_conditions_enabled(self):
        from strategies.nitro.eligibility import compute_score
        out = compute_score(self._full(95))
        assert out["status"] == "NITRO_ENABLED"

    def test_mid_conditions_reduced(self):
        from strategies.nitro.eligibility import compute_score
        out = compute_score(self._full(85))
        assert out["status"] == "NITRO_REDUCED"

    def test_poor_conditions_paused(self):
        from strategies.nitro.eligibility import compute_score
        out = compute_score(self._full(60))
        assert out["status"] == "NITRO_PAUSED"

    def test_hard_floor_overrides_good_average(self):
        # one broken execution leg disqualifies even with a high score
        from strategies.nitro.eligibility import compute_score
        comps = self._full(100)
        comps["latency_quality"] = 40
        out = compute_score(comps)
        assert out["status"] == "NITRO_PAUSED"

    def test_missing_evidence_scores_low(self):
        from strategies.nitro.eligibility import compute_score
        out = compute_score({})
        assert out["status"] == "NITRO_PAUSED"


@pytest.mark.unit
class TestCertificationLifecycle:
    def test_lifecycle_transitions(self):
        from strategies.certification import transition_allowed
        assert transition_allowed("DRAFT", "VALIDATING")
        assert transition_allowed("CERTIFIED", "LIVE")
        assert transition_allowed("LIVE", "SUSPENDED")
        assert not transition_allowed("DRAFT", "LIVE")
        assert not transition_allowed("REVOKED", "LIVE")

    def test_identity_is_the_full_combination(self):
        from strategies.certification import cert_identity
        a = cert_identity("p1", "nitro_scalper", "1.0.0", "BrokerA",
                          "BrokerA-Live02", "controlled", "LIVE")
        b = cert_identity("p1", "nitro_scalper", "1.0.0", "BrokerA",
                          "BrokerA-Demo01", "controlled", "LIVE")
        assert a["identity_hash"] != b["identity_hash"]

    def test_auto_suspend_triggers_defined(self):
        from strategies.certification import AUTO_SUSPEND_TRIGGERS
        for t in ("strategy_disabled", "strategy_version_changed",
                  "clock_unhealthy_for_nitro"):
            assert t in AUTO_SUSPEND_TRIGGERS


@pytest.mark.unit
class TestFeatureFlags:
    def test_defaults_ship_safe(self, monkeypatch):
        for k in ("PAMM_STRATEGY_ASSIGNMENT", "PAMM_MULTI_STRATEGY",
                  "PAMM_DYNAMIC_AI", "PAMM_NITRO_LIVE"):
            monkeypatch.delenv(k, raising=False)
        from modules.pamm.strategy_assignment import feature_flags
        f = feature_flags()
        assert f["PAMM_STRATEGY_ASSIGNMENT"] is True
        assert f["PAMM_MULTI_STRATEGY"] is False
        assert f["PAMM_DYNAMIC_AI"] is False
        assert f["PAMM_NITRO_LIVE"] is False
