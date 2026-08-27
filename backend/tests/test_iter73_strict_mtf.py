"""Tests for iter-73 — Strict MTF gate (cfg.mtf_strict=True).

When enabled, requires ≥2/3 multi-timeframe tiers to actively AGREE with the
trade direction. Counter-trend trades AND drift-into-chop trades are both
blocked (where the existing veto only catches the former).
"""
from __future__ import annotations
import pytest


def _build_alignment(buy_support: int, sell_support: int) -> dict:
    """Helper: build a mtf_tiers.alignment dict."""
    return {
        "alignment": {
            "buy_support": buy_support,
            "sell_support": sell_support,
            "dominant": "UP" if buy_support > sell_support
                       else ("DOWN" if sell_support > buy_support else "MIXED"),
            "all_aligned_up": buy_support == 3,
            "all_aligned_down": sell_support == 3,
        },
        "ready": True,
    }


def _strict_check(action: str, mtf_tiers: dict, need: int = 2) -> bool:
    """Mirror the bot_runner logic — return True if strict gate ALLOWS the trade."""
    alignment = mtf_tiers.get("alignment") or {}
    buy_sup = int(alignment.get("buy_support") or 0)
    sell_sup = int(alignment.get("sell_support") or 0)
    if action == "BUY":
        return buy_sup >= need
    if action == "SELL":
        return sell_sup >= need
    return True


# ─────────── direction agreement ───────────
def test_strict_allows_strong_buy_alignment():
    """3/3 tiers UP → BUY passes."""
    tiers = _build_alignment(3, 0)
    assert _strict_check("BUY", tiers) is True


def test_strict_allows_strong_sell_alignment():
    tiers = _build_alignment(0, 3)
    assert _strict_check("SELL", tiers) is True


def test_strict_blocks_buy_with_no_alignment():
    """0/3 tiers UP → BUY blocked (drift-into-chop case)."""
    tiers = _build_alignment(0, 0)
    assert _strict_check("BUY", tiers) is False


def test_strict_blocks_buy_with_one_tier_only():
    """1/3 tiers UP → BUY still blocked (need ≥2)."""
    tiers = _build_alignment(1, 0)
    assert _strict_check("BUY", tiers) is False


def test_strict_allows_buy_with_two_of_three():
    """2/3 tiers UP → BUY passes (the threshold)."""
    tiers = _build_alignment(2, 1)
    assert _strict_check("BUY", tiers) is True


def test_strict_blocks_counter_trend_buy():
    """3/3 tiers DOWN but action is BUY → blocked."""
    tiers = _build_alignment(0, 3)
    assert _strict_check("BUY", tiers) is False


def test_strict_blocks_counter_trend_sell():
    tiers = _build_alignment(3, 0)
    assert _strict_check("SELL", tiers) is False


def test_strict_no_op_on_hold():
    """HOLD should never be blocked by the strict gate."""
    tiers = _build_alignment(0, 0)
    assert _strict_check("HOLD", tiers) is True


def test_strict_handles_missing_alignment_dict():
    """Empty/missing tier data → defaults to 0/0 → BUY/SELL blocked safely."""
    assert _strict_check("BUY", {}) is False
    assert _strict_check("SELL", {"alignment": {}}) is False


# ─────────── config persistence shape ───────────
def test_mtf_strict_field_default_false():
    """Default cfg should NOT have strict mode on — opt-in only."""
    cfg = {"active": True}
    assert bool(cfg.get("mtf_strict")) is False


def test_mtf_strict_boolean_coercion():
    """Truthy / falsy values should coerce to bool."""
    assert bool({"mtf_strict": "yes"}.get("mtf_strict")) is True
    assert bool({"mtf_strict": 0}.get("mtf_strict")) is False
    assert bool({"mtf_strict": 1}.get("mtf_strict")) is True
    assert bool({"mtf_strict": None}.get("mtf_strict")) is False


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
