"""Broker-server alias registry (iter-125, correction #5.1).

Broker-server matching must be EXACT against normalized alias groups —
never substring matching ("IC" would match "ICMarkets" AND "NicMarkets").
A configured and a reported server match only when their normalized forms
are equal OR both belong to the same registered alias group.
"""
import re

# Canonical alias groups. Each inner set lists every normalized name that
# refers to the SAME physical broker server. Extend via db.broker_profiles
# (server_names) which is merged in by callers that have a db handle.
ALIAS_GROUPS: list[set[str]] = [
    {"roboforex-pro", "roboforex-prime"},
    {"icmarketssc-live", "icmarkets-live", "icmarketssc-live01",
     "icmarketssc-live02"},
    {"icmarketssc-demo", "icmarkets-demo"},
    {"onequity-live", "onequity-real"},
    {"onequity-demo"},
]


def normalize_server(name) -> str:
    """Lowercase, trim, collapse inner whitespace. NO other mutation —
    'MetaQuotes-Demo' and 'MetaQuotes-Demo2' must stay distinct."""
    s = str(name or "").strip().lower()
    return re.sub(r"\s+", " ", s)


def servers_match(configured, reported, extra_aliases=None) -> bool:
    """Exact normalized equality, or joint membership in one alias group.
    `extra_aliases`: optional iterable of alias iterables (e.g. from
    db.broker_profiles server_names)."""
    a, b = normalize_server(configured), normalize_server(reported)
    if not a or not b:
        return False
    if a == b:
        return True
    groups = list(ALIAS_GROUPS)
    for grp in (extra_aliases or []):
        norm = {normalize_server(x) for x in grp if x}
        if len(norm) > 1:
            groups.append(norm)
    return any(a in g and b in g for g in groups)
