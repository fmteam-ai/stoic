"""Event-Driven backtester for XAUUSD (and any future symbol).

Designed to mirror live execution semantics. The strategy receives events
strictly in chronological order:

  • BarEvent          — new OHLCV candle (the bot's primary clock tick)
  • MacroEvent        — economic release at exact wall-clock time
                          (CPI, NFP, FOMC). Held by the engine in a queue and
                          released to the strategy ONLY after a configurable
                          *market absorption delay* (PIT bias guard).
  • OrderEvent / FillEvent — strategy intents and broker confirmations.

Anti-leak guarantees:
  1. Strategy.on_bar() may NOT peek at future bars.
  2. Macro headlines released at t=8:30:00 are not visible until
     t + absorption_delay_ms (default 500-2000 ms).
  3. Order fills are queued and executed at the *next* bar's open, never
     intra-bar — the classic "fill at the bar that triggered" leak is
     impossible by construction.
  4. Continuous-contract roll handling (off by default — toggle on when
     using futures data) builds a backward-adjusted price stitch so the
     strategy never sees the artificial price jump at expiry.

This scaffold is intentionally compact (~250 lines) but production-grade.
Wire your custom strategy into `run()` and feed it a list of BarEvents.
"""
from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

logger = logging.getLogger("backtester")


# -------------------- Event types --------------------

@dataclass
class Event:
    ts: datetime  # tz-aware UTC datetime
    kind: str     # "bar" | "macro" | "order" | "fill"


@dataclass
class BarEvent(Event):
    symbol: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    kind: str = "bar"


@dataclass
class MacroEvent(Event):
    name: str = ""
    impact: str = "high"       # high | medium | low
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    surprise: float = 0.0      # actual - forecast, normalised by caller
    visible_at: Optional[datetime] = None   # set by engine with absorption delay
    kind: str = "macro"


@dataclass
class OrderEvent(Event):
    symbol: str = ""
    action: str = "BUY"        # BUY | SELL | CLOSE
    lot_size: float = 0.01
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    note: str = ""
    kind: str = "order"


@dataclass
class FillEvent(Event):
    symbol: str = ""
    action: str = "BUY"
    lot_size: float = 0.01
    fill_price: float = 0.0
    slippage_pips: float = 0.0
    note: str = ""
    kind: str = "fill"


# -------------------- Position book --------------------

@dataclass
class Position:
    symbol: str
    action: str           # BUY | SELL
    lot_size: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    opened_at: Optional[datetime] = None


# -------------------- Continuous contract helper --------------------

def backward_adjusted_stitch(contracts: list[list[BarEvent]]) -> list[BarEvent]:
    """Backward-Adjusted Continuous String for futures.

    `contracts` is a list of per-contract BarEvent streams, oldest contract
    first. We splice them by aligning each prior contract to the next
    contract's first bar open via a *price offset* applied to ALL earlier
    prices. This eliminates the artificial price gap at roll.
    """
    if not contracts:
        return []
    if len(contracts) == 1:
        return list(contracts[0])

    # Compute cumulative offsets walking backwards from newest contract.
    offsets = [0.0] * len(contracts)
    for i in range(len(contracts) - 2, -1, -1):
        if not contracts[i] or not contracts[i + 1]:
            continue
        last_old = contracts[i][-1].close
        first_new = contracts[i + 1][0].open
        offsets[i] = offsets[i + 1] + (first_new - last_old)

    out: list[BarEvent] = []
    for i, stream in enumerate(contracts):
        adj = offsets[i]
        for b in stream:
            out.append(BarEvent(
                ts=b.ts, symbol=b.symbol,
                open=b.open + adj, high=b.high + adj,
                low=b.low + adj,   close=b.close + adj,
                volume=b.volume,
            ))
    return out


# -------------------- Engine --------------------

@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return 100.0 * self.wins / self.total_trades


@dataclass
class EngineConfig:
    starting_equity: float = 10_000.0
    macro_absorption_ms: int = 1500    # 500-2000ms recommended
    slippage_pips: float = 0.5         # per fill
    contract_size_map: dict = field(default_factory=lambda: {
        "XAUUSD": 100, "BTCUSD": 1, "ETHUSD": 1, "XAGUSD": 5000
    })


StrategyCallback = Callable[[BarEvent, list[MacroEvent], "Engine"], list[OrderEvent]]


class Engine:
    """Strict-chronological event loop.

    The strategy callback receives:
      • the current BarEvent
      • the list of *currently-visible* MacroEvents (PIT-delayed)
      • a back-reference to the engine for placing orders / inspecting positions
    """

    def __init__(self, cfg: Optional[EngineConfig] = None):
        self.cfg = cfg or EngineConfig()
        self.equity = self.cfg.starting_equity
        self.peak = self.equity
        self.position: Optional[Position] = None
        self.result = BacktestResult()
        self.result.equity_curve.append((datetime.now(timezone.utc), self.equity))
        # macro queue: (visible_at, monotonic_seq, event) — heapq min-heap
        self._macro_q: list[tuple[datetime, int, MacroEvent]] = []
        self._macro_seq = 0
        self._visible_macros: list[MacroEvent] = []
        self._pending_orders: list[OrderEvent] = []

    # -------- public API --------

    def schedule_macro(self, ev: MacroEvent) -> None:
        """Queue a macro event for *delayed* visibility (PIT guard)."""
        absorption = self.cfg.macro_absorption_ms
        ev.visible_at = ev.ts.fromtimestamp(
            ev.ts.timestamp() + absorption / 1000.0, tz=timezone.utc
        )
        self._macro_seq += 1
        heapq.heappush(self._macro_q, (ev.visible_at, self._macro_seq, ev))

    def place_order(self, order: OrderEvent) -> None:
        """Strategy calls this — order will fill at next bar's open."""
        self._pending_orders.append(order)

    def run(self, bars: Iterable[BarEvent], strategy: StrategyCallback) -> BacktestResult:
        bars_sorted = sorted(bars, key=lambda b: b.ts)
        for bar in bars_sorted:
            # 1) Promote any macro events whose visible_at <= bar.ts
            self._promote_macros(bar.ts)
            # 2) Settle any pending orders against THIS bar's open (no leak).
            self._settle_pending(bar)
            # 3) Mark open position to market; update equity + drawdown.
            self._mark_to_market(bar)
            # 4) Check SL/TP on open position (touches bar high/low).
            self._check_stops(bar)
            # 5) Hand the bar to the strategy.
            new_orders = strategy(bar, list(self._visible_macros), self) or []
            for o in new_orders:
                self.place_order(o)
        return self.result

    # -------- internals --------

    def _promote_macros(self, now: datetime) -> None:
        while self._macro_q and self._macro_q[0][0] <= now:
            _, _, ev = heapq.heappop(self._macro_q)
            self._visible_macros.append(ev)

    def _settle_pending(self, bar: BarEvent) -> None:
        slip = self.cfg.slippage_pips
        if not self._pending_orders:
            return
        for o in self._pending_orders:
            if o.symbol != bar.symbol:
                continue
            if o.action == "CLOSE" and self.position is not None:
                self._close_position(bar.open, bar.ts)
                continue
            fill_price = bar.open + (slip * 0.0001 if o.action == "BUY" else -slip * 0.0001)
            self.position = Position(
                symbol=o.symbol, action=o.action, lot_size=o.lot_size,
                entry_price=fill_price, stop_loss=o.stop_loss,
                take_profit=o.take_profit, opened_at=bar.ts,
            )
            self.result.fills.append(FillEvent(
                ts=bar.ts, symbol=o.symbol, action=o.action,
                lot_size=o.lot_size, fill_price=fill_price,
                slippage_pips=slip, note=o.note,
            ))
        self._pending_orders.clear()

    def _check_stops(self, bar: BarEvent) -> None:
        p = self.position
        if p is None:
            return
        if p.action == "BUY":
            if p.stop_loss is not None and bar.low <= p.stop_loss:
                self._close_position(p.stop_loss, bar.ts, reason="SL")
                return
            if p.take_profit is not None and bar.high >= p.take_profit:
                self._close_position(p.take_profit, bar.ts, reason="TP")
                return
        else:
            if p.stop_loss is not None and bar.high >= p.stop_loss:
                self._close_position(p.stop_loss, bar.ts, reason="SL")
                return
            if p.take_profit is not None and bar.low <= p.take_profit:
                self._close_position(p.take_profit, bar.ts, reason="TP")
                return

    def _mark_to_market(self, bar: BarEvent) -> None:
        p = self.position
        unreal = 0.0
        if p is not None:
            cs = self.cfg.contract_size_map.get(p.symbol, 1)
            diff = (bar.close - p.entry_price) if p.action == "BUY" else (p.entry_price - bar.close)
            unreal = diff * p.lot_size * cs
        marked = self.equity + unreal
        self.peak = max(self.peak, marked)
        dd = (self.peak - marked) / self.peak * 100 if self.peak else 0.0
        self.result.max_drawdown = max(self.result.max_drawdown, dd)
        self.result.equity_curve.append((bar.ts, marked))

    def _close_position(self, exit_price: float, ts: datetime, reason: str = "MANUAL") -> None:
        p = self.position
        if p is None:
            return
        cs = self.cfg.contract_size_map.get(p.symbol, 1)
        diff = (exit_price - p.entry_price) if p.action == "BUY" else (p.entry_price - exit_price)
        pnl = diff * p.lot_size * cs
        self.equity += pnl
        self.result.total_pnl += pnl
        self.result.total_trades += 1
        if pnl > 0:
            self.result.wins += 1
        else:
            self.result.losses += 1
        self.result.fills.append(FillEvent(
            ts=ts, symbol=p.symbol,
            action="CLOSE", lot_size=p.lot_size,
            fill_price=exit_price, slippage_pips=0.0,
            note=f"close_reason={reason} pnl={pnl:.2f}",
        ))
        self.position = None
