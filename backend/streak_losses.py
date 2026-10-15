"""v1.60.48 — what counts as a LOSS for streak logic (anti-tilt freeze, learning freeze).

Breakeven scratches (−0.04, −0.92 on a $X risk) are not losses: on 15 Oct two scratches plus two real losses
tripped a 4 h anti-tilt freeze (469 blocked decisions) and the 24 h learning freeze. A trade is a streak loss
only when it gave back more than SCRATCH_R of its planned risk; a trade closed by a breakeven rule never is.
When the planned risk cannot be reconstructed, any negative pnl still counts (conservative)."""

SCRATCH_R = 0.1
BREAKEVEN_EXIT_RULES = {"breakeven_0_5r"}
STREAK_FIELDS = {"pnl": 1, "closed_at": 1, "entry_price": 1, "stop_loss": 1, "lot_size": 1, "symbol": 1,
                 "scalp_exit_rule": 1, "breakeven_set": 1, "be_target_sl": 1}


def is_streak_loss(trade: dict) -> bool:
    pnl = float(trade.get("pnl") or 0)
    if pnl >= 0:
        return False
    if trade.get("scalp_exit_rule") in BREAKEVEN_EXIT_RULES:
        return False
    from analytics import _r_multiple
    r = _r_multiple(trade)
    if r is None:
        return True
    return r < -SCRATCH_R
