"""Formal versioned feature contract (review item 5).

Every decision and every trained model artifact is stamped with
feature_schema_version. Training only consumes decisions whose schema matches
the current version, and persisted models with a mismatched schema are never
loaded — eliminating training/serving drift when the feature set evolves.

Adding/removing/reordering features = new FeatureSchemaVN entry + bump
FEATURE_SCHEMA_VERSION. NEVER mutate an existing schema tuple.
"""

FEATURE_SCHEMA_VERSION = 1

FEATURE_SCHEMAS: dict[int, tuple] = {
    1: (
        "ret_1s", "ret_3s", "ret_5s", "ret_10s", "ret_30s",
        "accel", "vwap_dist", "vol_short", "vol_long",
        "tick_rate", "spread_pips", "spread_pctl",
        "uptick_ratio", "time_since_change_s",
    ),
}


def keys_for(version: int | None) -> tuple | None:
    """Feature key order for a schema version (None → unknown version)."""
    try:
        return FEATURE_SCHEMAS.get(int(version or 1))
    except (TypeError, ValueError):
        return None


def current_keys() -> tuple:
    return FEATURE_SCHEMAS[FEATURE_SCHEMA_VERSION]
