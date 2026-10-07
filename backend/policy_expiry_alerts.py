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


async def evaluate(db, now: datetime | None = None, *, raise_alert, notify=None) -> tuple[set, int]:
    if notify is None:
        from security_agent.alerts import send_telegram as notify
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    p = plan(exp, now)
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
