"""Subscription plans — prepaid model with explicit `valid_until` dates.

We model recurring revenue as discrete prepaid plan purchases rather than
true Stripe recurring subscriptions, because the emergentintegrations
StripeCheckout wrapper is designed for one-time payments.

Each plan grants `duration_months` of access after a successful payment.
Renewal is explicit (user re-purchases) — no surprise auto-charges.
"""
from dataclasses import dataclass

BASE_MONTHLY_USD = 49.0


@dataclass(frozen=True)
class Plan:
    id: str
    label: str
    duration_months: int
    discount_pct: int  # 0 for monthly
    description: str

    @property
    def amount_usd(self) -> float:
        gross = BASE_MONTHLY_USD * self.duration_months
        return round(gross * (1.0 - self.discount_pct / 100.0), 2)

    @property
    def effective_monthly_usd(self) -> float:
        return round(self.amount_usd / self.duration_months, 2)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "duration_months": self.duration_months,
            "discount_pct": self.discount_pct,
            "amount_usd": self.amount_usd,
            "effective_monthly_usd": self.effective_monthly_usd,
            "savings_usd": round(
                BASE_MONTHLY_USD * self.duration_months - self.amount_usd, 2
            ),
            "description": self.description,
        }


PLANS = {
    "monthly": Plan("monthly", "Monthly", 1, 0, "Pay as you go. Cancel any time."),
    "quarterly": Plan("quarterly", "3 Months", 3, 10, "Save 10% — 1 month free over a year."),
    "semi_annual": Plan("semi_annual", "6 Months", 6, 20, "Save 20% — locked in for half a year."),
    "annual": Plan("annual", "Annual", 12, 40, "Save 40% — best value, our power-user pick."),
}


def get_plan(plan_id: str) -> Plan | None:
    return PLANS.get(plan_id)


def all_plans_public() -> list[dict]:
    return [p.to_public() for p in PLANS.values()]
