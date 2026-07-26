"""Normalized broker registry (iter-139).

Replaces dynamic broker-name matching with a curated, admin-manageable
registry: server aliases, canonical→broker symbol mappings, contract
specifications, stop/freeze levels and session windows. The execution
path consults it when the EA hasn't reported MarketWatch symbols yet;
live EA-reported specs still win when present (broker_intel).
"""
import time
import logging
from datetime import datetime, timezone

from broker_servers import normalize_server

logger = logging.getLogger("broker_registry")

REGISTRY_SEED_VERSION = 1

_GOLD_SPEC = {"contract_size": 100, "digits": 2, "min_lot": 0.01,
              "lot_step": 0.01}
_BTC_SPEC = {"contract_size": 1, "digits": 2, "min_lot": 0.01,
             "lot_step": 0.01}
_METAL_SESSIONS = [{"open_utc": "22:05", "close_utc": "21:55",
                    "days": "Sun-Fri", "note": "daily break 21:55-22:05 UTC"}]

SEED_BROKERS = [
    {"broker_id": "roboforex", "name": "RoboForex",
     "server_aliases": ["RoboForex-ECN", "RoboForex-Pro", "RoboForex-Demo"],
     "symbol_map": {"XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 10, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "icmarkets", "name": "IC Markets",
     "server_aliases": ["ICMarkets-Live01", "ICMarkets-Live02",
                        "ICMarkets-Live03", "ICMarkets-Demo",
                        "ICMarketsSC-Live", "ICMarketsSC-Demo"],
     "symbol_map": {"XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 0, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "pepperstone", "name": "Pepperstone",
     "server_aliases": ["Pepperstone-Live01", "Pepperstone-Live02",
                        "Pepperstone-Demo", "Pepperstone-Edge"],
     "symbol_map": {"XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 0, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "exness", "name": "Exness",
     "server_aliases": ["Exness-MT5Real", "Exness-MT5Real4",
                        "Exness-MT5Real8", "Exness-MT5Trial"],
     "symbol_map": {"XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSDm"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 0, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "ftmo", "name": "FTMO",
     "server_aliases": ["FTMO-Server", "FTMO-Demo", "FTMO-Server2"],
     "symbol_map": {"XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 0, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "xm", "name": "XM Global",
     "server_aliases": ["XMGlobal-MT5", "XMGlobal-MT5 2", "XMGlobal-MT5 5",
                        "XMGlobal-Demo MT5"],
     "symbol_map": {"XAUUSD": "GOLD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 5, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "vtmarkets", "name": "VT Markets",
     "server_aliases": ["VTMarkets-Live", "VTMarkets-Live 2",
                        "VTMarkets-Demo"],
     "symbol_map": {"XAUUSD": "XAUUSD-ECN", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 0, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
    {"broker_id": "fxtm", "name": "FXTM",
     "server_aliases": ["ForexTimeFXTM-Live", "ForexTimeFXTM-Demo"],
     "symbol_map": {"XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD"},
     "contract_specs": {"XAUUSD": _GOLD_SPEC, "BTCUSD": _BTC_SPEC},
     "stop_level_points": 10, "freeze_level_points": 0,
     "sessions": _METAL_SESSIONS},
]


async def ensure_seed(db) -> int:
    """Idempotent: inserts seed brokers that don't exist yet; never
    overwrites admin edits (matched by broker_id)."""
    inserted = 0
    for b in SEED_BROKERS:
        existing = await db.broker_registry.find_one(
            {"broker_id": b["broker_id"]})
        if existing:
            continue
        doc = dict(b)
        doc["aliases_normalized"] = [normalize_server(a)
                                     for a in b["server_aliases"]]
        doc["seed_version"] = REGISTRY_SEED_VERSION
        doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await db.broker_registry.insert_one(doc)
        inserted += 1
    if inserted:
        logger.info("broker_registry seeded %d brokers", inserted)
    return inserted


# 5-minute in-memory resolve cache (registry changes are rare)
_CACHE: dict = {}
_CACHE_TTL = 300.0


# iter-158 — broker capability profiles: engines consult these instead of
# hard-coded assumptions. Registry entries may override any subset.
DEFAULT_CAPABILITIES = {
    "position_mode": "hedging",      # hedging | netting
    "fill_policy": "IOC",            # IOC | FOK | RETURN
    "partial_fills": True,
    "max_pending_orders": 200,
    "min_lot": 0.01,
    "lot_step": 0.01,
    "supports_stop_limit": True,
    "trailing_stops_server_side": False,
}


def merged_capabilities(entry: dict | None) -> dict:
    caps = dict(DEFAULT_CAPABILITIES)
    if entry:
        overrides = entry.get("capabilities") or {}
        caps.update({k: v for k, v in overrides.items()
                     if k in DEFAULT_CAPABILITIES})
    return caps


async def capabilities_for(db, server_name: str | None) -> dict:
    """Full capability profile for a broker server (defaults when unknown)."""
    entry = await resolve_registry(db, server_name)
    return merged_capabilities(entry)


def invalidate_cache() -> None:
    _CACHE.clear()


async def resolve_registry(db, server_name: str | None) -> dict | None:
    """Match a reported/configured broker server to a registry entry via
    normalized alias comparison (exact, then prefix). The returned entry
    always carries a full `capabilities` block (registry overrides merged
    over defaults) so callers never hard-code broker assumptions."""
    if not server_name:
        return None
    key = normalize_server(server_name)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    entry = await db.broker_registry.find_one({"aliases_normalized": key})
    if not entry:
        async for b in db.broker_registry.find({}):
            if any(key.startswith(a) or a.startswith(key)
                   for a in b.get("aliases_normalized", []) if a):
                entry = b
                break
    if entry:
        entry = dict(entry)
        entry["id"] = str(entry.pop("_id"))
        entry["capabilities"] = merged_capabilities(entry)
    _CACHE[key] = (time.monotonic(), entry)
    return entry


async def registry_symbol_for(server_name: str | None,
                              base_symbol: str) -> str | None:
    """Canonical → broker ticker from the registry, or None."""
    from database import get_db
    entry = await resolve_registry(get_db(), server_name)
    if not entry:
        return None
    mapped = (entry.get("symbol_map") or {}).get(base_symbol)
    return mapped or None

