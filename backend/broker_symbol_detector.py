"""Broker symbol-suffix auto-detector (iter-76).

When the MT5 EA reports its MarketWatch instrument list on heartbeat,
this module infers the broker's symbol-naming convention so the bot can
route trades correctly *without* the user manually configuring it.

Pure functions, no I/O — fully unit-testable. The heartbeat handler in
`bridge_routes.py` is the sole caller.

Algorithm
---------
1. Take the broker's `available_symbols` list (e.g.
   ["XAUUSD.fx", "EURUSD.fx", "BTCUSD.fx", "USDJPY.fx", "USOIL"]).
2. For each KNOWN_BASE, find candidates whose stripped form == the base.
3. Tally the suffix used per base (case-preserving).
4. The broker's convention is the suffix that appears most often across
   bases. Ties broken by:
     · longest suffix (more specific → safer)
     · alphabetical (deterministic)
5. If only one base matches, return that suffix anyway (it's the only
   evidence we have).
6. If NO base matches anything → return "" (bare names, no suffix).

The detector is conservative on purpose: it only matches symbols whose
"core" exactly equals a known base. It does NOT try to invent aliases
(e.g. "GOLD" → "XAUUSD") — that would need a separate alias map.
"""
from __future__ import annotations

from collections import Counter

# Bases the bot trades or might trade. We grep MarketWatch for these.
# Order matters: longer/more-specific bases first so they win prefix-match.
KNOWN_BASES = (
    "XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD",
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY",
    "USOIL", "UKOIL", "WTI", "BRENT",
    "US30", "NAS100", "SPX500", "GER40", "UK100", "JPN225",
)

# iter-92 · Broker-specific aliases. Some brokers (OnEquity, IC Markets Raw,
# Pepperstone Razor, etc.) publish gold as `GOLD#`/`GOLD.m` instead of the
# CME-convention `XAUUSD`. STOIC internally always uses `XAUUSD` as the
# canonical base for gold; this map lets `resolve_broker_symbol` accept the
# broker's naming without the user having to configure aliases per-account.
#   canonical_base → tuple of accepted alias prefixes (case-insensitive)
BASE_ALIASES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("XAUUSD", "GOLD"),
    "XAGUSD": ("XAGUSD", "SILVER"),
    # iter-43 · Equity index CFDs — broker naming varies wildly.
    "US30":   ("US30", "DJ30", "WS30", "DOW30", "DJI30"),
    "NAS100": ("NAS100", "USTEC", "US100", "NDX100", "USTECH"),
}


def _aliases_for(base: str) -> tuple[str, ...]:
    """Return every acceptable prefix for a canonical base (base itself + aliases)."""
    return BASE_ALIASES.get(base.upper(), (base.upper(),))

# Maximum total suffix length we'll trust. Anything longer is probably
# a totally different instrument (e.g. "XAUUSDmicro_eur_cross_v2").
MAX_SUFFIX_LEN = 6


def _split_base_suffix(name: str) -> tuple[str, str] | None:
    """If `name` starts with a known base, return (base, suffix). Else None.
    Suffix may be empty (bare match). Suffix is rejected if it's longer
    than MAX_SUFFIX_LEN or contains a slash/space (not a real suffix)."""
    if not name:
        return None
    upper = name.upper()
    for base in KNOWN_BASES:
        if upper.startswith(base):
            suffix = name[len(base):]
            if len(suffix) > MAX_SUFFIX_LEN:
                continue
            if any(c in suffix for c in (" ", "/", "\\")):
                continue
            return base, suffix
    return None


def infer_broker_suffix(available_symbols: list[str]) -> dict:
    """Return {
        'suffix': str,                  # broker's inferred suffix ('' = bare)
        'confidence': float,            # 0.0-1.0, share of matches agreeing
        'matched_bases': [str, ...],    # which bases supported the inference
        'matched_symbols': [str, ...],  # actual matched symbol names
        'sample_size': int,             # how many MarketWatch symbols we scanned
    }
    """
    if not available_symbols:
        return {"suffix": "", "confidence": 0.0,
                "matched_bases": [], "matched_symbols": [],
                "sample_size": 0}

    matches: list[tuple[str, str, str]] = []  # (base, suffix, original_name)
    for name in available_symbols:
        if not isinstance(name, str):
            continue
        hit = _split_base_suffix(name.strip())
        if hit:
            matches.append((hit[0], hit[1], name.strip()))

    if not matches:
        return {"suffix": "", "confidence": 0.0,
                "matched_bases": [], "matched_symbols": [],
                "sample_size": len(available_symbols)}

    # Tally suffix occurrences across DISTINCT bases (so a broker offering
    # two XAUUSD variants doesn't unfairly tilt the vote).
    by_suffix_bases: dict[str, set[str]] = {}
    by_suffix_examples: dict[str, list[str]] = {}
    for base, suffix, original in matches:
        by_suffix_bases.setdefault(suffix, set()).add(base)
        by_suffix_examples.setdefault(suffix, []).append(original)

    # Score: # of distinct bases supporting this suffix.
    # Tie-break: longer suffix (more specific), then alphabetical.
    def _key(suffix: str) -> tuple:
        return (-len(by_suffix_bases[suffix]), -len(suffix), suffix)
    winner = sorted(by_suffix_bases.keys(), key=_key)[0]

    total_distinct_bases = len({m[0] for m in matches})
    winner_bases = by_suffix_bases[winner]
    confidence = round(len(winner_bases) / max(1, total_distinct_bases), 3)

    return {
        "suffix": winner,
        "confidence": confidence,
        "matched_bases": sorted(winner_bases),
        "matched_symbols": sorted(set(by_suffix_examples[winner]))[:10],
        "sample_size": len(available_symbols),
    }


def resolve_broker_symbol(
    base_symbol: str,
    available_symbols: list[str] | None,
    suffix_fallback: str = "",
) -> str | None:
    """Resolve the exact broker ticker for a base symbol.

    The "one suffix per broker" model breaks on mixed-convention brokers
    (e.g. VTMarkets: forex pairs are bare `EURUSD`, but gold is
    `XAUUSD-ECN`). Per-base resolution looks at the actual MarketWatch
    list and picks the entry whose stripped form matches `base_symbol`.

    Args:
        base_symbol: STOIC-internal symbol (e.g. "XAUUSD", "BTCUSD").
        available_symbols: The broker's MarketWatch list as reported by
            the EA. May be None/empty when the EA hasn't reported yet.
        suffix_fallback: If `available_symbols` is empty, fall back to
            the broker-wide auto-detected suffix.

    Returns:
        The exact broker ticker to send to the broker (e.g. "XAUUSD-ECN",
        "XAUUSD.fx", "XAUUSD#"), OR `None` if the base isn't offered by
        the broker (caller should NOT send the order — execution will
        fail with symbol_not_found).
    """
    if not base_symbol:
        return None
    base_upper = base_symbol.upper()
    accepted_prefixes = _aliases_for(base_upper)

    # Path 1: per-base lookup using actual MarketWatch inventory (preferred)
    if available_symbols:
        # Prefer the BEST match: shortest tail (so plain `XAUUSD` beats
        # `XAUUSDmicro` if both exist); then alphabetical for determinism.
        # iter-92: also accept broker-alias prefixes (GOLD# for XAUUSD, etc.)
        candidates: list[tuple[int, str]] = []
        for name in available_symbols:
            if not isinstance(name, str):
                continue
            stripped = name.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            for prefix in accepted_prefixes:
                if upper.startswith(prefix):
                    suffix = stripped[len(prefix):]
                    if len(suffix) <= MAX_SUFFIX_LEN and not any(
                        c in suffix for c in (" ", "/", "\\")
                    ):
                        # canonical-prefix matches score better than alias
                        # matches, so 'XAUUSD' beats 'GOLD' when both exist
                        alias_penalty = 0 if prefix == base_upper else 1
                        candidates.append(
                            (alias_penalty, len(suffix), stripped)
                        )
                        break
        if candidates:
            candidates.sort()
            return candidates[0][2]
        # MarketWatch is known AND base not in it → broker doesn't offer it.
        return None

    # Path 2: no MarketWatch data yet — best-effort using broker-wide suffix
    return base_symbol + (suffix_fallback or "")
