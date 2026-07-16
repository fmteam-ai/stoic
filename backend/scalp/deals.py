"""Scalp subsystem · broker-deal classification (round 7 items 3/4).

The broker's reported remaining position volume is the AUTHORITY for
partial-vs-full discrimination — but tiny sub-minimum residuals caused by
rounding must never be inflated into phantom positions.
"""
from datetime import datetime, timezone


def classify_close(position_volume, prior_lot: float, deal_lot: float,
                   min_lot: float = 0.01, lot_step: float = 0.01):
    """Returns (is_partial, remaining_lots).

    - position_volume present (EA v1.45+): remaining >= min_lot − step/2 →
      partial with the EXACT broker volume (never inflated to min lot);
      a sub-minimum residual is treated as a FULL close.
    - Fallback (legacy EA / backfills): lot arithmetic with 1% tolerance.
    """
    if position_volume is not None:
        pv = float(position_volume)
        if pv >= min_lot - lot_step / 2:
            return True, pv
        return False, 0.0
    if prior_lot > 0 and deal_lot > 0 and deal_lot < prior_lot * 0.99:
        return True, round(prior_lot - deal_lot, 2)
    return False, 0.0


def build_financial_event(*, account_id: str, symbol: str, trade_id: str,
                          deal_id, event_type: str, profit, commission,
                          swap, remaining_lots=None, lease_epoch: int = 0,
                          at_iso: str | None = None) -> dict:
    """Round 10 item 2 — the ledger event is built from IMMUTABLE broker-deal
    facts, never from mutable runner state (_last_financial_event). Recovery
    can therefore reconstruct the exact event from broker_deals even when the
    runner already dedup-skipped the deal after a crash."""
    pnl = float(profit or 0)
    commission = float(commission or 0)
    swap = float(swap or 0)
    net_pnl = pnl + commission + swap
    trading_pnl = pnl + commission + min(0.0, swap)
    execution_cost = max(0.0, -commission) + max(0.0, -swap)
    ev = {"account_id": account_id, "symbol": symbol,
          "trade_id": trade_id, "deal_id": str(deal_id),
          "event_type": event_type,
          "net_pnl": round(net_pnl, 2),
          "trading_pnl": round(trading_pnl, 2),
          "execution_cost": round(execution_cost, 2),
          "commission": commission, "swap": swap,
          "lease_epoch": int(lease_epoch or 0),
          "at": at_iso or datetime.now(timezone.utc).isoformat()}
    if remaining_lots is not None:
        ev["remaining_lots"] = float(remaining_lots)
    return ev
