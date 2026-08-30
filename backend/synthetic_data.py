"""Synthetic-data isolation (review P1-6/7) — QA, chaos-drill and test
artifacts must never contaminate operator health, alert history or
readiness views. Alerts carry a persisted `synthetic` flag; operator
surfaces default to scope=real."""

SYNTHETIC_MARKERS = ("chaos_", "drill_", "synthetic_", "test_", "qa_",
                     "smoke_")

# audit v5 P1-3 — known QA/test identities that leak into operator alerts
SYNTHETIC_LABELS = {"fresh", "compat", "spread-test"}
SYNTHETIC_LABEL_PREFIXES = ("test_", "test-", "tmp_", "qa_", "paper#",
                            "chaos_", "drill_", "synthetic_", "smoke_")

SCOPES = ("real", "synthetic", "all")


def is_synthetic_label(label) -> bool:
    lab = str(label or "").strip().lower()
    return lab in SYNTHETIC_LABELS or lab.startswith(SYNTHETIC_LABEL_PREFIXES)


def mentions_synthetic_identity(message: str, meta: dict | None = None) -> bool:
    """True when an alert message/meta references a known test identity."""
    if meta and is_synthetic_label(meta.get("account_label")):
        return True
    import re
    for tok in re.split(r"[^A-Za-z0-9_#-]+", str(message or "")):
        if tok and is_synthetic_label(tok):
            return True
    return False


def is_synthetic_account(acc: dict) -> bool:
    if acc.get("synthetic") is True:
        return True
    label = str(acc.get("label") or "").lower()
    uid = str(acc.get("user_id") or "").lower()
    return (label.startswith(SYNTHETIC_MARKERS)
            or is_synthetic_label(label)
            or uid.startswith(SYNTHETIC_MARKERS))


def alert_scope_filter(scope: str) -> dict:
    """Mongo filter fragment for ops_alerts by scope (default real)."""
    scope = (scope or "real").lower()
    if scope == "synthetic":
        return {"synthetic": True}
    if scope == "all":
        return {}
    return {"synthetic": {"$ne": True}}
