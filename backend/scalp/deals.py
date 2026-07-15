"""Scalp subsystem · broker-deal classification (round 7 items 3/4).

The broker's reported remaining position volume is the AUTHORITY for
partial-vs-full discrimination — but tiny sub-minimum residuals caused by
rounding must never be inflated into phantom positions.
"""


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
