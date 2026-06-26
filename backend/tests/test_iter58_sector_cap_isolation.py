"""Tests for iter-58 — per-account sector caps + pre-trade lot fit.

Three changes covered:
  1. Default sector caps raised to realistic leveraged-trading defaults
     (commodity 500%, fx_major 600%, etc.).
  2. Per-account `cfg.sector_caps_pct_override` layers on top of defaults.
  3. `fit_lot_to_sector_cap` pre-trade helper trims (or skips) lot sizes
     that would breach the cap, eliminating the open→auto-deleverage race.
"""
import pytest
from unittest.mock import AsyncMock

from portfolio.risk_manager import (
    SECTOR_CAPS_PCT, _resolve_sector_caps, _sector_exposure, build_snapshot,
)
from portfolio.sector_cap_fit import fit_lot_to_sector_cap


# ============ defaults ============
def test_default_caps_are_realistic_for_leverage():
    """Old defaults (commodity=40%) made a single Kelly-sized XAU position
    blow past the cap on small accounts. New defaults should accommodate
    1-5x leveraged exposure."""
    assert SECTOR_CAPS_PCT["commodity"] >= 300, (
        "commodity cap must allow a Kelly-sized XAU position on small accounts")
    assert SECTOR_CAPS_PCT["fx_major"] >= 300
    assert SECTOR_CAPS_PCT["crypto"] >= 200


# ============ per-account override ============
def test_resolve_sector_caps_no_override():
    assert _resolve_sector_caps(None) is SECTOR_CAPS_PCT
    assert _resolve_sector_caps({}) is SECTOR_CAPS_PCT


def test_resolve_sector_caps_merges_override():
    out = _resolve_sector_caps({"commodity": 800.0, "crypto": 150.0})
    assert out["commodity"] == 800.0
    assert out["crypto"] == 150.0
    # Untouched sectors fall through to default.
    assert out["fx_major"] == SECTOR_CAPS_PCT["fx_major"]


def test_resolve_sector_caps_ignores_bad_values():
    out = _resolve_sector_caps({"commodity": "not-a-number", "crypto": None})
    assert out["commodity"] == SECTOR_CAPS_PCT["commodity"]
    assert out["crypto"] == SECTOR_CAPS_PCT["crypto"]


@pytest.mark.asyncio
async def test_sector_exposure_uses_per_account_cap():
    positions = [
        {"symbol": "XAUUSD", "lot_size": 0.05, "entry_price": 4000.0, "status": "open"},
    ]
    # Default cap (500%): 0.05 × 4000 × 100 = $20k notional on $10k equity = 200% → under cap
    out_default = await _sector_exposure(positions, equity=10_000)
    assert out_default["commodity"]["over_cap"] is False
    # Override to 100%: same trade now over cap
    out_strict = await _sector_exposure(positions, equity=10_000,
                                        caps={**SECTOR_CAPS_PCT, "commodity": 100.0})
    assert out_strict["commodity"]["over_cap"] is True


# ============ sector_cap_fit pre-trade ============
def test_fit_no_existing_positions_within_cap():
    """First trade on an empty book, within cap → pass-through."""
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=[], equity=10_000,
    )
    # 0.05 lot × 4000 × 100 = $20k notional / $10k = 200% (within 500% default)
    assert out["lot"] == 0.05
    assert out["scale"] == 1.0
    assert out["would_breach"] is False
    assert out["post_pct"] == pytest.approx(200.0, abs=1)


def test_fit_trims_when_proposed_would_breach():
    """Existing 0.10 lot ($40k = 400%) + new 0.05 lot ($20k = 200%) = 600% → breach.
    Cap=500%, safety margin 95% → fit to 475% target → trim new lot."""
    existing = [
        {"symbol": "XAUUSD", "lot_size": 0.10, "entry_price": 4000.0, "status": "open"},
    ]
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=existing, equity=10_000,
    )
    assert out["would_breach"] is True
    assert 0 < out["lot"] < 0.05
    assert out["scale"] < 1.0
    # Result should be inside cap × safety margin (475%).
    assert out["post_pct"] <= 500


def test_fit_skips_when_cap_already_saturated():
    """Existing positions ALREADY at or past cap → return lot=0 (skip)."""
    existing = [
        # 0.13 × 4000 × 100 = $52k / $10k = 520% > 500% cap
        {"symbol": "XAUUSD", "lot_size": 0.13, "entry_price": 4000.0, "status": "open"},
    ]
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=existing, equity=10_000,
    )
    assert out["lot"] == 0.0
    assert out["scale"] == 0.0
    assert "already" in out["reason"].lower() or "skipping" in out["reason"].lower()


def test_fit_honours_per_account_override():
    """A stricter per-account commodity cap (150%) forces trim where the
    default 500% would allow the trade."""
    cfg = {"sector_caps_pct_override": {"commodity": 150.0}}
    # 0.05 × 4000 × 100 = $20k / $10k = 200% > 150% cap → must trim.
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=[], equity=10_000, cfg=cfg,
    )
    assert out["cap_pct"] == 150.0
    assert out["would_breach"] is True
    assert out["lot"] < 0.05


def test_fit_ignores_other_sectors():
    """A BTCUSD position should not affect a new XAUUSD trade's cap fit."""
    existing = [
        {"symbol": "BTCUSD", "lot_size": 0.01, "entry_price": 60_000.0, "status": "open"},
    ]
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=existing, equity=10_000,
    )
    # XAU position alone = 200% commodity, BTC doesn't count → fits cleanly
    assert out["lot"] == 0.05
    assert out["scale"] == 1.0


def test_fit_handles_zero_equity_gracefully():
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.05, new_entry_price=4000.0,
        open_positions=[], equity=0,
    )
    assert out["lot"] == 0.05  # equity=0 means no calc — pass-through


def test_fit_handles_zero_lot_gracefully():
    out = fit_lot_to_sector_cap(
        new_symbol="XAUUSD", new_lot=0.0, new_entry_price=4000.0,
        open_positions=[], equity=10_000,
    )
    assert out["lot"] == 0.0


# ============ build_snapshot integration ============
@pytest.mark.asyncio
async def test_build_snapshot_uses_cfg_override(monkeypatch):
    """Per-account snapshot computation must honour cfg.sector_caps_pct_override."""
    from portfolio import risk_manager
    # Stub out non-essential calls so we can probe sector-cap behaviour alone.
    monkeypatch.setattr(risk_manager, "get_drawdown",
        AsyncMock(return_value={"hwm": 10_000, "dd_pct": 0, "dd_usd": 0,
                                "soft_breach": False, "hard_breach": False}))
    monkeypatch.setattr(risk_manager, "calculate_var",
        AsyncMock(return_value={"var_95_usd": 0, "var_95_pct_equity": 0,
                                "var_99_usd": 0, "var_99_pct_equity": 0,
                                "cvar_95_usd": 0, "cvar_95_pct_equity": 0,
                                "cvar_99_usd": 0, "cvar_99_pct_equity": 0,
                                "portfolio_sigma_pct": 0, "horizon_days": 1,
                                "equity": 10_000, "positions": [],
                                "correlation_matrix": {}, "notes": []}))
    monkeypatch.setattr(risk_manager, "_avg_pairwise_corr",
        AsyncMock(return_value=0.0))

    acc = {"_id": "x", "equity": 10_000, "balance": 10_000, "user_id": "u"}
    positions = [
        {"symbol": "XAUUSD", "lot_size": 0.05,
         "entry_price": 4000.0, "status": "open"},
    ]

    # Without override: default 500% cap → not over_cap
    snap_default = await build_snapshot(None, account=acc, open_positions=positions)
    assert snap_default["sectors"]["commodity"]["over_cap"] is False
    assert snap_default["limits"]["sector_caps_pct"]["commodity"] == 500.0

    # With strict 100% override: same position breaches
    cfg = {"sector_caps_pct_override": {"commodity": 100.0}}
    snap_strict = await build_snapshot(None, account=acc, open_positions=positions, cfg=cfg)
    assert snap_strict["sectors"]["commodity"]["over_cap"] is True
    assert snap_strict["limits"]["sector_caps_pct"]["commodity"] == 100.0
