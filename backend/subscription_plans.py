"""Subscription plans — tiered (Starter/Pro/Elite) × duration (Monthly/Quarterly/Semi/Annual).

iter-60 expanded the single-tier `monthly/quarterly/semi_annual/annual`
catalogue into a 3×4 matrix so feature-gating can drive upgrade revenue.

  Tier      Monthly  3-Months  6-Months  Annual
  ──────────────────────────────────────────────
  Starter   $29      $78       $139      $209
  Pro       $99      $267      $475      $713
  Elite     $199     $537      $955      $1432

Duration discounts are applied uniformly across tiers:
   monthly=0%, quarterly=10%, semi=20%, annual=40%.

Each Plan carries the tier's `Features` dataclass — feature-gates query
this via `subscription_service.get_user_tier(user_id)`.

Backward compatibility:
  The legacy plan_ids (`monthly`/`quarterly`/`semi_annual`/`annual`) are
  preserved as aliases mapping to the `pro_*` SKUs so existing customers
  carry the Pro tier through any upgrade flow.
"""
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# Tier feature matrix
# =============================================================================
@dataclass(frozen=True)
class Features:
    """Capability flags resolved at request-time by feature-gates."""
    label: str                       # Marketing label
    max_accounts: int                # Hard cap on MT5/Binance accounts (-1=unlimited)
    allowed_symbols: tuple[str, ...] # Symbols the bot may trade ("*" = all)
    auto_execute: bool               # Bot can auto-fire trades (vs signal-only)
    min_signal_cooldown_minutes: int # Floor for cfg.signal_cooldown_minutes
    loss_lab: bool                   # Post-mortem AI investigations
    auto_heal: bool                  # Background self-healing daemon
    correlation_kelly: bool          # iter-51 correlation/CVaR lot trim
    calibrated_p_win: bool           # iter-52 Platt-scaled probabilities
    drift_auto_retrain: bool         # iter-52 ADWIN-triggered retrain
    paper_shadow_mode: bool          # Run new strategies without real money
    custom_thresholds: bool          # User can tune A+/entropy/sector caps
    weekly_digest_pdf: bool          # Weekly AI-generated activity digest
    api_access: bool                 # Programmatic /api/external/* read access
    priority_notifications: bool     # SMS + email + Telegram (vs Telegram only)
    support_tier: str                # "community" | "priority" | "concierge"


STARTER = Features(
    label="Starter",
    max_accounts=1,
    allowed_symbols=("XAUUSD",),
    auto_execute=False,
    min_signal_cooldown_minutes=15,
    loss_lab=False, auto_heal=False, correlation_kelly=False,
    calibrated_p_win=False, drift_auto_retrain=False,
    paper_shadow_mode=False, custom_thresholds=False,
    weekly_digest_pdf=False, api_access=False,
    priority_notifications=False,
    support_tier="community",
)
PRO = Features(
    label="Pro",
    max_accounts=3,
    allowed_symbols=("XAUUSD", "BTCUSD", "ETHUSD"),
    auto_execute=True,
    min_signal_cooldown_minutes=3,
    loss_lab=True, auto_heal=True, correlation_kelly=True,
    calibrated_p_win=True, drift_auto_retrain=False,
    paper_shadow_mode=False, custom_thresholds=False,
    weekly_digest_pdf=False, api_access=False,
    priority_notifications=True,
    support_tier="priority",
)
ELITE = Features(
    label="Elite",
    max_accounts=-1,
    allowed_symbols=("*",),
    auto_execute=True,
    min_signal_cooldown_minutes=1,
    loss_lab=True, auto_heal=True, correlation_kelly=True,
    calibrated_p_win=True, drift_auto_retrain=True,
    paper_shadow_mode=True, custom_thresholds=True,
    weekly_digest_pdf=True, api_access=True,
    priority_notifications=True,
    support_tier="concierge",
)


# Tier ordering — used by feature-gate `tier_at_least(...)`
TIER_RANK = {"starter": 1, "pro": 2, "elite": 3, "admin": 99}
TIERS = {"starter": STARTER, "pro": PRO, "elite": ELITE}


def get_tier_features(tier: str) -> Features:
    """Resolve features for a tier name; admin → Elite (full bypass),
    unknown → Starter (safe default)."""
    t = (tier or "starter").lower()
    if t == "admin":
        return ELITE
    return TIERS.get(t, STARTER)


def tier_at_least(user_tier: str, required: str) -> bool:
    """True when user_tier ≥ required (admin > elite > pro > starter)."""
    return TIER_RANK.get((user_tier or "").lower(), 0) >= TIER_RANK.get(required.lower(), 0)


# =============================================================================
# Plan SKUs (tier × duration)
# =============================================================================
TIER_BASE_USD = {"starter": 29.0, "pro": 99.0, "elite": 199.0}
DURATION_DISCOUNTS = [
    ("monthly",     1,  0),
    ("quarterly",   3,  10),
    ("semi_annual", 6,  20),
    ("annual",     12,  40),
]


@dataclass(frozen=True)
class Plan:
    id: str                # e.g. "pro_annual"
    tier: str              # "starter" | "pro" | "elite"
    duration_label: str    # "Monthly" / "3 Months" / "6 Months" / "Annual"
    duration_months: int
    discount_pct: int
    amount_usd: float
    description: str

    @property
    def effective_monthly_usd(self) -> float:
        return round(self.amount_usd / self.duration_months, 2)

    @property
    def features(self) -> Features:
        return TIERS[self.tier]

    def to_public(self) -> dict:
        f = self.features
        return {
            "id": self.id,
            "tier": self.tier,
            "tier_label": f.label,
            "duration_label": self.duration_label,
            "duration_months": self.duration_months,
            "discount_pct": self.discount_pct,
            "amount_usd": self.amount_usd,
            "effective_monthly_usd": self.effective_monthly_usd,
            "savings_usd": round(
                TIER_BASE_USD[self.tier] * self.duration_months - self.amount_usd, 2
            ),
            "description": self.description,
        }


def _build_plans() -> dict[str, Plan]:
    out: dict[str, Plan] = {}
    for tier, base_usd in TIER_BASE_USD.items():
        for dur_id, months, disc in DURATION_DISCOUNTS:
            gross = base_usd * months
            net = round(gross * (1.0 - disc / 100.0), 2)
            label_dur = {
                "monthly": "Monthly", "quarterly": "3 Months",
                "semi_annual": "6 Months", "annual": "Annual",
            }[dur_id]
            desc = {
                "monthly":     "Pay as you go. Cancel any time.",
                "quarterly":   "Save 10% — 3 months prepaid.",
                "semi_annual": "Save 20% — 6 months prepaid.",
                "annual":      "Save 40% — best value, our power-user pick.",
            }[dur_id]
            out[f"{tier}_{dur_id}"] = Plan(
                id=f"{tier}_{dur_id}", tier=tier,
                duration_label=label_dur, duration_months=months,
                discount_pct=disc, amount_usd=net, description=desc,
            )
    return out


PLANS = _build_plans()

# Legacy plan IDs (pre iter-60) — map to Pro tier for backward compatibility.
# Existing paying customers will be grandfathered into Pro on first lookup.
LEGACY_ALIASES = {
    "monthly":     "pro_monthly",
    "quarterly":   "pro_quarterly",
    "semi_annual": "pro_semi_annual",
    "annual":      "pro_annual",
}


def get_plan(plan_id: str) -> Optional[Plan]:
    """Resolve a plan_id, honouring legacy aliases."""
    if not plan_id:
        return None
    resolved = LEGACY_ALIASES.get(plan_id, plan_id)
    return PLANS.get(resolved)


def all_plans_public() -> list[dict]:
    return [p.to_public() for p in PLANS.values()]


def tier_features_public() -> dict:
    """Marketing-friendly serialisation of the 3 tier feature matrices."""
    return {tier: {**vars(f), "label": f.label} for tier, f in TIERS.items()}
