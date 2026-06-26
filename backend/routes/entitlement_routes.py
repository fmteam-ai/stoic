"""Tier + feature-flag inspection endpoints (iter-60).

Exposes:
  GET /api/entitlements/me        — current user's tier + features
  GET /api/entitlements/tiers     — public tier comparison matrix
"""
from fastapi import APIRouter, Depends
from auth import get_current_user
from subscription_plans import tier_features_public, get_tier_features
from subscription_service import get_user_tier
from database import get_db

router = APIRouter(prefix="/entitlements", tags=["entitlements"])


@router.get("/me")
async def my_entitlements(user=Depends(get_current_user)):
    """Resolve this user's tier + their feature dict + current account count.

    Frontend uses this to:
      • render the Upgrade button only when needed
      • show/hide locked menu items in the sidebar
      • surface "X of Y accounts used" badge on the Accounts page
    """
    tier = await get_user_tier(user["id"])
    feats = get_tier_features(tier)
    db = get_db()
    account_count = await db.accounts.count_documents({"user_id": user["id"]})
    return {
        "tier": tier,
        "features": {**vars(feats), "label": feats.label},
        "account_count": account_count,
    }


@router.get("/tiers")
async def public_tier_matrix():
    """Marketing-friendly comparison matrix used by the /subscription page."""
    return tier_features_public()
