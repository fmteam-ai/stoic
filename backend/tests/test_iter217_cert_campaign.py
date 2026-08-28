"""v62.2 — PAMM × Strategy certification campaigns (offline suite)."""
import pytest


@pytest.mark.unit
class TestLifecycleWiring:
    def test_next_state_map_respects_transition_table(self):
        from strategies.certification import transition_allowed
        from strategies.certification_campaign import NEXT_STATE
        for cur, nxt in NEXT_STATE.items():
            assert transition_allowed(cur, nxt), (cur, nxt)

    def test_every_evidence_stage_has_criteria(self):
        from strategies.certification_campaign import (EVIDENCE_STAGES,
                                                       STAGE_CRITERIA)
        assert set(STAGE_CRITERIA) == set(EVIDENCE_STAGES)

    def test_pipeline_order_replay_shadow_demo_canary(self):
        from strategies.certification_campaign import (EVIDENCE_STAGES,
                                                       NEXT_STATE)
        assert EVIDENCE_STAGES == ["REPLAY", "SHADOW", "DEMO", "CANARY"]
        assert NEXT_STATE["VALIDATING"] == "REPLAY"
        assert NEXT_STATE["CANARY"] == "CERTIFIED"
        assert NEXT_STATE["CERTIFIED"] == "LIVE"

    def test_revoked_is_terminal(self):
        from strategies.certification import transition_allowed
        for target in ("DRAFT", "LIVE", "CERTIFIED"):
            assert not transition_allowed("REVOKED", target)


@pytest.mark.unit
class TestReplayGate:
    def _good(self):
        return {"decisions_replayed": 250, "determinism_ok": True,
                "expectancy_lower_r": 0.05, "risk_violations": 0}

    def test_passes_on_good_metrics(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("REPLAY", self._good())
        assert r["passed"] is True
        assert len(r["checks"]) == 4

    def test_fails_below_min_decisions(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("REPLAY", {**self._good(),
                                      "decisions_replayed": 199})
        assert r["passed"] is False

    def test_fails_without_determinism(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("REPLAY", {**self._good(),
                                      "determinism_ok": False})
        assert r["passed"] is False

    def test_fails_on_nonpositive_lower_bound(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("REPLAY", {**self._good(),
                                      "expectancy_lower_r": 0.0})
        assert r["passed"] is False

    def test_missing_evidence_fails_never_assumed(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("REPLAY", {})
        assert r["passed"] is False
        assert all(not c["ok"] for c in r["checks"])


@pytest.mark.unit
class TestShadowGate:
    def _good(self):
        return {"days_elapsed": 6, "shadow_decisions": 150,
                "agreement_rate": 0.97, "risk_violations": 0,
                "orders_placed": 0}

    def test_passes(self):
        from strategies.certification_campaign import evaluate_stage
        assert evaluate_stage("SHADOW", self._good())["passed"] is True

    def test_shadow_never_trades(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("SHADOW", {**self._good(), "orders_placed": 1})
        assert r["passed"] is False
        bad = [c for c in r["checks"] if not c["ok"]]
        assert bad[0]["key"] == "no_orders_placed"

    def test_low_agreement_fails(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("SHADOW", {**self._good(),
                                      "agreement_rate": 0.90})
        assert r["passed"] is False


@pytest.mark.unit
class TestDemoGate:
    def _good(self):
        return {"environment": "DEMO", "days_elapsed": 11,
                "closed_trades": 60, "unknown_rate": 0.0,
                "reject_rate": 0.01, "max_drawdown_pct": 4.0,
                "expectancy_r": 0.12}

    def test_passes(self):
        from strategies.certification_campaign import evaluate_stage
        assert evaluate_stage("DEMO", self._good())["passed"] is True

    def test_wrong_environment_fails(self):
        from strategies.certification_campaign import evaluate_stage
        for env in ("LIVE", "PAPER", None):
            r = evaluate_stage("DEMO", {**self._good(),
                                        "environment": env})
            assert r["passed"] is False, env

    def test_unknown_rate_bound(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("DEMO", {**self._good(), "unknown_rate": 0.03})
        assert r["passed"] is False


@pytest.mark.unit
class TestCanaryGate:
    def _good(self):
        return {"environment": "LIVE", "canary_capital_pct": 3.0,
                "days_elapsed": 6, "closed_trades": 25,
                "max_drawdown_pct": 1.0, "critical_incidents": 0,
                "execution_health": "GREEN"}

    def test_passes(self):
        from strategies.certification_campaign import evaluate_stage
        assert evaluate_stage("CANARY", self._good())["passed"] is True

    def test_capital_cap_enforced(self):
        from strategies.certification_campaign import (
            CANARY_MAX_OPEN_RISK_PCT, evaluate_stage)
        r = evaluate_stage("CANARY", {**self._good(),
                                      "canary_capital_pct":
                                      CANARY_MAX_OPEN_RISK_PCT + 0.1})
        assert r["passed"] is False

    def test_open_risk_metric_name_supported(self):
        from strategies.certification_campaign import (
            CANARY_MAX_OPEN_RISK_PCT, evaluate_stage)
        m = {k: v for k, v in self._good().items()
             if k != "canary_capital_pct"}
        m["canary_open_risk_pct"] = CANARY_MAX_OPEN_RISK_PCT + 0.1
        assert evaluate_stage("CANARY", m)["passed"] is False
        m["canary_open_risk_pct"] = 3.0
        assert evaluate_stage("CANARY", m)["passed"] is True

    def test_drawdown_hard_limit(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("CANARY", {**self._good(),
                                      "max_drawdown_pct": 2.5})
        assert r["passed"] is False

    def test_red_execution_health_fails(self):
        from strategies.certification_campaign import evaluate_stage
        for h in ("RED", None):
            r = evaluate_stage("CANARY", {**self._good(),
                                          "execution_health": h})
            assert r["passed"] is False, h

    def test_critical_incident_fails(self):
        from strategies.certification_campaign import evaluate_stage
        r = evaluate_stage("CANARY", {**self._good(),
                                      "critical_incidents": 1})
        assert r["passed"] is False


@pytest.mark.unit
class TestUnknownStage:
    def test_unknown_stage_never_passes(self):
        from strategies.certification_campaign import evaluate_stage
        assert evaluate_stage("CERTIFIED", {"anything": 1})["passed"] is False
        assert evaluate_stage("BOGUS", {})["passed"] is False


@pytest.mark.unit
class TestCertificationEnforcement:
    def test_require_certification_flag_defaults_on(self, monkeypatch):
        monkeypatch.delenv("PAMM_REQUIRE_CERTIFICATION", raising=False)
        from modules.pamm.strategy_assignment import feature_flags
        assert feature_flags()["PAMM_REQUIRE_CERTIFICATION"] is True

    def test_flag_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("PAMM_REQUIRE_CERTIFICATION", "false")
        from modules.pamm.strategy_assignment import feature_flags
        assert feature_flags()["PAMM_REQUIRE_CERTIFICATION"] is False

    def test_cert_events_registered(self):
        from modules.pamm.events import EVENT_TYPES
        for t in ("PAMM_CERT_CAMPAIGN_STARTED", "PAMM_CERT_CHECKPOINT",
                  "PAMM_CERT_STAGE_ADVANCED", "PAMM_STRATEGY_CERTIFIED",
                  "PAMM_STRATEGY_REVOKED"):
            assert t in EVENT_TYPES

    def test_routes_declared_in_bola_matrix(self):
        from security_matrix import BOLA_MATRIX
        base = "/api/pamm/programs/{program_id}/certification"
        assert ("GET", base) in BOLA_MATRIX
        for action in ("start", "checkpoint", "evaluate", "advance",
                       "revoke"):
            assert ("POST", f"{base}/{action}") in BOLA_MATRIX, action


@pytest.mark.unit
class TestEvidenceChain:
    def test_chain_hash_reuses_soak_primitives(self):
        from soak_campaign import evidence_hash, verify_chain
        r1 = {"campaign_id": "scc_x", "seq": 1, "kind": "checkpoint",
              "payload": {"stage": "REPLAY"}, "prev_hash": "genesis"}
        r1["hash"] = evidence_hash(r1, "genesis")
        r2 = {"campaign_id": "scc_x", "seq": 2, "kind": "evaluation",
              "payload": {"passed": True}, "prev_hash": r1["hash"]}
        r2["hash"] = evidence_hash(r2, r1["hash"])
        assert verify_chain([r1, r2]) is True
        r1["payload"]["stage"] = "CANARY"  # tamper
        assert verify_chain([r1, r2]) is False
