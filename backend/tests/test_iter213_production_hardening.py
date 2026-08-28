"""iter-213 — Production-Proof hardening pass (offline suite)."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.unit
class TestBrokerEnvironments:
    def test_paper(self):
        from broker_env import broker_environment
        assert broker_environment({"mode": "paper"}) == "PAPER"

    def test_demo_server_detected(self):
        from broker_env import broker_environment
        assert broker_environment(
            {"broker_server": "ICMarkets-Demo02"}) == "DEMO"

    def test_live_default(self):
        from broker_env import broker_environment
        assert broker_environment(
            {"broker_server": "Exness-Real7"}) == "LIVE"

    def test_explicit_field_wins(self):
        from broker_env import broker_environment
        assert broker_environment(
            {"broker_environment": "DEMO",
             "broker_server": "Exness-Real7"}) == "DEMO"


@pytest.mark.unit
class TestStrategyTiers:
    def test_tier_a_requires_lower_bound_and_health(self):
        from certification import strategy_tier
        assert strategy_tier(150, 0.3, 0.1, True, "HEALTHY") == "CERTIFIED_A"
        assert strategy_tier(150, 0.3, -0.05, True, "HEALTHY") == "CERTIFIED_B"
        assert strategy_tier(150, 0.3, 0.1, True, "WATCH") == "CERTIFIED_B"

    def test_tier_b_and_provisional(self):
        from certification import strategy_tier
        assert strategy_tier(40, 0.2, -0.1, True, "WATCH") == "CERTIFIED_B"
        assert strategy_tier(40, 0.2, -0.1, False, "WATCH") == "PROVISIONAL"
        assert strategy_tier(40, 0.2, -0.1, True, "DEGRADED") == "PROVISIONAL"

    def test_uncertified(self):
        from certification import strategy_tier
        assert strategy_tier(10, 0.5, 0.2, True, "HEALTHY") == "UNCERTIFIED"
        assert strategy_tier(50, -0.1, -0.3, True, "HEALTHY") == "UNCERTIFIED"


@pytest.mark.unit
class TestCertValidity:
    def _cert(self, **kw):
        base = {"passed": True, "revoked": False,
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(days=1)).isoformat()}
        return {**base, **kw}

    def test_valid(self):
        from certification import cert_validity
        assert cert_validity(self._cert())["valid"] is True

    def test_expired(self):
        from certification import cert_validity
        v = cert_validity(self._cert(
            expires_at=(datetime.now(timezone.utc)
                        - timedelta(days=1)).isoformat()))
        assert v["expired"] is True and v["valid"] is False

    def test_revoked(self):
        from certification import cert_validity
        assert cert_validity(self._cert(revoked=True))["valid"] is False

    def test_failed_at_issue_never_valid(self):
        from certification import cert_validity
        assert cert_validity(self._cert(passed=False))["valid"] is False


@pytest.mark.unit
class TestSoakHardening:
    def _campaign(self, days_ago, days=14):
        return {"campaign_id": "soak_x", "days": days,
                "started_at": (datetime.now(timezone.utc)
                               - timedelta(days=days_ago)).isoformat()}

    def test_coverage_requirement_is_full(self):
        from soak_campaign import MIN_CHECKPOINT_COVERAGE, evaluate
        assert MIN_CHECKPOINT_COVERAGE == 1.0
        cps = [{"day": d, "green": True} for d in range(1, 14)]  # 13/14
        ev = evaluate(self._campaign(14.5), cps, [])
        assert ev["verdict"] == "FAIL"
        assert ev["criteria"]["checkpoint_coverage_ok"] is False

    def test_major_incident_budget(self):
        from soak_campaign import evaluate
        cps = [{"day": d, "green": True} for d in range(1, 15)]
        majors = [{"severity": "major", "note": str(i)} for i in range(3)]
        ev = evaluate(self._campaign(14.5), cps, majors)
        assert ev["verdict"] == "FAIL"
        assert ev["criteria"]["major_incidents_within_budget"] is False
        ev2 = evaluate(self._campaign(14.5), cps, majors[:2])
        assert ev2["verdict"] == "PASS"

    def test_severity_taxonomy_defined(self):
        from soak_campaign import SEVERITIES
        assert set(SEVERITIES) == {"critical", "major", "minor"}
        assert "duplicate" in SEVERITIES["critical"]

    def test_version_drift_pure(self):
        from soak_campaign import version_drift
        assert version_drift({"ea_version": "1.56", "release": "a"},
                             {"ea_version": "1.56", "release": "a"}) == []
        assert version_drift({"ea_version": "1.56"},
                             {"ea_version": "1.57"}) == ["ea_version"]

    def test_release_fingerprint_stable(self):
        from soak_campaign import release_fingerprint
        assert release_fingerprint() == release_fingerprint()
        assert len(release_fingerprint()) >= 10


@pytest.mark.unit
class TestEvidenceChain:
    def test_chain_verifies_and_detects_tampering(self):
        from soak_campaign import evidence_hash, verify_chain
        r1 = {"campaign_id": "c", "seq": 1, "day": 1,
              "checkpoint": {"green": True}, "prev_hash": "genesis"}
        r1["hash"] = evidence_hash(r1, "genesis")
        r2 = {"campaign_id": "c", "seq": 2, "day": 2,
              "checkpoint": {"green": True}, "prev_hash": r1["hash"]}
        r2["hash"] = evidence_hash(r2, r1["hash"])
        assert verify_chain([r1, r2]) is True
        tampered = dict(r1, checkpoint={"green": False})
        assert verify_chain([tampered, r2]) is False

    def test_empty_chain_valid(self):
        from soak_campaign import verify_chain
        assert verify_chain([]) is True


@pytest.mark.unit
class TestMarkerIsolationPolicy:
    def test_every_test_file_declares_a_suite_marker(self):
        import os
        import re
        suite = ("unit", "integration", "http", "broker", "external",
                 "chaos", "soak")
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tests")
        missing = []
        for f in sorted(os.listdir(base)):
            if not (f.startswith("test_") and f.endswith(".py")):
                continue
            src = open(os.path.join(base, f)).read()
            if not re.search(
                    r"pytest\.mark\.(" + "|".join(suite) + r")\b", src):
                missing.append(f)
        assert not missing, (
            f"test files without a suite marker (add pytestmark = "
            f"pytest.mark.<unit|integration|http|...>): {missing}")
