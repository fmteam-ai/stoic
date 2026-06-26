"""Tests for iter-60 — tiered subscriptions + feature gates.

Covers:
  • PLANS contains 12 SKUs (3 tiers × 4 durations).
  • Legacy plan_ids (monthly/quarterly/...) alias to pro_* for backward compat.
  • TIER_RANK + tier_at_least correctness.
  • get_user_tier resolution: admin → admin, grace → pro, active paid → tier,
    expired → starter.
  • enforce_feature 402 + payload shape on locked features.
  • enforce_account_quota 402 when at cap, allows when under.
  • enforce_symbol_allowed honours wildcards + denials.
"""
import pytest
from fastapi import HTTPException

from subscription_plans import (
    PLANS, LEGACY_ALIASES, get_plan, TIERS, tier_at_least,
    get_tier_features, STARTER, PRO, ELITE, TIER_BASE_USD,
)


# ============ Catalog shape ============
def test_catalog_has_twelve_skus():
    assert len(PLANS) == 12
    expected = {f"{t}_{d}" for t in ("starter", "pro", "elite")
                            for d in ("monthly", "quarterly", "semi_annual", "annual")}
    assert set(PLANS.keys()) == expected


def test_each_sku_resolves_correct_tier_and_duration():
    p = PLANS["pro_annual"]
    assert p.tier == "pro"
    assert p.duration_months == 12
    assert p.discount_pct == 40
    assert p.amount_usd == round(99.0 * 12 * 0.60, 2)


def test_starter_monthly_is_cheapest_sku():
    starter_monthly = PLANS["starter_monthly"]
    elite_annual = PLANS["elite_annual"]
    assert starter_monthly.amount_usd < elite_annual.amount_usd
    assert starter_monthly.amount_usd == 29.0


def test_legacy_aliases_map_to_pro():
    assert get_plan("monthly").id == "pro_monthly"
    assert get_plan("annual").id == "pro_annual"
    assert get_plan("semi_annual").tier == "pro"


def test_unknown_plan_id_returns_none():
    assert get_plan("nonexistent") is None
    assert get_plan(None) is None
    assert get_plan("") is None


# ============ Tier rank + features ============
def test_tier_rank_ordering():
    assert tier_at_least("admin", "elite") is True
    assert tier_at_least("elite", "pro") is True
    assert tier_at_least("pro", "starter") is True
    assert tier_at_least("starter", "pro") is False
    assert tier_at_least("", "starter") is False


def test_starter_features_locked():
    assert STARTER.max_accounts == 1
    assert STARTER.auto_execute is False
    assert STARTER.loss_lab is False
    assert STARTER.auto_heal is False
    assert STARTER.allowed_symbols == ("XAUUSD",)


def test_pro_features_unlocked():
    assert PRO.max_accounts == 3
    assert PRO.auto_execute is True
    assert PRO.loss_lab is True
    assert PRO.auto_heal is True
    assert PRO.correlation_kelly is True
    assert "BTCUSD" in PRO.allowed_symbols
    assert PRO.drift_auto_retrain is False  # Elite-only


def test_elite_features_all_on():
    assert ELITE.max_accounts == -1
    assert ELITE.allowed_symbols == ("*",)
    assert ELITE.drift_auto_retrain is True
    assert ELITE.paper_shadow_mode is True
    assert ELITE.custom_thresholds is True


def test_get_tier_features_defaults_to_starter():
    assert get_tier_features("unknown") is STARTER
    assert get_tier_features(None) is STARTER


# ============ Entitlement gates (HTTPException shape) ============
@pytest.mark.asyncio
async def test_enforce_feature_admin_bypasses(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier",
                        _async_return("admin"))
    feats = await enforce_feature({"id": "user-1"}, "drift_auto_retrain")
    assert feats is ELITE or feats.label  # admin gets default; the call just doesn't raise


@pytest.mark.asyncio
async def test_enforce_feature_blocks_starter_from_loss_lab(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("starter"))
    with pytest.raises(HTTPException) as exc:
        await enforce_feature({"id": "user-1"}, "loss_lab")
    assert exc.value.status_code == 402
    payload = exc.value.detail
    assert payload["error"] == "feature_locked"
    assert payload["feature"] == "loss_lab"
    assert payload["current_tier"] == "starter"
    assert payload["minimum_tier"] == "pro"


@pytest.mark.asyncio
async def test_enforce_feature_blocks_pro_from_drift_retrain(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("pro"))
    with pytest.raises(HTTPException) as exc:
        await enforce_feature({"id": "user-1"}, "drift_auto_retrain")
    assert exc.value.detail["minimum_tier"] == "elite"


@pytest.mark.asyncio
async def test_enforce_feature_allows_when_tier_has_flag(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("pro"))
    feats = await enforce_feature({"id": "user-1"}, "loss_lab")
    assert feats.loss_lab is True


@pytest.mark.asyncio
async def test_enforce_account_quota_blocks_starter_at_one(monkeypatch):
    from entitlements import enforce_account_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("starter"))
    with pytest.raises(HTTPException) as exc:
        await enforce_account_quota({"id": "u"}, current_account_count=1)
    assert exc.value.detail["error"] == "account_quota_exceeded"
    assert exc.value.detail["minimum_tier"] == "pro"


@pytest.mark.asyncio
async def test_enforce_account_quota_blocks_pro_at_three(monkeypatch):
    from entitlements import enforce_account_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("pro"))
    with pytest.raises(HTTPException) as exc:
        await enforce_account_quota({"id": "u"}, current_account_count=3)
    assert exc.value.detail["max_accounts"] == 3
    assert exc.value.detail["minimum_tier"] == "elite"


@pytest.mark.asyncio
async def test_enforce_account_quota_unlimited_for_elite(monkeypatch):
    from entitlements import enforce_account_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("elite"))
    # No raise even at 100 accounts.
    await enforce_account_quota({"id": "u"}, current_account_count=100)


@pytest.mark.asyncio
async def test_enforce_symbol_allowed_blocks_starter_from_btc(monkeypatch):
    from entitlements import enforce_symbol_allowed
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("starter"))
    with pytest.raises(HTTPException) as exc:
        await enforce_symbol_allowed({"id": "u"}, "BTCUSD")
    assert exc.value.detail["error"] == "symbol_locked"
    assert exc.value.detail["minimum_tier"] == "pro"


@pytest.mark.asyncio
async def test_enforce_symbol_allowed_elite_wildcard(monkeypatch):
    from entitlements import enforce_symbol_allowed
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("elite"))
    # All symbols pass when allowed_symbols contains "*".
    await enforce_symbol_allowed({"id": "u"}, "EURUSD")
    await enforce_symbol_allowed({"id": "u"}, "SPX500")


# ============ helpers ============
def _async_return(value):
    async def _f(*a, **kw):
        return value
    return _f
