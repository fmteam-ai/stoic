"""Audit r28 P2-05 — shared chart provenance contract."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_contract_fields_freshness_gaps_and_kinds(monkeypatch):
    import chart_provenance as cp
    monkeypatch.setenv("APP_ENV", "production")
    now = datetime.now(timezone.utc)
    pts = [{"date": (now - timedelta(days=d)).isoformat()} for d in (6, 5, 4, 1, 0)]   # gap: 4 → 1 (2 missing days)
    p = cp.build(provider="broker_deals", source_kind="broker_reconciled", points=pts, expected_interval_s=86400,
                 reconciliation_id="rec-1", ledger_id="led-1")
    assert p["contract_version"] == 1 and p["provider"] == "broker_deals" and p["source_kind"] == "broker_reconciled"
    assert p["timezone"] == "UTC" and p["environment"] == "production" and p["points"] == 5
    assert p["freshness_s"] < 5 and p["stale"] is False
    assert p["missing_intervals_count"] == 1 and p["missing_intervals"][0]["missing"] == 2
    assert p["ledger_id"] == "led-1" and p["reconciliation_id"] == "rec-1"
    stale = cp.build(provider="x", source_kind="indicative", points=[{"date": (now - timedelta(days=5)).isoformat()}])
    assert stale["stale"] is True and stale["cache_status"] == "live"
    empty = cp.build(provider="x", source_kind="simulated", points=[])
    assert empty["as_of"] is None and empty["freshness_s"] is None and empty["stale"] is False
    with pytest.raises(AssertionError):
        cp.build(provider="x", source_kind="guess", points=[])
    # epoch-ms and epoch-s timestamps are accepted
    ms = cp.build(provider="x", source_kind="derived", points=[{"date": int(now.timestamp() * 1000)}])
    assert ms["points"] == 1 and ms["freshness_s"] < 5


def test_every_chart_endpoint_attaches_the_contract():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    for rel in ("routes/market_routes.py", "routes/account_routes.py", "routes/performance_routes.py",
                "modules/pamm/api/__init__.py", "routes/analytics_routes.py"):
        src = open(os.path.join(root, rel)).read()
        assert "from chart_provenance import build as provenance" in src, rel
        assert '"provenance": provenance(' in src, rel
    ui = open(os.path.join(root, "..", "frontend", "src", "components", "ChartProvenance.jsx")).read()
    for kind in ("broker_reconciled", "indicative", "simulated", "derived"):
        assert kind in ui   # the four kinds are visually distinct in the UI
