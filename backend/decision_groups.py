"""v1.60.37 — mirrored-trade decision grouping.

Each account produces its OWN signal document for the same market decision (OnEquity 13:46:43 and RoboForex 13:46:44
were two signal docs), so grouping by signal_id still counted mirrored trades twice. A decision is therefore
symbol + direction + entry time within ±DECISION_WINDOW_SEC. Greedy clustering on the sorted entry times: a trade joins
the first cluster of the same symbol/direction whose anchor (first entry) is within the window."""
from __future__ import annotations

from datetime import datetime, timezone

from pip_utils import base_symbol

DECISION_WINDOW_SEC = 120


def _ts(v) -> float | None:
    if not v:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def entry_time(doc: dict) -> float | None:
    return _ts(doc.get("opened_at") or doc.get("entry_time") or doc.get("created_at"))


def group_decisions(docs: list, window_sec: int = DECISION_WINDOW_SEC) -> list:
    """Returns a list of clusters (lists of docs, original insertion order inside each cluster)."""
    indexed = sorted(enumerate(docs), key=lambda p: (entry_time(p[1]) or float("inf"), p[0]))
    clusters: list = []
    anchors: dict = {}            # (symbol, action) -> list of (anchor_ts, cluster_index)
    for _, d in indexed:
        key = (base_symbol(d.get("symbol")), d.get("action"))
        ts = entry_time(d)
        placed = False
        if ts is not None:
            for anchor_ts, ci in anchors.get(key, []):
                if abs(ts - anchor_ts) <= window_sec:
                    clusters[ci].append(d)
                    placed = True
                    break
        if not placed:
            clusters.append([d])
            if ts is not None:
                anchors.setdefault(key, []).append((ts, len(clusters) - 1))
    return clusters


def decision_key(cluster: list) -> str:
    d = cluster[0]
    ts = entry_time(d)
    return f"{base_symbol(d.get('symbol'))}|{d.get('action')}|{int(ts) if ts is not None else d.get('_id')}"
