"""Unit — PAMM Investor Mirroring share math (pure, no DB)."""
from modules.pamm.investor_view import investor_share, redact_program


NAVS = [{"at": "2026-01-01T00:00:00", "nav": 100000.0},
        {"at": "2026-02-01T00:00:00", "nav": 110000.0},
        {"at": "2026-03-01T00:00:00", "nav": 121000.0}]


def test_single_allocation_tracks_master_nav():
    s = investor_share([{"amount": 10000.0, "at": "2026-01-15T00:00:00"}],
                       NAVS, 121000.0)
    # bought 10% of a 100k program → worth 12.1k at 121k NAV
    assert s["share_pct"] == 10.0
    assert s["estimated_value"] == 12100.0
    assert s["mirrored_pnl"] == 2100.0
    assert s["mirrored_return_pct"] == 21.0
    assert s["estimated"] is True


def test_multiple_allocations_price_at_their_own_nav():
    s = investor_share([{"amount": 10000.0, "at": "2026-01-15T00:00:00"},
                        {"amount": 11000.0, "at": "2026-02-15T00:00:00"}],
                       NAVS, 121000.0)
    # 10% + 10% = 20% of program → 24.2k on 21k contributed
    assert s["share_pct"] == 20.0
    assert s["contributed"] == 21000.0
    assert s["estimated_value"] == 24200.0
    assert s["mirrored_pnl"] == 3200.0
    assert s["priced_allocations"] == 2


def test_allocation_before_first_snapshot_uses_first_nav():
    s = investor_share([{"amount": 5000.0, "at": "2025-12-01T00:00:00"}],
                       NAVS, 121000.0)
    assert s["share_pct"] == 5.0


def test_no_allocations_yields_nulls_not_zeros():
    s = investor_share([], NAVS, 121000.0)
    assert s["contributed"] == 0.0
    assert s["share_pct"] is None
    assert s["estimated_value"] is None
    assert s["mirrored_pnl"] is None


def test_no_nav_at_all_never_fabricates_value():
    s = investor_share([{"amount": 5000.0, "at": "2026-01-15T00:00:00"}],
                       [], None)
    assert s["contributed"] == 5000.0
    assert s["estimated_value"] is None
    assert s["priced_allocations"] == 0


def test_redact_program_hides_governance_and_limits():
    p = {"program_id": "pgm_x", "name": "Alpha", "risk_limits": {"x": 1},
         "governance": {"secret": 1}, "manager_id": "u1",
         "risk_breach": {"kind": "dd"}, "position_truth": {"status": "in_sync"}}
    r = redact_program(p)
    assert r["program_id"] == "pgm_x" and r["name"] == "Alpha"
    assert "risk_limits" not in r and "governance" not in r
    assert "manager_id" not in r
    assert r["risk_breach"] is True
    assert r["position_truth_status"] == "in_sync"
    assert r["op_state"] == "running"


def test_attestation_environment_requires_verified_identity():
    """Audit #4 P3 — ambiguous/heuristic LIVE never backs a public claim."""
    from routes.performance_routes import _attestation_environment as env
    live = {"mode": "live", "server": "Broker-Real7",
            "verified_identity": {"account_number": "1"}}
    assert env(live) == "LIVE"
    assert env({"mode": "live", "server": "Broker-Real7"}) == "UNKNOWN"
    assert env({"mode": "paper"}) == "PAPER"
    assert env({"server": "Broker-Demo", "verified_identity": {}}) == "DEMO"
    assert env({"account_type": "demo",
                "verified_identity": {"a": 1}}) == "DEMO"
