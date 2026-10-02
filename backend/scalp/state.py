"""Scalp subsystem · Step 3 — in-memory rolling tick state.

Everything here is O(1)-ish per tick. NO database reads, NO LLM calls,
NO indicator-history recomputation on the hot path.
"""
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TickEvent:
    symbol: str
    broker_time_ms: int      # MT5 time_msc (broker/server quote time)
    received_time_ms: int    # local arrival (backend clock)
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None
    source_sequence: int | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


class ScalpState:
    """Rolling per-(account, symbol) microstructure state."""

    def __init__(self, pip_size: float):
        self.pip = pip_size
        # (broker_ms, bid, ask, mid)
        self.ticks: deque = deque(maxlen=3000)
        self.spreads: deque = deque(maxlen=900)     # pips, session proxy
        self.vols: deque = deque(maxlen=600)        # vol_short history (pctl)
        self.last_tick: TickEvent | None = None
        self.last_price_change_ms: int = 0
        self.vwap_ewma: float = 0.0                 # tick-volume-less mid EWMA
        self._vwap_alpha = 0.02
        # execution quality (Step 3 "recent order-fill quality")
        self.slippage_ewma_pips: float = 0.0
        self.fills_seen: int = 0
        self.rejects_recent: deque = deque(maxlen=50)   # (ms, 0|1)
        # clock offset broker vs local (ms): ROBUST median over recent
        # TRUSTED ticks only — a persistently delayed stream must never
        # drag the offset and make old data look current (round 4 item 3).
        self._offsets: deque = deque(maxlen=60)
        self.clock_drift_ms: float = 0.0
        # newest batch's TZ-snapped recv−sent_at residual (iter-175): lets
        # the kill-switch flag an unusable broker clock even when samples
        # are rejected and the median never forms.
        self.last_batch_residual_ms: float | None = None
        # True while the EA reports sent_gmt_ms (its own UTC send time):
        # drift samples then EXCLUDE transport (sent_gmt − sent_at instead
        # of recv − sent_at), so steady transport lag is no longer absorbed
        # into the offset and shows up in broker_adjusted_age_ms.
        self.gmt_clock: bool = False
        self.last_transport_ms: float | None = None
        self.last_ea_tick_age_ms: float | None = None

    def update(self, t: TickEvent, trusted: bool = True) -> None:
        mid = (t.bid + t.ask) / 2.0
        prev = self.ticks[-1] if self.ticks else None
        self.ticks.append((t.broker_time_ms, t.bid, t.ask, mid))
        self.spreads.append((t.ask - t.bid) / self.pip)
        if prev is None or mid != prev[3]:
            self.last_price_change_ms = t.broker_time_ms
        if self.vwap_ewma == 0.0:
            self.vwap_ewma = mid
        else:
            self.vwap_ewma += self._vwap_alpha * (mid - self.vwap_ewma)
        self.last_tick = t

    def record_offset_sample(self, off: float) -> None:
        """One drift sample per BATCH against the batch's newest tick
        (recv − sent_at): per-tick sampling biased the median by the
        in-batch tick age (iter-175). Sudden large jumps are rejected once
        the estimate is stable."""
        if len(self._offsets) < 10 or abs(off - self.clock_drift_ms) < 10_000:
            self._offsets.append(off)
            s = sorted(self._offsets)
            self.clock_drift_ms = float(s[len(s) // 2])

    def set_clock_mode(self, gmt_clock: bool) -> None:
        """Switching between legacy (recv − sent_at) and EA-UTC
        (sent_gmt − sent_at) samples changes what the offset means — never
        mix the two populations in one median."""
        if bool(gmt_clock) != self.gmt_clock:
            self._offsets.clear()
            self.clock_drift_ms = 0.0
            self.gmt_clock = bool(gmt_clock)

    # -------- execution feedback --------
    def record_fill(self, slippage_pips: float) -> None:
        self.fills_seen += 1
        a = 0.2 if self.fills_seen > 5 else 0.5
        self.slippage_ewma_pips += a * (abs(slippage_pips) - self.slippage_ewma_pips)
        self.rejects_recent.append((now_ms(), 0))

    def record_reject(self) -> None:
        self.rejects_recent.append((now_ms(), 1))

    def reject_rate(self) -> float:
        if not self.rejects_recent:
            return 0.0
        return sum(r for _, r in self.rejects_recent) / len(self.rejects_recent)

    # -------- basic reads --------
    def mid(self) -> float | None:
        return self.ticks[-1][3] if self.ticks else None

    def spread_pips(self) -> float | None:
        return self.spreads[-1] if self.spreads else None

    def quote_age_ms(self) -> int:
        if self.last_tick is None:
            return 1 << 30
        # age vs local clock, corrected by measured broker/local drift
        return max(0, now_ms() - self.last_tick.received_time_ms)

    def broker_adjusted_age_ms(self) -> int:
        """Age of the newest BROKER-MARKET timestamp, corrected by the
        measured clock offset. Guards against delayed batches whose local
        receipt time looks fresh."""
        if self.last_tick is None:
            return 1 << 30
        return max(0, int(now_ms() - self.last_tick.broker_time_ms
                          - self.clock_drift_ms))
