"""v1.60.48 — what counts as a LOSS for streak logic (anti-tilt freeze, learning freeze).

Breakeven scratches (−0.04, −0.92 on a $X risk) are not losses: on 15 Oct two scratches plus two real losses
tripped a 4 h anti-tilt freeze (469 blocked decisions) and the 24 h learning freeze. A trade is a streak loss
only when it gave back more than SCRATCH_R of its planned risk; a trade closed by a breakeven rule never is.
When the planned risk cannot be reconstructed, any negative pnl still counts (conservative)."""

SCRATCH_R = 0.1
BREAKEVEN_EXIT_RULES = {"breakeven_0_5r"}
STREAK_FIELDS = {"pnl": 1, "closed_at": 1, "entry_price": 1, "stop_loss": 1, "original_stop_loss": 1, "lot_size": 1,
                 "original_lot_size": 1, "symbol": 1, "scalp_exit_rule": 1, "breakeven_set": 1, "be_target_sl": 1,
                 "account_id": 1, "origin": 1, "action": 1, "stale_feature_entry": 1}


def _planned_r(trade: dict, account: dict | None = None):
    """R against the ORIGINAL stop (v1.60.50 — after a BE move `stop_loss` sits at entry → risk ≈ 0 → every
    scratch looked like a huge loss). #40 — contract size from the account's broker spec / instrument table."""
    from analytics import trade_r_multiple
    return trade_r_multiple(trade, account)


def classify_loss(trade: dict, account: dict | None = None) -> dict:
    """{counts: bool, r: float|None, why: str} — the ONE rule, with its explanation for Bot Health (#39)."""
    pnl = float(trade.get("pnl") or 0)
    if pnl >= 0:
        return {"counts": False, "r": None, "why": "pnl ≥ 0 — not a loss"}
    if trade.get("stale_feature_entry"):
        return {"counts": False, "r": None, "why": "stale-feature entry (#55: live price > 0.5×ATR15 from the closed-bar features) — not a tilt loss"}
    # a scratch is a stop the EA CONFIRMED moved to break-even (`breakeven_set`), not merely a requested BE rule
    if trade.get("scalp_exit_rule") in BREAKEVEN_EXIT_RULES and trade.get("breakeven_set"):
        return {"counts": False, "r": None, "why": "breakeven scratch (EA confirmed BE stop) — not a loss"}
    r = _planned_r(trade, account)
    if r is None:
        return {"counts": True, "r": None, "why": "planned risk unknown (no SL / contract size) — counts, conservative"}
    r = round(r, 3)
    if r < -SCRATCH_R:
        return {"counts": True, "r": r, "why": f"{r:+.2f}R — gave back more than {SCRATCH_R}R of planned risk"}
    return {"counts": False, "r": r, "why": f"{r:+.2f}R — scratch (within {SCRATCH_R}R of planned risk), not a loss"}


def is_streak_loss(trade: dict, account: dict | None = None) -> bool:
    return classify_loss(trade, account)["counts"]
