"""v1.60.48 — what counts as a LOSS for streak logic (anti-tilt freeze, learning freeze).

Breakeven scratches (−0.04, −0.92 on a $X risk) are not losses: on 15 Oct two scratches plus two real losses
tripped a 4 h anti-tilt freeze (469 blocked decisions) and the 24 h learning freeze. A trade is a streak loss
only when it gave back more than SCRATCH_R of its planned risk; a trade closed by a breakeven rule never is.
When the planned risk cannot be reconstructed, any negative pnl still counts (conservative)."""

SCRATCH_R = 0.1
BREAKEVEN_EXIT_RULES = {"breakeven_0_5r"}
STREAK_FIELDS = {"pnl": 1, "closed_at": 1, "entry_price": 1, "stop_loss": 1, "original_stop_loss": 1, "lot_size": 1,
                 "original_lot_size": 1, "symbol": 1, "scalp_exit_rule": 1, "breakeven_set": 1, "be_target_sl": 1}


def _planned_r(trade: dict):
    """R against the ORIGINAL stop (v1.60.50 — after a BE move `stop_loss` sits at entry → risk ≈ 0 → every
    scratch looked like a huge loss)."""
    from analytics import _r_multiple
    osl = trade.get("original_stop_loss")
    row = {**trade, "stop_loss": osl} if osl else trade
    if trade.get("original_lot_size"):               # v1.60.52 — the field trades actually store (filled volume)
        row = {**row, "lot_size": trade["original_lot_size"]}
    return _r_multiple(row)


def is_streak_loss(trade: dict) -> bool:
    pnl = float(trade.get("pnl") or 0)
    if pnl >= 0:
        return False
    # a scratch is a stop the EA CONFIRMED moved to break-even (`breakeven_set`), not merely a requested BE rule
    if trade.get("scalp_exit_rule") in BREAKEVEN_EXIT_RULES and trade.get("breakeven_set"):
        return False
    r = _planned_r(trade)
    if r is None:
        return True
    return r < -SCRATCH_R
