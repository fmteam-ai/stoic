"""CLI: run an event-driven backtest on the Kalman-smoothed strategy.

Usage:
    python -m backtester.run XAUUSD               # ~365 daily bars
    python -m backtester.run XAUUSD --bars 730
    python -m backtester.run BTCUSD --bars 365

Strategy: a small "smoothed-trend follower" used as a sanity stand-in for the
live AI signal — it goes LONG when Kalman velocity > +threshold, SHORT when
velocity < -threshold, and flat otherwise. SL = 1.5 ATR, TP = 3 ATR.

This is intentionally simple; the real value of this scaffold is in
demonstrating the **no-leak event loop** and the **macro-PIT delay**. To
plug in your live ai_signals.analyze_symbol pipeline, replace `strategy()`
with a function that returns a list of OrderEvent based on the historical
analysis.
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

# Allow `python -m backtester.run` from /app/backend
sys.path.insert(0, "/app/backend")

from backtester.engine import (   # noqa: E402
    Engine, EngineConfig, BarEvent, OrderEvent,
)
from kalman import kalman_smooth  # noqa: E402
from market import get_history     # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("backtest")


def _to_bar(symbol: str, row: dict) -> BarEvent:
    ts = row.get("ts") or row.get("date")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if not isinstance(ts, datetime):
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return BarEvent(
        ts=ts, symbol=symbol,
        open=float(row.get("open", row.get("close", 0))),
        high=float(row.get("high", row.get("close", 0))),
        low=float(row.get("low", row.get("close", 0))),
        close=float(row.get("close", 0)),
        volume=float(row.get("volume", 0)),
    )


def _atr(bars: list[BarEvent], i: int, period: int = 14) -> float:
    """Simple ATR — used to size SL/TP."""
    start = max(1, i - period)
    trs = []
    for j in range(start, i + 1):
        prev_close = bars[j - 1].close
        b = bars[j]
        trs.append(max(b.high - b.low,
                       abs(b.high - prev_close),
                       abs(b.low - prev_close)))
    return sum(trs) / len(trs) if trs else 0.0


def _build_strategy(bars: list[BarEvent], velocity_threshold: float):
    """Returns the StrategyCallback closure with precomputed Kalman state."""
    closes = [b.close for b in bars]
    smoothed = kalman_smooth(closes)
    by_idx = {id(bars[i]): smoothed[i] for i in range(len(bars))}
    bars_index = {id(b): i for i, b in enumerate(bars)}

    def strategy(bar: BarEvent, macros, engine) -> list[OrderEvent]:
        i = bars_index.get(id(bar))
        if i is None or i < 30:
            return []   # warm-up
        k = by_idx[id(bar)]
        velocity = k["k_velocity"]
        # If already in position, hold (SL/TP exits handled by engine).
        if bar.symbol in engine.positions:
            return []
        atr = _atr(bars, i, period=14)
        if atr <= 0:
            return []
        if velocity > velocity_threshold:
            sl = bar.close - 1.5 * atr
            tp = bar.close + 3.0 * atr
            return [OrderEvent(ts=bar.ts, symbol=bar.symbol, action="BUY",
                               lot_size=0.01, stop_loss=sl, take_profit=tp,
                               note=f"k_vel={velocity:.4f}")]
        if velocity < -velocity_threshold:
            sl = bar.close + 1.5 * atr
            tp = bar.close - 3.0 * atr
            return [OrderEvent(ts=bar.ts, symbol=bar.symbol, action="SELL",
                               lot_size=0.01, stop_loss=sl, take_profit=tp,
                               note=f"k_vel={velocity:.4f}")]
        return []
    return strategy


async def _amain() -> int:
    p = argparse.ArgumentParser(description="Event-driven Kalman backtest.")
    p.add_argument("symbol", default="XAUUSD", nargs="?")
    p.add_argument("--bars", type=int, default=365,
                   help="Max number of daily bars to backtest (default 365).")
    p.add_argument("--vel-threshold", type=float, default=0.05,
                   help="Min Kalman velocity (price units / day) to take a trade.")
    p.add_argument("--macro-absorption-ms", type=int, default=1500)
    args = p.parse_args()

    log.info("Fetching history for %s …", args.symbol)
    try:
        history = await get_history(args.symbol)
    except Exception as e:
        log.error("History fetch failed: %s", e)
        return 1
    if not history:
        log.error("No history returned.")
        return 1

    bars = [_to_bar(args.symbol, r) for r in history][-args.bars:]
    log.info("Loaded %d bars from %s to %s", len(bars), bars[0].ts, bars[-1].ts)

    engine = Engine(EngineConfig(macro_absorption_ms=args.macro_absorption_ms))
    strategy = _build_strategy(bars, velocity_threshold=args.vel_threshold)
    res = engine.run(bars, strategy)

    print()
    print("============================================================")
    print(f"  BACKTEST · {args.symbol} · {len(bars)} bars")
    print("============================================================")
    print(f"  Final equity     : ${engine.equity:,.2f}")
    print(f"  Total P&L        : ${res.total_pnl:,.2f}")
    print(f"  Trades           : {res.total_trades}  (W {res.wins} / L {res.losses})")
    print(f"  Win rate         : {res.win_rate:.1f}%")
    print(f"  Max drawdown     : {res.max_drawdown:.2f}%")
    print(f"  Macro PIT delay  : {engine.cfg.macro_absorption_ms} ms")
    print("============================================================")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
