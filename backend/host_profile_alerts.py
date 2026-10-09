"""A20-P1-03 / M117-1 — freshness of the signed host profile (deploy/state/host_profile.json, refreshed every 6 h by
stoic-host-profile.timer): warning once a refresh is overdue (>12 h, ≥1 missed run), critical once the profile is
no longer verified (>24 h / unsigned / unparseable) — in live mode readiness then blocks new exposure."""
from __future__ import annotations

from datetime import datetime, timezone

KIND_OVERDUE, KIND_UNVERIFIED = "host_profile_refresh_overdue", "host_profile_unverified"
KEY_OVERDUE, KEY_UNVERIFIED = "host_profile:overdue", "host_profile:unverified"


def plan(hp: dict) -> dict:
    out = {"active": set(), "raise": []}
    if not hp.get("verified"):
        if hp.get("profile") == "unknown" and hp.get("source") == "env" and not hp.get("detected_at"):
            return out   # never recorded (dev/preview without an installer run) — the readiness card already says so
        out["active"].add(KEY_UNVERIFIED)
        out["raise"].append((KIND_UNVERIFIED, "critical", KEY_UNVERIFIED,
                             f"host profile UNVERIFIED ({hp.get('unverified_reason')}) — live trading blocked until a fresh signed "
                             "profile is written: systemctl status stoic-host-profile.timer · sudo bash deploy/host-profile-refresh.sh"))
    elif hp.get("refresh_overdue"):
        out["active"].add(KEY_OVERDUE)
        out["raise"].append((KIND_OVERDUE, "warning", KEY_OVERDUE,
                             f"host profile refresh overdue — {hp.get('age_hours')} h old (timer every 6 h; unverified at 24 h): "
                             "check systemctl status stoic-host-profile.timer / journalctl -u stoic-host-profile.service"))
    return out


async def evaluate(db, now: datetime | None = None, *, raise_alert) -> tuple[set, int]:
    from host_profile import host_profile
    p = plan(host_profile(now=now or datetime.now(timezone.utc)))
    raised = 0
    for kind, severity, key, text in p["raise"]:
        if await raise_alert(db, kind, severity, text, dedup_key=key):
            raised += 1
    return p["active"], raised
