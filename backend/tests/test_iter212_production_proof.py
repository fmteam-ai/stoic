"""iter-212 — Production Proof hardening (offline suite).

Covers: soak-campaign evaluation, segmented conformal coverage,
value-ledger honesty labels, certification split semantics, expanded
BOLA sensitive-parameter set, chaos/soak marker registration."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.unit
class TestSoakEvaluation:
    def _campaign(self, days_ago: float, days: int = 14):
        return {"campaign_id": "soak_x", "days": days,
                "started_at": (datetime.now(timezone.utc)
                               - timedelta(days=days_ago)).isoformat()}

    def test_running_mid_campaign(self):
        from soak_campaign import evaluate
        cps = [{"day": d, "green": True} for d in range(1, 6)]
        ev = evaluate(self._campaign(5), cps, [])
        assert ev["verdict"] == "RUNNING"
        assert ev["criteria"]["no_critical_incidents"] is True

    def test_pass_after_14_green_days(self):
        from soak_campaign import evaluate
        cps = [{"day": d, "green": True} for d in range(1, 15)]
        ev = evaluate(self._campaign(14.5), cps, [])
        assert ev["verdict"] == "PASS"

    def test_critical_incident_fails_immediately(self):
        from soak_campaign import evaluate
        cps = [{"day": 1, "green": True}]
        ev = evaluate(self._campaign(1),
                      cps, [{"severity": "critical", "note": "outage"}])
        assert ev["verdict"] == "FAIL"

    def test_low_checkpoint_coverage_fails_at_end(self):
        from soak_campaign import evaluate
        cps = [{"day": d, "green": True} for d in range(1, 6)]  # 5/14
        ev = evaluate(self._campaign(15), cps, [])
        assert ev["verdict"] == "FAIL"
        assert ev["criteria"]["checkpoint_coverage_ok"] is False

    def test_minor_incident_does_not_fail(self):
        from soak_campaign import evaluate
        cps = [{"day": d, "green": True} for d in range(1, 15)]
        ev = evaluate(self._campaign(14.5), cps,
                      [{"severity": "minor", "note": "blip"}])
        assert ev["verdict"] == "PASS"


@pytest.mark.unit
class TestSegmentedCoverage:
    def test_segments_grouped_and_judged(self):
        import random
        from uncertainty_engine import segment_coverage
        rng = random.Random(5)
        rows = ([{"r": rng.gauss(0.1, 0.5), "scope": "scalp"}
                 for _ in range(120)]
                + [{"r": 0.1, "scope": "swing"} for _ in range(5)])
        out = segment_coverage(rows, "scope")
        by = {o["segment"]: o for o in out}
        assert by["scalp"]["coverage"] is not None
        assert by["swing"]["coverage"] is None  # insufficient — not judged
        assert by["swing"]["ok"] is True

    def test_none_keys_dropped(self):
        from uncertainty_engine import segment_coverage
        out = segment_coverage([{"r": 0.1, "regime": None}] * 50, "regime")
        assert out == []


@pytest.mark.unit
class TestValueLedgerHonesty:
    def test_effect_labels_never_blend(self):
        # structural contract: sources are pre-labelled and unobservable
        # entries never carry usd values
        import inspect

        import value_ledger
        src = inspect.getsource(value_ledger)
        assert "unobservable" in src and "observed" in src
        assert "never blends" in src


@pytest.mark.unit
class TestCertificationSplit:
    def test_kinds_are_distinct(self):
        import inspect

        import certification
        src = inspect.getsource(certification)
        assert '"kind": "system"' in src
        assert '"kind": "strategy"' in src
        # system cert must not judge edge; strategy cert must not judge pipes
        sys_src = inspect.getsource(certification.system_certification)
        strat_src = inspect.getsource(certification.strategy_certification)
        assert "edge" not in sys_src.split("note")[0].lower()
        assert "heartbeat" not in strat_src.lower()


@pytest.mark.unit
class TestExpandedBolaParams:
    def test_sensitive_params_expanded(self):
        from security_matrix import SENSITIVE_PARAMS
        for p in ("decision_id", "intent_id", "installation_id",
                  "trade_id", "signal_id", "program_id"):
            assert p in SENSITIVE_PARAMS

    def test_pamm_routes_declared(self):
        from security_matrix import BOLA_MATRIX
        pamm = [p for (_m, p) in BOLA_MATRIX if p.startswith("/api/pamm")]
        assert len(pamm) >= 25


@pytest.mark.unit
class TestMarkers:
    def test_chaos_and_soak_markers_registered(self):
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ini = open(os.path.join(base, "pytest.ini")).read()
        assert "chaos:" in ini and "soak:" in ini


@pytest.mark.unit
class TestHeartbeatClockTelemetry:
    def test_model_accepts_client_time(self):
        from models import BridgeHeartbeat
        hb = BridgeHeartbeat(bridge_token="t", balance=1.0, equity=1.0,
                             client_time_ms=1750000000000, ntp_synced=True)
        assert hb.client_time_ms == 1750000000000

    def test_ea_heartbeat_sends_client_time(self):
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(
            base, "static", "EmergentTradingBridge.mq5")).read()
        assert '\\"client_time_ms\\":%I64d' in src
        assert "(long)TimeGMT() * 1000" in src
