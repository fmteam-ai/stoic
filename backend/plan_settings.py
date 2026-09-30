"""Admin → Integrations → Plans: runtime pricing / trial / currency overlay.

Stored in platform_state {_id: "plan_pricing"}; applied in-process to the
`subscription_plans` catalog at boot and immediately on every update. Updates
require admin re-auth and are appended to the hash-chained audit log.
"""
from datetime import datetime, timezone

import subscription_plans as sp

DOC_ID = "plan_pricing"
TRIAL_MAX_DAYS = 90
DEFAULT_TRIAL_DAYS = 15
DEFAULT_TRIAL_TIER = "trader"
DURATION_IDS = [d for d, _m, _x in sp.DURATION_DISCOUNTS]

_state = {"trial_days": DEFAULT_TRIAL_DAYS, "trial_tier": DEFAULT_TRIAL_TIER,
          "trial_enabled_at": None, "updated_at": None, "updated_by": None}


def current() -> dict:
    return {"base_cents": {t: sp.TIER_BASE_CENTS[t] for t in sp.TIER_ORDER},
            "discounts": {d: disc for d, _m, disc in sp.DURATION_DISCOUNTS},
            "currency": sp.CURRENCY, "currency_symbol": sp.currency_symbol(),
            "supported_currencies": list(sp.SUPPORTED_CURRENCIES),
            "trial_days": _state["trial_days"], "trial_tier": _state["trial_tier"],
            "trial_enabled_at": _state["trial_enabled_at"],
            "tiers": list(sp.TIER_ORDER), "durations": DURATION_IDS,
            "defaults": {"base_cents": dict(sp.DEFAULT_BASE_CENTS), "discounts": dict(sp.DEFAULT_DISCOUNTS),
                         "currency": "usd", "trial_days": DEFAULT_TRIAL_DAYS, "trial_tier": DEFAULT_TRIAL_TIER},
            "matrix": sp.all_plans_public(),
            "updated_at": _state["updated_at"], "updated_by": _state["updated_by"]}


def trial_config() -> dict:
    return {"days": _state["trial_days"], "tier": _state["trial_tier"], "enabled_at": _state["trial_enabled_at"]}


def validate(payload: dict) -> dict:
    base = {}
    for t in sp.TIER_ORDER:
        try:
            c = int(payload.get("base_cents", {}).get(t, sp.TIER_BASE_CENTS[t]))
        except (TypeError, ValueError):
            raise ValueError(f"{t}: price must be a whole number of cents")
        if not 100 <= c <= 10_000_000:
            raise ValueError(f"{t}: price must be between 1.00 and 100,000.00")
        base[t] = c
    if not (base["starter"] <= base["trader"] <= base["professional"] <= base["elite_ai"]):
        raise ValueError("prices must not decrease from Starter → Elite AI (upgrade proration relies on it)")
    disc = {}
    for d in DURATION_IDS:
        try:
            v = int(payload.get("discounts", {}).get(d, sp.DEFAULT_DISCOUNTS[d]))
        except (TypeError, ValueError):
            raise ValueError(f"{d}: discount must be a whole percent")
        if not 0 <= v <= 90:
            raise ValueError(f"{d}: discount must be 0-90%")
        disc[d] = v
    disc["monthly"] = 0
    cur = str(payload.get("currency") or sp.CURRENCY).lower()
    if cur not in sp.SUPPORTED_CURRENCIES:
        raise ValueError("unsupported currency")
    try:
        trial_days = int(payload.get("trial_days", _state["trial_days"]))
    except (TypeError, ValueError):
        raise ValueError("trial_days must be a whole number")
    if not 0 <= trial_days <= TRIAL_MAX_DAYS:
        raise ValueError(f"trial_days must be 0-{TRIAL_MAX_DAYS}")
    trial_tier = str(payload.get("trial_tier") or _state["trial_tier"]).lower()
    if trial_tier not in sp.TIERS:
        raise ValueError("unknown trial tier")
    return {"base_cents": base, "discounts": disc, "currency": cur, "trial_days": trial_days, "trial_tier": trial_tier}


def _apply(doc: dict) -> None:
    sp.apply_pricing(doc.get("base_cents") or {}, doc.get("discounts") or {}, doc.get("currency") or "usd")
    _state.update({"trial_days": int(doc.get("trial_days", DEFAULT_TRIAL_DAYS)),
                   "trial_tier": doc.get("trial_tier") or DEFAULT_TRIAL_TIER,
                   "trial_enabled_at": doc.get("trial_enabled_at"),
                   "updated_at": doc.get("updated_at"), "updated_by": doc.get("updated_by")})


async def load(db) -> bool:
    doc = await db.platform_state.find_one({"_id": DOC_ID})
    if not doc:   # first boot: persist defaults so the 15-day trial window has a stable start
        seed = {**validate({}), "trial_enabled_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": None, "updated_by": "system-default"}
        await db.platform_state.update_one({"_id": DOC_ID}, {"$setOnInsert": seed}, upsert=True)
        doc = await db.platform_state.find_one({"_id": DOC_ID})
    try:
        _apply(doc)
    except Exception:  # noqa: BLE001 — a corrupt overlay must never block boot
        return False
    return True


async def update(db, payload: dict, actor: dict) -> dict:
    clean = validate(payload or {})
    now = datetime.now(timezone.utc).isoformat()
    prev = current()
    enabled_at = _state["trial_enabled_at"]
    if clean["trial_days"] > 0 and (not enabled_at or prev["trial_days"] == 0):
        enabled_at = now          # trial window starts for sign-ups from now on
    if clean["trial_days"] == 0:
        enabled_at = None
    doc = {**clean, "trial_enabled_at": enabled_at, "updated_at": now, "updated_by": actor.get("email")}
    await db.platform_state.update_one({"_id": DOC_ID}, {"$set": doc}, upsert=True)
    _apply(doc)
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": actor.get("email"), "action": "plan_pricing_update",
                              "target_kind": "plan_pricing", "target_id": DOC_ID, "target_label": "Stripe plans",
                              "reason": f"currency={clean['currency']} trial={clean['trial_days']}d/{clean['trial_tier']}",
                              "meta": {"reauth": True, "before": {"base_cents": prev["base_cents"], "discounts": prev["discounts"],
                                                                  "currency": prev["currency"], "trial_days": prev["trial_days"],
                                                                  "trial_tier": prev["trial_tier"]},
                                       "after": clean},
                              "at": now})
    return current()
