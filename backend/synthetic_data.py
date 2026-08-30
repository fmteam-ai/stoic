"""Synthetic-data isolation (review P1-6/7) — QA, chaos-drill and test
artifacts must never contaminate operator health, alert history or
readiness views. Alerts carry a persisted `synthetic` flag; operator
surfaces default to scope=real."""

SYNTHETIC_MARKERS = ("chaos_", "drill_", "synthetic_", "test_", "qa_",
                     "smoke_")

SCOPES = ("real", "synthetic", "all")


def is_synthetic_account(acc: dict) -> bool:
    if acc.get("synthetic") is True:
        return True
    label = str(acc.get("label") or "").lower()
    uid = str(acc.get("user_id") or "").lower()
    return (label.startswith(SYNTHETIC_MARKERS)
            or uid.startswith(SYNTHETIC_MARKERS))


def alert_scope_filter(scope: str) -> dict:
    """Mongo filter fragment for ops_alerts by scope (default real)."""
    scope = (scope or "real").lower()
    if scope == "synthetic":
        return {"synthetic": True}
    if scope == "all":
        return {}
    return {"synthetic": {"$ne": True}}
