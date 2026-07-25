"""Tests for the 4-tier subscription catalog + feature gates (iter-120).

Covers:
  • PLANS contains 16 SKUs (4 tiers × 4 durations) at the new base prices.
  • Legacy plan_ids alias forward (monthly/pro_* → trader_*, elite_* → professional_*).
  • TIER_RANK + tier_at_least correctness incl. legacy tier names.
  • Per-tier feature matrices (Starter/Trader/Professional/Elite AI).
  • enforce_feature 402 + payload shape on locked features.
  • enforce_account_quota 402 at the 1/3/10/50 caps.
  • enforce_mode_ceiling — plan ladder ceiling on operational modes.
  • enforce_vps_quota — multi-VPS locked below Elite AI.
"""
import pytest
from fastapi import HTTPException

from subscription_plans import (
    PLANS, LEGACY_ALIASES, get_plan, TIERS, tier_at_least,
    get_tier_features, STARTER, TRADER, PROFESSIONAL, ELITE_AI,
    TIER_BASE_USD,
)


# ============ Catalog shape ============
def test_catalog_has_sixteen_skus():
    assert len(PLANS) == 16
    expected = {f"{t}_{d}" for t in ("starter", "trader", "professional", "elite_ai")
                            for d in ("monthly", "quarterly", "semi_annual", "annual")}
    assert set(PLANS.keys()) == expected


def test_each_sku_resolves_correct_tier_and_duration():
    p = PLANS["trader_annual"]
    assert p.tier == "trader"
    assert p.duration_months == 12
    assert p.discount_pct == 40
    assert p.amount_usd == round(99.0 * 12 * 0.60, 2)
    e = PLANS["elite_ai_monthly"]
    assert e.amount_usd == 399.0 and e.tier == "elite_ai"


def test_starter_monthly_is_cheapest_sku():
    starter_monthly = PLANS["starter_monthly"]
    elite_annual = PLANS["elite_ai_annual"]
    assert starter_monthly.amount_usd < elite_annual.amount_usd
    assert starter_monthly.amount_usd == 39.0
    assert TIER_BASE_USD == {"starter": 39.0, "trader": 99.0,
                             "professional": 199.0, "elite_ai": 399.0}


def test_legacy_aliases_map_forward():
    # Pre-iter-60 singles and iter-60 pro_* → Trader (price-equivalent)
    assert get_plan("monthly").id == "trader_monthly"
    assert get_plan("annual").id == "trader_annual"
    assert get_plan("pro_monthly").id == "trader_monthly"
    assert get_plan("pro_annual").tier == "trader"
    # iter-60 elite_* → Professional (price-equivalent)
    assert get_plan("elite_monthly").id == "professional_monthly"
    assert get_plan("elite_annual").tier == "professional"


def test_unknown_plan_id_returns_none():
    assert get_plan("nonexistent") is None
    assert get_plan(None) is None
    assert get_plan("") is None


# ============ Tier rank + features ============
def test_tier_rank_ordering():
    assert tier_at_least("admin", "elite_ai") is True
    assert tier_at_least("elite_ai", "professional") is True
    assert tier_at_least("professional", "trader") is True
    assert tier_at_least("trader", "starter") is True
    assert tier_at_least("starter", "trader") is False
    assert tier_at_least("", "starter") is False
    # Legacy names keep price-equivalent ranks
    assert tier_at_least("pro", "trader") is True
    assert tier_at_least("elite", "professional") is True
    assert tier_at_least("elite", "elite_ai") is False


def test_starter_features():
    assert STARTER.max_accounts == 1
    assert STARTER.max_operational_mode == "demo_autopilot"
    assert STARTER.paper_shadow_mode is True
    assert STARTER.allowed_symbols == ("*",)
    for locked in ("replay_studio", "ai_coach", "digital_twin", "research_lab",
                   "vps_quick_connect", "vps_management", "portfolio_optimization",
                   "api_access", "loss_lab"):
        assert getattr(STARTER, locked) is False, locked


def test_trader_features():
    assert TRADER.max_accounts == 3
    assert TRADER.max_operational_mode == "supervised_live"
    for on in ("replay_studio", "ai_coach", "evidence_board", "broker_intelligence",
               "vps_quick_connect", "loss_lab", "auto_heal", "priority_notifications"):
        assert getattr(TRADER, on) is True, on
    for locked in ("digital_twin", "research_lab", "strategy_marketplace",
                   "vps_management", "calibrated_p_win", "api_access", "multi_vps"):
        assert getattr(TRADER, locked) is False, locked


def test_professional_features():
    assert PROFESSIONAL.max_accounts == 10
    assert PROFESSIONAL.max_operational_mode == "supervised_live"
    for on in ("digital_twin", "research_lab", "strategy_marketplace",
               "portfolio_optimization", "calibrated_p_win", "chaos_testing",
               "agent_report_cards", "vps_management", "api_access"):
        assert getattr(PROFESSIONAL, on) is True, on
    for locked in ("multi_vps", "strategy_evolution", "hypothesis_generation",
                   "fleet_monitoring", "white_label_reporting"):
        assert getattr(PROFESSIONAL, locked) is False, locked


def test_elite_ai_features():
    assert ELITE_AI.max_accounts == 50
    assert ELITE_AI.max_operational_mode == "autonomous_live"
    for on in ("multi_vps", "strategy_evolution", "hypothesis_generation",
               "fleet_monitoring", "white_label_reporting", "digital_twin",
               "research_lab"):
        assert getattr(ELITE_AI, on) is True, on
    assert ELITE_AI.support_tier == "premium"


def test_get_tier_features_defaults_and_legacy():
    assert get_tier_features("unknown") is STARTER
    assert get_tier_features(None) is STARTER
    assert get_tier_features("pro") is TRADER
    assert get_tier_features("elite") is PROFESSIONAL
    assert get_tier_features("admin") is ELITE_AI


# ============ Entitlement gates (HTTPException shape) ============
@pytest.mark.asyncio
async def test_enforce_feature_admin_bypasses(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier",
                        _async_return("admin"))
    feats = await enforce_feature({"id": "user-1"}, "strategy_evolution")
    assert feats is ELITE_AI


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
    assert payload["minimum_tier"] == "trader"


@pytest.mark.asyncio
async def test_enforce_feature_blocks_professional_from_evolution(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("professional"))
    with pytest.raises(HTTPException) as exc:
        await enforce_feature({"id": "user-1"}, "strategy_evolution")
    assert exc.value.detail["minimum_tier"] == "elite_ai"
    # trader locked out of professional features → points at professional
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("trader"))
    with pytest.raises(HTTPException) as exc2:
        await enforce_feature({"id": "user-1"}, "digital_twin")
    assert exc2.value.detail["minimum_tier"] == "professional"


@pytest.mark.asyncio
async def test_enforce_feature_allows_when_tier_has_flag(monkeypatch):
    from entitlements import enforce_feature
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("trader"))
    feats = await enforce_feature({"id": "user-1"}, "loss_lab")
    assert feats.loss_lab is True
    # Legacy "pro" tier resolves to trader features
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("pro"))
    feats2 = await enforce_feature({"id": "user-1"}, "replay_studio")
    assert feats2 is TRADER


@pytest.mark.asyncio
async def test_enforce_account_quota_blocks_at_caps(monkeypatch):
    from entitlements import enforce_account_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("starter"))
    with pytest.raises(HTTPException) as exc:
        await enforce_account_quota({"id": "u"}, current_account_count=1)
    assert exc.value.detail["error"] == "account_quota_exceeded"
    assert exc.value.detail["minimum_tier"] == "trader"
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("trader"))
    with pytest.raises(HTTPException) as exc2:
        await enforce_account_quota({"id": "u"}, current_account_count=3)
    assert exc2.value.detail["max_accounts"] == 3
    assert exc2.value.detail["minimum_tier"] == "professional"
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("professional"))
    with pytest.raises(HTTPException) as exc3:
        await enforce_account_quota({"id": "u"}, current_account_count=10)
    assert exc3.value.detail["minimum_tier"] == "elite_ai"


@pytest.mark.asyncio
async def test_enforce_account_quota_elite_ai_cap_fifty(monkeypatch):
    from entitlements import enforce_account_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("elite_ai"))
    await enforce_account_quota({"id": "u"}, current_account_count=49)  # no raise
    with pytest.raises(HTTPException) as exc:
        await enforce_account_quota({"id": "u"}, current_account_count=50)
    assert exc.value.detail["max_accounts"] == 50
    # Admin is unlimited
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("admin"))
    await enforce_account_quota({"id": "u"}, current_account_count=500)


@pytest.mark.asyncio
async def test_enforce_mode_ceiling(monkeypatch):
    from entitlements import enforce_mode_ceiling
    # Starter capped at demo — supervised_live blocked
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("starter"))
    await enforce_mode_ceiling({"id": "u"}, "demo_autopilot")  # allowed
    with pytest.raises(HTTPException) as exc:
        await enforce_mode_ceiling({"id": "u"}, "supervised_live")
    assert exc.value.status_code == 402
    assert exc.value.detail["error"] == "mode_locked"
    assert exc.value.detail["minimum_tier"] == "trader"
    # Trader allowed supervised, blocked autonomous (min tier elite_ai)
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("trader"))
    await enforce_mode_ceiling({"id": "u"}, "supervised_live")
    with pytest.raises(HTTPException) as exc2:
        await enforce_mode_ceiling({"id": "u"}, "autonomous_live")
    assert exc2.value.detail["minimum_tier"] == "elite_ai"
    # Professional also blocked from autonomous; Elite AI + admin allowed
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("professional"))
    with pytest.raises(HTTPException):
        await enforce_mode_ceiling({"id": "u"}, "autonomous_live")
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("elite_ai"))
    await enforce_mode_ceiling({"id": "u"}, "autonomous_live")
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("admin"))
    await enforce_mode_ceiling({"id": "u"}, "autonomous_live")


@pytest.mark.asyncio
async def test_enforce_vps_quota(monkeypatch):
    from entitlements import enforce_vps_quota
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("professional"))
    await enforce_vps_quota({"id": "u"}, active_deployment_count=0)  # first VPS ok
    with pytest.raises(HTTPException) as exc:
        await enforce_vps_quota({"id": "u"}, active_deployment_count=1)
    assert exc.value.detail["feature"] == "multi_vps"
    assert exc.value.detail["minimum_tier"] == "elite_ai"
    monkeypatch.setattr("entitlements.get_user_tier", _async_return("elite_ai"))
    await enforce_vps_quota({"id": "u"}, active_deployment_count=5)


# ============ helpers ============
def _async_return(value):
    async def _f(*a, **kw):
        return value
    return _f
