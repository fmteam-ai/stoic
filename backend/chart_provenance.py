"""Shared chart provenance contract (audit r28 P2-05). Every financial series
the UI draws carries: provider, source kind, as-of time (UTC), freshness,
missing intervals, cache/fallback status, environment and the ledger /
reconciliation id it was derived from — so simulated, indicative and
broker-reconciled series are never visually interchangeable."""
import os
from datetime import datetime, timezone

CONTRACT_VERSION = 1
SOURCE_KINDS = ("broker_reconciled", "indicative", "simulated", "derived")


def _ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), tz=timezone.utc)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _weekend_only(a: datetime, b: datetime) -> bool:
    from datetime import timedelta
    d = (a + timedelta(days=1)).date()
    while d < b.date():
        if d.weekday() < 5:
            return False
        d += timedelta(days=1)
    return True


def build(*, provider: str, source_kind: str, points: list | None = None, time_key: str = "date",
          expected_interval_s: int = 86400, as_of=None, cache_status: str = "live",
          ledger_id: str | None = None, reconciliation_id: str | None = None,
          note: str | None = None) -> dict:
    assert source_kind in SOURCE_KINDS, source_kind
    now = datetime.now(timezone.utc)
    stamps = sorted(t for t in (_ts((p or {}).get(time_key)) for p in (points or []) if isinstance(p, dict)) if t)
    last = _ts(as_of) or (stamps[-1] if stamps else None)
    freshness = int((now - last).total_seconds()) if last else None
    gaps = []
    for a, b in zip(stamps, stamps[1:]):
        delta = (b - a).total_seconds()
        if delta > expected_interval_s * 1.5:
            if expected_interval_s >= 86400 and _weekend_only(a, b):
                continue    # markets closed Sat/Sun — not missing data
            gaps.append({"from": a.isoformat(), "to": b.isoformat(), "missing": int(delta // expected_interval_s) - 1})
    return {
        "contract_version": CONTRACT_VERSION,
        "provider": provider,
        "source_kind": source_kind,
        "as_of": last.isoformat() if last else None,
        "timezone": "UTC",
        "freshness_s": freshness,
        "stale": bool(freshness is not None and freshness > expected_interval_s * 2),
        "expected_interval_s": expected_interval_s,
        "points": len(stamps),
        "missing_intervals": gaps[:10],
        "missing_intervals_count": len(gaps),
        "cache_status": cache_status,
        "environment": (os.environ.get("APP_ENV") or "development").lower(),
        "ledger_id": ledger_id,
        "reconciliation_id": reconciliation_id,
        "note": note,
    }
