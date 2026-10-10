"""M120-2 — `key_rotation_due` ops alert (warning → Telegram/email like every warning): one row per due item
(release key / runtime key > 180 d, Origin CA cert < 30 d), auto-resolves once the item was rotated/renewed."""
from __future__ import annotations

from datetime import datetime, timezone

KIND = "key_rotation_due"


def dedup_key(name: str) -> str:
    return f"key_age:{name}"


def plan(ages: dict) -> dict:
    out = {"active": set(), "raise": []}
    for it in ages.get("items") or []:
        if not it.get("due"):
            continue
        key = dedup_key(it["name"])
        out["active"].add(key)
        if it["kind"] == "key":
            text = f"{it['label']} is {it['age_days']} days old — rotate (limit {it['limit_days']} d): {it['fix']}"
        elif it["days_left"] >= 0:
            text = f"{it['label']} expires in {it['days_left']} days — renew: {it['fix']}"
        else:
            text = f"{it['label']} EXPIRED {-it['days_left']} days ago — Cloudflare Full (strict) refuses the origin: {it['fix']}"
        out["raise"].append((KIND, "warning", key, text))
    return out


async def evaluate(db, now: datetime | None = None, *, raise_alert) -> tuple[set, int]:
    from key_ages import key_ages
    p = plan(key_ages(now=now or datetime.now(timezone.utc)))
    raised = 0
    for kind, severity, key, text in p["raise"]:
        if await raise_alert(db, kind, severity, text, dedup_key=key):
            raised += 1
    return p["active"], raised
