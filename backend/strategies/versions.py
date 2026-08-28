"""Strategy version pinning (v62.1) — live money is NEVER assigned
'latest'. An assignment pins strategy_id + exact version + config hash;
a new version must pass Validation → Replay → Shadow → Demo →
Certification → Promotion before any PAMM may migrate."""
from strategies.registry import REGISTRY, get_strategy, strategy_hash

PROMOTION_PIPELINE = ["VALIDATION", "REPLAY", "SHADOW", "DEMO",
                      "CERTIFICATION", "PROMOTION"]


def current_version(strategy_id: str) -> str | None:
    d = get_strategy(strategy_id)
    return d.version if d else None


def current_hash(strategy_id: str) -> str | None:
    d = get_strategy(strategy_id)
    return strategy_hash(d) if d else None


def version_pin_valid(strategy_id: str, version: str,
                      pinned_hash: str) -> dict:
    """An assignment is only executable when its pinned version AND hash
    still match the registry — any drift means the strategy artifact
    changed underneath the assignment."""
    d = get_strategy(strategy_id)
    if not d:
        return {"ok": False, "reason": "unknown_strategy"}
    if version != d.version:
        return {"ok": False, "reason": "version_drift",
                "assigned": version, "registry": d.version}
    if pinned_hash != strategy_hash(d):
        return {"ok": False, "reason": "hash_drift"}
    return {"ok": True}


def all_versions() -> dict:
    return {sid: {"version": d.version, "hash": strategy_hash(d)}
            for sid, d in REGISTRY.items()}
