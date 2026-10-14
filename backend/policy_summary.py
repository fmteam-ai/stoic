"""P1-04 (external audit 14 Oct) — ONE place that names the active trading policy for the UI and status probes."""


async def active_policy_summary(db) -> dict:
    """{policy_version, demo_only, expires_at, expired, scope, label} from the signed rules policy (platform_state)."""
    try:
        from rules_policy import active
        pol = await active(db)
    except Exception:  # noqa: BLE001
        pol = None
    if not pol:
        return {"policy_version": None, "demo_only": True, "expires_at": None, "expired": False,
                "scope": "none", "label": "NO ACTIVE POLICY · close-only"}
    demo_only = bool(pol.get("demo_only", True))
    scope = "demo_only" if demo_only else "live_capital"
    label = f"{pol.get('policy_version')} · {'DEMO-ONLY' if demo_only else 'LIVE CAPITAL'}"
    if pol.get("expired"):
        label += " · EXPIRED"
    return {"policy_version": pol.get("policy_version"), "demo_only": demo_only,
            "expires_at": pol.get("expires_at"), "expired": bool(pol.get("expired")),
            "scope": scope, "label": label,
            "live_target": "6/3/3 identity-bound inventory (future live-capital policy — not active)"}
