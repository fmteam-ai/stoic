"""A16-5 — a DEMO-attested account whose broker reports REAL/CONTEST money: the attestation is already
void (broker_env.attested_environment → LIVE, trading stops on that account); here we make it LOUD —
critical ops alert + security Telegram, auto-resolved when the account is re-attested or trading mode
goes back to demo."""
from __future__ import annotations

import logging

logger = logging.getLogger("demo_mode_alerts")

KIND = "demo_account_reports_real"


def dedup_key(account_id) -> str:
    return f"demo_real:{account_id}"


def plan(accounts: list[dict]) -> dict:
    from broker_env import broker_reports_real, reported_trade_mode
    active, to_raise = set(), []
    for acc in accounts:
        att = acc.get("environment_attestation") or {}
        if not att.get("approved_by") or str(att.get("environment") or "").upper() != "DEMO":
            continue
        if not broker_reports_real(acc):
            continue
        key = dedup_key(acc.get("_id"))
        active.add(key)
        label = acc.get("label") or str(acc.get("_id"))
        num = str(acc.get("account_number") or "")
        masked = ("…" + num[-3:]) if len(num) > 3 else num
        text = ("STOIC · DEMO ACCOUNT REPORTS REAL MONEY\n"
                f"Account: {label}" + (f" ({acc.get('broker')} · {masked})" if acc.get("broker") else "") + "\n"
                f"The broker's own ACCOUNT_TRADE_MODE is '{reported_trade_mode(acc)}' on an account attested as DEMO.\n"
                "The attestation is VOID and trading on this account is stopped. Check the terminal login; "
                "never re-attest DEMO while the broker says real/contest.")
        to_raise.append((key, text, {"account_id": str(acc.get("_id")), "trade_mode": reported_trade_mode(acc)}, acc))
    return {"active": active, "raise": to_raise}


async def evaluate(db, now=None, *, raise_alert, notify=None) -> tuple[set, int]:
    if notify is None:
        from security_agent.alerts import send_telegram as notify
    try:
        from synthetic_data import is_synthetic_account as is_test
    except Exception:  # noqa: BLE001
        def is_test(a):
            return bool(a.get("synthetic"))
    accounts = [a async for a in db.accounts.find(
        {"environment_attestation.approved_by": {"$exists": True}, "status": {"$ne": "deleted"}},
        {"label": 1, "broker": 1, "account_number": 1, "environment_attestation": 1, "account_trade_mode": 1,
         "ea_identity": 1, "mode": 1, "user_id": 1, "synthetic": 1}).limit(2000)]
    p = plan(accounts)
    raised = 0
    for key, text, meta, acc in p["raise"]:
        synthetic = bool(is_test(acc))
        new_id = await raise_alert(db, KIND, "critical", text.splitlines()[0] + f" — {acc.get('label') or acc.get('_id')} ({meta['trade_mode']})",
                                   dedup_key=key, meta=meta, synthetic=synthetic)
        if new_id:
            raised += 1
            if not synthetic:
                try:
                    await notify(text)
                except Exception as e:  # noqa: BLE001
                    logger.warning("demo real-money telegram failed: %s", type(e).__name__)
    return p["active"], raised
