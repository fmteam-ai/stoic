"""Subscription plans — 4 tiers (Starter/Trader/Professional/Elite AI) × duration.

Catalog (iter-120 repricing — replaces the iter-60 Starter/Pro/Elite matrix):

  Tier          Monthly  3-Months  6-Months  Annual
  ────────────────────────────────────────────────────
  Starter       $39      $105      $187      $281
  Trader        $99      $267      $475      $713
  Professional  $199     $537      $955      $1432
  Elite AI      $399     $1077     $1915     $2873

Duration discounts uniform across tiers: monthly=0%, quarterly=10%,
semi=20%, annual=40%.

Backward compatibility:
  • Legacy plan_ids alias forward: monthly/quarterly/semi_annual/annual and
    pro_* → trader_*; elite_* → professional_* (price-equivalent mapping).
  • Legacy tier names "pro"/"elite" resolve to trader/professional features.

Feature gates query via `subscription_service.get_user_tier(user_id)` →
`get_tier_features(tier)`. The `max_operational_mode` ceiling is enforced by
`entitlements.enforce_mode_ceiling` at mode-promotion time — Autonomous Live
additionally requires broker certification via the promotion gate.
"""
from dataclasses import dataclass
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
    max_operational_mode: str        # Ceiling on the mode ladder (entitlements.enforce_mode_ceiling)
    loss_lab: bool                   # Post-mortem AI investigations
    auto_heal: bool                  # Background self-healing daemon
    correlation_kelly: bool          # Correlation/CVaR lot trim (risk management core)
    calibrated_p_win: bool           # AI probability calibration (Platt-scaled)
    drift_auto_retrain: bool         # ADWIN-triggered retrain
    paper_shadow_mode: bool          # Shadow Mode — run strategies without real money
    custom_thresholds: bool          # User can tune A+/entropy/sector caps
    weekly_digest_pdf: bool          # Advanced reporting (weekly AI digest)
    api_access: bool                 # Programmatic /api/v1 read access
    priority_notifications: bool     # Mobile/SMS/Telegram alerts (vs email only)
    support_tier: str                # "community" | "standard" | "priority" | "premium"
    replay_studio: bool              # Tier 12 Replay Studio
    ai_coach: bool                   # Tier 11 AI Coach cards
    evidence_board: bool             # Evidence/verification board
    broker_intelligence: bool        # Broker execution intelligence
    vps_quick_connect: bool          # VPS Path B — connect existing VPS + EA pairing
    digital_twin: bool               # Tier 6 Digital Twin
    research_lab: bool               # Self-improving Research Agent
    strategy_marketplace: bool       # Tier 5 Strategy Marketplace
    portfolio_optimization: bool     # RL capital allocator / portfolio optimizer
    chaos_testing: bool              # Tier 14 chaos drills
    agent_report_cards: bool         # Weekly per-agent report cards
    vps_management: bool             # VPS Path A — provisioning + management dashboard
    multi_vps: bool                  # More than one managed VPS deployment
    strategy_evolution: bool         # Strategy genetics / evolution
    hypothesis_generation: bool      # AI hypothesis generation
    fleet_monitoring: bool           # Fleet-wide monitoring
    white_label_reporting: bool      # White-label report exports


STARTER = Features(
    label="Starter",
    max_accounts=1,
    allowed_symbols=("*",),
    auto_execute=True,
    min_signal_cooldown_minutes=15,
    max_operational_mode="demo_autopilot",
    loss_lab=False, auto_heal=False, correlation_kelly=True,
    calibrated_p_win=False, drift_auto_retrain=False,
    paper_shadow_mode=True, custom_thresholds=False,
    weekly_digest_pdf=False, api_access=False,
    priority_notifications=False,
    support_tier="community",
    replay_studio=False, ai_coach=False, evidence_board=False,
    broker_intelligence=False, vps_quick_connect=False,
    digital_twin=False, research_lab=False, strategy_marketplace=False,
    portfolio_optimization=False, chaos_testing=False,
    agent_report_cards=False, vps_management=False, multi_vps=False,
    strategy_evolution=False, hypothesis_generation=False,
    fleet_monitoring=False, white_label_reporting=False,
)
TRADER = Features(
    label="Trader",
    max_accounts=3,
    allowed_symbols=("*",),
    auto_execute=True,
    min_signal_cooldown_minutes=3,
    max_operational_mode="supervised_live",
    loss_lab=True, auto_heal=True, correlation_kelly=True,
    calibrated_p_win=False, drift_auto_retrain=False,
    paper_shadow_mode=True, custom_thresholds=False,
    weekly_digest_pdf=True, api_access=False,
    priority_notifications=True,
    support_tier="standard",
    replay_studio=True, ai_coach=True, evidence_board=True,
    broker_intelligence=True, vps_quick_connect=True,
    digital_twin=False, research_lab=False, strategy_marketplace=False,
    portfolio_optimization=False, chaos_testing=False,
    agent_report_cards=False, vps_management=False, multi_vps=False,
    strategy_evolution=False, hypothesis_generation=False,
    fleet_monitoring=False, white_label_reporting=False,
)
PROFESSIONAL = Features(
    label="Professional",
    max_accounts=10,
    allowed_symbols=("*",),
    auto_execute=True,
    min_signal_cooldown_minutes=2,
    max_operational_mode="supervised_live",
    loss_lab=True, auto_heal=True, correlation_kelly=True,
    calibrated_p_win=True, drift_auto_retrain=True,
    paper_shadow_mode=True, custom_thresholds=True,
    weekly_digest_pdf=True, api_access=True,
    priority_notifications=True,
    support_tier="priority",
    replay_studio=True, ai_coach=True, evidence_board=True,
    broker_intelligence=True, vps_quick_connect=True,
    digital_twin=True, research_lab=True, strategy_marketplace=True,
    portfolio_optimization=True, chaos_testing=True,
    agent_report_cards=True, vps_management=True, multi_vps=False,
    strategy_evolution=False, hypothesis_generation=False,
    fleet_monitoring=False, white_label_reporting=False,
)
ELITE_AI = Features(
    label="Elite AI",
    max_accounts=50,
    allowed_symbols=("*",),
    auto_execute=True,
    min_signal_cooldown_minutes=1,
    max_operational_mode="autonomous_live",
    loss_lab=True, auto_heal=True, correlation_kelly=True,
    calibrated_p_win=True, drift_auto_retrain=True,
    paper_shadow_mode=True, custom_thresholds=True,
    weekly_digest_pdf=True, api_access=True,
    priority_notifications=True,
    support_tier="premium",
    replay_studio=True, ai_coach=True, evidence_board=True,
    broker_intelligence=True, vps_quick_connect=True,
    digital_twin=True, research_lab=True, strategy_marketplace=True,
    portfolio_optimization=True, chaos_testing=True,
    agent_report_cards=True, vps_management=True, multi_vps=True,
    strategy_evolution=True, hypothesis_generation=True,
    fleet_monitoring=True, white_label_reporting=True,
)


# Tier ordering — used by feature-gate `tier_at_least(...)`.
# Legacy tier names ("pro"/"elite") keep their price-equivalent rank.
TIER_ORDER = ("starter", "trader", "professional", "elite_ai")
TIER_RANK = {"starter": 1, "trader": 2, "professional": 3, "elite_ai": 4,
             "pro": 2, "elite": 3, "admin": 99}
TIERS = {"starter": STARTER, "trader": TRADER,
         "professional": PROFESSIONAL, "elite_ai": ELITE_AI}
LEGACY_TIER_NAMES = {"pro": "trader", "elite": "professional"}


def canonical_tier(tier: str) -> str:
    t = (tier or "starter").lower()
    return LEGACY_TIER_NAMES.get(t, t)


def get_tier_features(tier: str) -> Features:
    """Resolve features for a tier name; admin → Elite AI (full bypass),
    legacy pro/elite → trader/professional, unknown → Starter."""
    t = canonical_tier(tier)
    if t == "admin":
        return ELITE_AI
    return TIERS.get(t, STARTER)


def tier_at_least(user_tier: str, required: str) -> bool:
    """True when user_tier ≥ required (admin > elite_ai > professional > trader > starter)."""
    return TIER_RANK.get((user_tier or "").lower(), 0) >= TIER_RANK.get(
        (required or "").lower(), 0)


# =============================================================================
# Plan SKUs (tier × duration) — prices stored as INTEGER CENTS (iter-122).
# =============================================================================
TIER_BASE_CENTS = {"starter": 3900, "trader": 9900,
                   "professional": 19900, "elite_ai": 39900}
# Float view kept for display/back-compat — derived, never authoritative.
TIER_BASE_USD = {t: c / 100.0 for t, c in TIER_BASE_CENTS.items()}
DURATION_DISCOUNTS = [
    ("monthly",     1,  0),
    ("quarterly",   3,  10),
    ("semi_annual", 6,  20),
    ("annual",     12,  40),
]
DEFAULT_BASE_CENTS = dict(TIER_BASE_CENTS)
DEFAULT_DISCOUNTS = {d: disc for d, _m, disc in DURATION_DISCOUNTS}
SUPPORTED_CURRENCIES = {"usd": "$", "eur": "€", "gbp": "£", "chf": "CHF ", "aud": "A$", "cad": "C$"}
CURRENCY = "usd"   # platform-wide checkout currency — Admin → Integrations → Plans overrides at runtime
PRICING_VERSION = 0   # monotonically increasing; bumped by every admin pricing update (audit r28 P2-01)


def currency_symbol() -> str:
    return SUPPORTED_CURRENCIES.get(CURRENCY, CURRENCY.upper() + " ")


@dataclass(frozen=True)
class Plan:
    id: str                # e.g. "trader_annual"
    tier: str              # "starter" | "trader" | "professional" | "elite_ai"
    duration_label: str    # "Monthly" / "3 Months" / "6 Months" / "Annual"
    duration_months: int
    discount_pct: int
    amount_cents: int      # authoritative price — INTEGER CENTS
    description: str

    @property
    def amount_usd(self) -> float:
        """Display/back-compat view — exact because cents are integral."""
        return self.amount_cents / 100.0

    @property
    def effective_monthly_usd(self) -> float:
        return round(self.amount_cents / self.duration_months / 100.0, 2)

    @property
    def features(self) -> Features:
        return TIERS[self.tier]

    def to_public(self) -> dict:
        f = self.features
        savings_cents = TIER_BASE_CENTS[self.tier] * self.duration_months - self.amount_cents
        return {
            "id": self.id,
            "tier": self.tier,
            "tier_label": f.label,
            "duration_label": self.duration_label,
            "duration_months": self.duration_months,
            "discount_pct": self.discount_pct,
            # currency-neutral integer minor units (audit r28 P2-04) — authoritative
            "amount_cents": self.amount_cents,
            "amount_minor": self.amount_cents,
            "effective_monthly_minor": int(round(self.amount_cents / self.duration_months)),
            "savings_minor": savings_cents,
            "currency": CURRENCY,
            "currency_symbol": currency_symbol(),
            "pricing_version": PRICING_VERSION,
            # DEPRECATED: USD-named floats kept for compatibility; they carry the
            # active currency's amount, not necessarily US dollars.
            "amount_usd": self.amount_usd,
            "effective_monthly_usd": self.effective_monthly_usd,
            "savings_usd": savings_cents / 100.0,
            "description": self.description,
        }


def _build_plans() -> dict[str, Plan]:
    out: dict[str, Plan] = {}
    for tier, base_cents in TIER_BASE_CENTS.items():
        for dur_id, months, disc in DURATION_DISCOUNTS:
            gross_cents = base_cents * months
            net_cents = int(round(gross_cents * (100 - disc) / 100.0))
            label_dur = {
                "monthly": "Monthly", "quarterly": "3 Months",
                "semi_annual": "6 Months", "annual": "Annual",
            }[dur_id]
            desc = {
                "monthly":     "Pay as you go. Cancel any time.",
                "quarterly":   f"Save {disc}% — 3 months prepaid.",
                "semi_annual": f"Save {disc}% — 6 months prepaid.",
                "annual":      f"Save {disc}% — best value, our power-user pick.",
            }[dur_id]
            out[f"{tier}_{dur_id}"] = Plan(
                id=f"{tier}_{dur_id}", tier=tier,
                duration_label=label_dur, duration_months=months,
                discount_pct=disc, amount_cents=net_cents, description=desc,
            )
    return out


PLANS = _build_plans()


def apply_pricing(base_cents: dict, discounts: dict, currency: str, pricing_version: int | None = None) -> None:
    """Runtime override (Admin → Integrations → Plans). Mutates the module
    catalog IN PLACE so every `from subscription_plans import PLANS/TIER_BASE_CENTS`
    binding sees the new prices without a restart."""
    global CURRENCY, PRICING_VERSION
    for t in TIER_ORDER:
        TIER_BASE_CENTS[t] = int(base_cents.get(t, DEFAULT_BASE_CENTS[t]))
        TIER_BASE_USD[t] = TIER_BASE_CENTS[t] / 100.0
    for i, (dur_id, months, _disc) in enumerate(DURATION_DISCOUNTS):
        DURATION_DISCOUNTS[i] = (dur_id, months, int(discounts.get(dur_id, DEFAULT_DISCOUNTS[dur_id])))
    CURRENCY = (currency or "usd").lower()
    if pricing_version is not None:
        PRICING_VERSION = int(pricing_version)
    rebuilt = _build_plans()
    PLANS.clear()
    PLANS.update(rebuilt)

# Legacy plan IDs — map forward so existing customers keep a valid plan:
#   pre-iter-60 singles (monthly/…)   → Trader (was Pro)
#   iter-60 pro_* SKUs                → trader_* (price-equivalent, $99 base)
#   iter-60 elite_* SKUs              → professional_* (price-equivalent, $199 base)
LEGACY_ALIASES = {
    "monthly":     "trader_monthly",
    "quarterly":   "trader_quarterly",
    "semi_annual": "trader_semi_annual",
    "annual":      "trader_annual",
}
for _d in ("monthly", "quarterly", "semi_annual", "annual"):
    LEGACY_ALIASES[f"pro_{_d}"] = f"trader_{_d}"
    LEGACY_ALIASES[f"elite_{_d}"] = f"professional_{_d}"


def get_plan(plan_id: str) -> Optional[Plan]:
    """Resolve a plan_id, honouring legacy aliases."""
    if not plan_id:
        return None
    resolved = LEGACY_ALIASES.get(plan_id, plan_id)
    return PLANS.get(resolved)


def all_plans_public() -> list[dict]:
    return [p.to_public() for p in PLANS.values()]


def tier_features_public() -> dict:
    """Marketing-friendly serialisation of the 4 tier feature matrices."""
    return {tier: {**vars(f), "label": f.label} for tier, f in TIERS.items()}
