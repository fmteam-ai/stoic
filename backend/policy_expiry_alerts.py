"""A16-4 — lifetime of the approved signed (demo) inventory policy: a Telegram reminder 3 days before
it expires and a critical alert once it has — the projection already turns the inventory close-only."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("policy_expiry_alerts")

KIND_EXPIRING, KIND_EXPIRED = "policy_expiring", "policy_expired"
KEY_EXPIRING, KEY_EXPIRED = "policy_expiry:expiring", "policy_expiry:expired"


def plan(exp: dict, now: datetime | None = None) -> dict:
    from inventory_projection import policy_expiry
    now = now or datetime.now(timezone.utc)
    pe = policy_expiry(exp, now)
    version = (exp or {}).get("policy_version") or "?"
    kind = "DEMO-only" if (exp or {}).get("demo_only") else "signed"
    out = {"active": set(), "raise": []}
    if not pe["expires_at"]:
        return out
    if pe["expired"]:
        out["active"].add(KEY_EXPIRED)
        out["raise"].append((KIND_EXPIRED, "critical", KEY_EXPIRED,
                             f"STOIC · {kind.upper()} POLICY EXPIRED\nPolicy {version} expired {pe['expires_at'][:16]} UTC — the inventory is "
                             "CLOSE-ONLY (no new entries) until a new signed policy is approved.\n"
                             "Fix: Actions → policy-migration (new version, previous = the expired one) → update → Propose → Approve."))
    elif pe["reminder_due"]:
        out["active"].add(KEY_EXPIRING)
        out["raise"].append((KIND_EXPIRING, "warning", KEY_EXPIRING,
                             f"STOIC · {kind.upper()} POLICY EXPIRES IN {pe['days_left']} DAY(S)\nPolicy {version} expires {pe['expires_at'][:16]} UTC. "
                             "Sign and approve the next policy before then or the inventory turns close-only."))
    return out


KEY_RULES_EXPIRING, KEY_RULES_EXPIRED = "rules_policy_expiry:expiring", "rules_policy_expiry:expired"


def plan_rules(pol: dict | None, now: datetime | None = None) -> dict:
    """A4 — rules policy: reminder 7 days before expiry (with the pre-filled rules-policy workflow link), critical once expired."""
    import rules_policy as rp
    out = {"active": set(), "raise": []}
    e = rp.expiry(pol, now)
    if not pol or not e["expires_at"]:
        return out
    link = rp.renew_link() or "Actions → rules-policy"
    pre = rp.renewal_prefill(pol)
    form = (f"version {pre['policy_version']} · demo_only {str(pre['demo_only']).lower()} · cap {pre['max_accounts_per_user']} · brokers {pre['allowed_brokers']}"
            f" · max_risk {pre['max_risk_pct'] or '-'} · max_daily_loss {pre['max_daily_loss_pct'] or '-'} · {pre['expires_days']} days")
    if e["expired"]:
        out["active"].add(KEY_RULES_EXPIRED)
        out["raise"].append((KIND_EXPIRED, "critical", KEY_RULES_EXPIRED,
                             f"STOIC · RULES POLICY EXPIRED\nPolicy {pol.get('policy_version')} expired {e['expires_at'][:16]} UTC — every account is "
                             f"CLOSE-ONLY until a new signed rules policy is active.\nRenew: {link}\nForm: {form}\nThen Admin → Policy → Load latest signed policy."))
    elif e["reminder_due"]:
        out["active"].add(KEY_RULES_EXPIRING)
        out["raise"].append((KIND_EXPIRING, "warning", KEY_RULES_EXPIRING,
                             f"STOIC · RULES POLICY EXPIRES IN {e['days_left']} DAY(S)\nPolicy {pol.get('policy_version')} expires {e['expires_at'][:16]} UTC.\n"
                             f"Renew: {link}\nForm: {form}\nThen Admin → Policy → Load latest signed policy."))
    return out


async def evaluate(db, now: datetime | None = None, *, raise_alert, notify=None) -> tuple[set, int]:
    if notify is None:
        from security_agent.alerts import send_telegram as notify
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    p = plan(exp, now)
    # A4 (Easy Start) — the RULES policy: 7-day renewal reminder with the pre-filled workflow link
    import rules_policy as rp
    pol = await rp.active(db)
    for kind, severity, key, text in plan_rules(pol, now)["raise"]:
        p["active"].add(key)
        p["raise"].append((kind, severity, key, text))
    raised = 0
    for kind, severity, key, text in p["raise"]:
        new_id = await raise_alert(db, kind, severity, text.splitlines()[0] + " — " + text.splitlines()[1][:160],
                                   dedup_key=key, meta={"policy_version": exp.get("policy_version"), "expires_at": exp.get("policy_expires_at")})
        if new_id:
            raised += 1
            try:
                await notify(text)
            except Exception as e:  # noqa: BLE001
                logger.warning("policy expiry telegram failed: %s", type(e).__name__)
    return p["active"], raised
