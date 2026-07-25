"""Feature-gate helpers — 4-tier subscriptions (iter-120).

Routes that gate features should `Depend(...)` on the helpers below or
explicitly call `enforce_feature(...)` so the same 402 (Payment Required)
shape comes back from every gated endpoint.

Usage pattern from a FastAPI route:

    from entitlements import enforce_feature

    @router.post("/expensive-thing")
    async def do_thing(payload: dict, user=Depends(get_current_user)):
        features = await enforce_feature(user, "research_lab")
        ...

`enforce_feature` raises HTTPException(402) when the feature flag is False
on the user's resolved tier. The error includes the missing feature key
plus the minimum tier that would unlock it so the frontend can render an
upgrade-prompt modal targeted at the right plan.
"""
from __future__ import annotations
from fastapi import HTTPException

from subscription_plans import (
    Features, TIERS, TIER_ORDER, canonical_tier, get_tier_features,
    tier_at_least,
)
from subscription_service import get_user_tier, get_user_features


def _minimum_tier_for(feature: str) -> str:
    """Walk Starter → Trader → Professional → Elite AI and return the first
    tier where the feature flag is True. Returns 'elite_ai' as a safe
    fallback so the upgrade-prompt always points somewhere useful."""
    for tier in TIER_ORDER:
        if getattr(TIERS[tier], feature, False) is True:
            return tier
    return "elite_ai"


async def enforce_feature(user: dict, feature: str) -> Features:
    """Raise 402 unless the user's tier has `feature=True`. Returns the
    Features dataclass on success so callers can short-circuit further
    permission checks without an extra DB read."""
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    # Admins bypass every gate.
    if tier == "admin":
        return feats
    if not getattr(feats, feature, False):
        min_tier = _minimum_tier_for(feature)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "feature_locked",
                "feature": feature,
                "current_tier": canonical_tier(tier),
                "minimum_tier": min_tier,
                "message": (
                    f"This feature requires the {TIERS[min_tier].label} plan or higher."
                ),
            },
        )
    return feats


async def enforce_account_quota(user: dict, current_account_count: int) -> Features:
    """Raise 402 when the user has hit their tier's `max_accounts` cap.

    `current_account_count` is the count BEFORE inserting the new account.
    Caller should query `db.accounts.count_documents({"user_id": ...})`.
    """
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    if tier == "admin" or feats.max_accounts < 0:
        return feats
    if current_account_count >= feats.max_accounts:
        # First tier with a higher cap (top tier points at itself).
        cur = canonical_tier(tier)
        upgrade_to = TIER_ORDER[-1]
        for t in TIER_ORDER:
            cap = TIERS[t].max_accounts
            if cap < 0 or cap > feats.max_accounts:
                upgrade_to = t
                break
        raise HTTPException(
            status_code=402,
            detail={
                "error": "account_quota_exceeded",
                "current_tier": cur,
                "current_accounts": current_account_count,
                "max_accounts": feats.max_accounts,
                "minimum_tier": upgrade_to,
                "message": (
                    f"Your {feats.label} plan allows {feats.max_accounts} account(s). "
                    + (f"Upgrade to {TIERS[upgrade_to].label} for more."
                       if upgrade_to != cur else "You are on the top plan.")
                ),
            },
        )
    return feats


async def enforce_symbol_allowed(user: dict, symbol: str) -> Features:
    """Raise 402 when the symbol isn't in the user's tier allow-list.
    (All current tiers allow every symbol — kept for future laddering.)"""
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    if tier == "admin" or "*" in feats.allowed_symbols:
        return feats
    if symbol.upper() not in feats.allowed_symbols:
        upgrade_to = TIER_ORDER[-1]
        for t in TIER_ORDER[1:]:
            allowed = TIERS[t].allowed_symbols
            if "*" in allowed or symbol.upper() in allowed:
                upgrade_to = t
                break
        raise HTTPException(
            status_code=402,
            detail={
                "error": "symbol_locked",
                "symbol": symbol.upper(),
                "current_tier": canonical_tier(tier),
                "allowed_symbols": list(feats.allowed_symbols),
                "minimum_tier": upgrade_to,
                "message": (
                    f"Trading {symbol.upper()} requires the {TIERS[upgrade_to].label} plan."
                ),
            },
        )
    return feats


async def enforce_mode_ceiling(user: dict, target_mode: str) -> Features:
    """Raise 402 when the requested operational mode exceeds the plan's
    `max_operational_mode` ceiling. Called on mode PROMOTIONS only —
    demotions always apply instantly. The broker-certification promotion
    gate still runs AFTER this check (a plan never bypasses safety)."""
    from operational_modes import MODES
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    if tier == "admin":
        return feats
    target_rank = (MODES.get(target_mode) or {}).get("rank", 99)
    ceiling_rank = (MODES.get(feats.max_operational_mode) or {}).get("rank", 0)
    if target_rank > ceiling_rank:
        min_tier = TIER_ORDER[-1]
        for t in TIER_ORDER:
            t_ceiling = (MODES.get(TIERS[t].max_operational_mode) or {}).get("rank", 0)
            if t_ceiling >= target_rank:
                min_tier = t
                break
        raise HTTPException(
            status_code=402,
            detail={
                "error": "mode_locked",
                "target_mode": target_mode,
                "max_operational_mode": feats.max_operational_mode,
                "current_tier": canonical_tier(tier),
                "minimum_tier": min_tier,
                "message": (
                    f"{(MODES.get(target_mode) or {}).get('label', target_mode)} requires "
                    f"the {TIERS[min_tier].label} plan (your ceiling: "
                    f"{feats.max_operational_mode})."
                ),
            },
        )
    return feats


async def enforce_vps_quota(user: dict, active_deployment_count: int) -> Features:
    """Raise 402 when creating a SECOND (or later) VPS deployment on a plan
    without the `multi_vps` flag. First deployment is governed by the
    vps_management / vps_quick_connect flags instead."""
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    if tier == "admin" or feats.multi_vps:
        return feats
    if active_deployment_count >= 1:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "feature_locked",
                "feature": "multi_vps",
                "current_tier": canonical_tier(tier),
                "minimum_tier": _minimum_tier_for("multi_vps"),
                "message": ("Managing multiple VPS deployments requires the "
                            f"{TIERS[_minimum_tier_for('multi_vps')].label} plan."),
            },
        )
    return feats


async def cooldown_floor(user: dict) -> int:
    """Return the minimum signal-cooldown minutes for this user's tier.
    Bot-runner clamps `cfg.signal_cooldown_minutes` to this floor so a
    Starter user can't manually set it to 1 min via the API."""
    feats = await get_user_features(user["id"])
    return feats.min_signal_cooldown_minutes


__all__ = [
    "enforce_feature", "enforce_account_quota", "enforce_symbol_allowed",
    "enforce_mode_ceiling", "enforce_vps_quota",
    "cooldown_floor", "tier_at_least", "get_user_tier", "get_user_features",
    "require_feature",
]


def require_feature(feature: str):
    """FastAPI Depends factory — gate an entire router behind one flag.

    Usage:
        from auth import get_current_user
        from entitlements import require_feature
        router = APIRouter(dependencies=[Depends(require_feature("research_lab"))])
    """
    from fastapi import Depends
    from auth import get_current_user

    async def _dep(user=Depends(get_current_user)):
        await enforce_feature(user, feature)
        return True
    return _dep
