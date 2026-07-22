"""Phase E · Market Data Layer — tick validation, missing-tick detection,
session quality metrics, feed health and spread anomaly tracking.

The best model cannot compensate for poor market data: every inbound tick is
validated BEFORE it can touch features/decisions, gaps and anomalies are
measured continuously, and a rolling session quality score feeds the kill
switch (POOR data blocks NEW entries; closing is always allowed).
"""
from collections import deque

MAX_SPREAD_FRAC = 0.05         # spread > 5% of price = broken quote
JUMP_SUSPECT_FRAC = 0.02       # mid jump > 2% tick-to-tick = suspect
GAP_ALERT_MS = 10_000          # >10s without a tick inside a live session
SESSION_BREAK_MS = 1_800_000   # >30min gap = session break, not a data gap
WINDOW = 2000                  # rolling ticks kept for quality metrics

GOOD = "GOOD"
DEGRADED = "DEGRADED"
POOR = "POOR"


def validate_tick(bid: float, ask: float,
                  last_mid: float | None = None) -> dict:
    """Hard validation (drop) + soft anomaly flags (accept but count).
    Returns {ok, reason, suspect}."""
    try:
        bid = float(bid)
        ask = float(ask)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "unparseable", "suspect": False}
    if bid <= 0 or ask <= 0:
        return {"ok": False, "reason": "non_positive", "suspect": False}
    if ask < bid:
        return {"ok": False, "reason": "inverted", "suspect": False}
    mid = (bid + ask) / 2.0
    if (ask - bid) / mid > MAX_SPREAD_FRAC:
        return {"ok": False, "reason": "absurd_spread", "suspect": False}
    if last_mid and abs(mid - last_mid) / last_mid > JUMP_SUSPECT_FRAC:
        # a real flash move must not be dropped — flag it, count it, and let
        # the quality score degrade if these cluster
        return {"ok": True, "reason": "price_jump", "suspect": True}
    return {"ok": True, "reason": None, "suspect": False}


class DataQualityMonitor:
    """Rolling per-symbol session quality: counts every validation outcome,
    inter-tick gaps and spread anomalies; emits a 0-100 score."""

    def __init__(self):
        self.gaps = deque(maxlen=WINDOW)         # broker-time gap ms
        self.spreads = deque(maxlen=WINDOW)      # pips
        self.accepted = 0
        self.invalid = 0
        self.suspect = 0
        self.out_of_order = 0
        self.gap_events = 0                      # gaps > GAP_ALERT_MS
        self.max_gap_ms = 0
        self._last_tm: int | None = None

    def record_invalid(self, reason: str):       # noqa: ARG002
        self.invalid += 1

    def record_out_of_order(self):
        self.out_of_order += 1

    def record_tick(self, tm: int, spread_pips: float,
                    suspect: bool = False):
        if self._last_tm is not None:
            gap = tm - self._last_tm
            if 0 < gap < SESSION_BREAK_MS:       # session breaks don't count
                self.gaps.append(gap)
                if gap > GAP_ALERT_MS:
                    self.gap_events += 1
                    self.max_gap_ms = max(self.max_gap_ms, gap)
        self._last_tm = tm
        self.spreads.append(float(spread_pips))
        self.accepted += 1
        if suspect:
            self.suspect += 1

    def snapshot(self) -> dict:
        """Session quality metrics + composite score (0-100) + rating."""
        total = max(1, self.accepted + self.invalid)
        invalid_ratio = self.invalid / total
        suspect_ratio = self.suspect / max(1, self.accepted)
        ooo_ratio = self.out_of_order / total
        gaps = list(self.gaps)
        gap_ratio = (sum(1 for g in gaps if g > GAP_ALERT_MS)
                     / max(1, len(gaps)))
        sp = sorted(self.spreads)
        med_spread = sp[len(sp) // 2] if sp else None
        p95_spread = sp[int(len(sp) * 0.95)] if len(sp) >= 20 else None
        spread_anomaly_ratio = 0.0
        if med_spread and len(sp) >= 20:
            spread_anomaly_ratio = (sum(1 for s in self.spreads
                                        if s > 3 * med_spread)
                                    / len(self.spreads))
        score = 100.0
        score -= min(40.0, invalid_ratio * 400)      # 10% invalid = -40
        score -= min(20.0, ooo_ratio * 200)
        score -= min(20.0, gap_ratio * 100)          # 20% alert gaps = -20
        score -= min(10.0, suspect_ratio * 100)
        score -= min(10.0, spread_anomaly_ratio * 100)
        score = round(max(0.0, score), 1)
        rating = GOOD if score >= 80 else (DEGRADED if score >= 60 else POOR)
        return {"score": score, "rating": rating,
                "ticks_accepted": self.accepted,
                "ticks_invalid": self.invalid,
                "ticks_suspect": self.suspect,
                "out_of_order": self.out_of_order,
                "gap_events": self.gap_events,
                "max_gap_ms": self.max_gap_ms,
                "median_gap_ms": (sorted(gaps)[len(gaps) // 2]
                                  if gaps else None),
                "median_spread_pips": (round(med_spread, 2)
                                       if med_spread is not None else None),
                "p95_spread_pips": (round(p95_spread, 2)
                                    if p95_spread is not None else None),
                "spread_anomaly_ratio": round(spread_anomaly_ratio, 4)}
