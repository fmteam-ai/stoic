"""iter-58 · Loss Cooldown guardrail (iter-122b: per-account; #56: cross-account).

After a BOT trade closes at a loss, block NEW trades in the same symbol+direction for `loss_cooldown_minutes`
(default 30) on THAT account. #56 (16 Oct): RoboForex hit SL on XAUUSD BUY at 15:14:55 (−1R), user_direction_cap
released the slot and OnEquity opened the same XAUUSD BUY 43 s later — so a REAL loss (< −0.1R) on ANY account of the
user now pauses that symbol+direction on ALL the user's accounts for the evaluating account's cooldown duration.
Scratch / break-even closes (≥ −0.1R) do not trigger the cross-account rule."""
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol

DEFAULT_LOSS_COOLDOWN_MIN = 30
CROSS_ACCOUNT_MIN_LOSS_R = -0.1
CROSS_ACCOUNT_PREFIX = "Cross-account loss cooldown"


def _minutes(cfg: dict) -> float:
    try:
        raw = cfg.get("loss_cooldown_minutes")
        return DEFAULT_LOSS_COOLDOWN_MIN if raw is None else float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LOSS_COOLDOWN_MIN


def trade_r_multiple(t: dict) -> float | None:
    """r_multiple when recorded, else (exit − entry) / |entry − ORIGINAL stop| signed by direction (#60 — a BE-moved
    `stop_loss` sits at entry and would turn a scratch into a huge loss); None when not derivable."""
    r = t.get("r_multiple")
    if r is not None:
        try:
            return float(r)
        except (TypeError, ValueError):
            return None
    try:
        entry, stop = float(t.get("entry_price")), float(t.get("original_stop_loss") or t.get("stop_loss"))
        exit_px = float(t.get("exit_price") or t.get("close_price"))
    except (TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return (exit_px - entry) / risk * (1 if str(t.get("action")).upper() == "BUY" else -1)


def is_real_loss(t: dict) -> bool:
    """#56 — a loss worse than −0.1R (R unknown → any negative P&L counts, conservative)."""
    r = trade_r_multiple(t)
    if r is not None:
        return r < CROSS_ACCOUNT_MIN_LOSS_R
    try:
        return float(t.get("pnl") or 0) < 0
    except (TypeError, ValueError):
        return False


async def loss_cooldown_block(db, user_id: str, symbol: str, action: str, cfg: dict) -> str | None:
    if not cfg.get("loss_cooldown_enabled", True):
        return None
    minutes = _minutes(cfg)
    if minutes <= 0:
        return None
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=minutes)).isoformat()
    q = {
        "user_id": user_id, "status": "closed", "action": action,
        "origin": "auto",  # bot losses only — manual trades must not freeze the bot
        "pnl": {"$lt": 0}, "closed_at": {"$gte": cutoff},
        "stats_excluded": {"$ne": True},   # late fills of expired/PANIC orders are not strategy outcomes
    }
    account_id = cfg.get("account_id")
    rows = await db.trades.find(q).sort("closed_at", -1).limit(50).to_list(length=50)   # every account of the user
    base = base_symbol(symbol)
    for r in rows:
        if base_symbol(r.get("symbol")) != base:
            continue
        own = not account_id or str(r.get("account_id")) == str(account_id)
        if not own and not is_real_loss(r):
            continue                      # another account's scratch / BE close does not pause this one
        try:
            closed = datetime.fromisoformat(str(r.get("closed_at")))
            left = minutes - (now - closed).total_seconds() / 60.0
        except (ValueError, TypeError):
            left = minutes
        if own:
            return (f"Loss cooldown: a bot {action} on {base} closed at a loss "
                    f"(${float(r.get('pnl') or 0):.2f}) {minutes - left:.0f}min ago — "
                    f"same-direction re-entries paused on this account for "
                    f"{max(left, 0):.0f} more min.")
        rr = trade_r_multiple(r)
        return (f"{CROSS_ACCOUNT_PREFIX}: a bot {action} on {base} closed at a loss on account "
                f"…{str(r.get('account_id') or '')[-6:]} ({rr:+.2f}R, ${float(r.get('pnl') or 0):.2f}) {minutes - left:.0f}min ago — "
                f"same-direction re-entries paused on ALL accounts for {max(left, 0):.0f} more min.") if rr is not None else (
                f"{CROSS_ACCOUNT_PREFIX}: a bot {action} on {base} closed at a loss on account …{str(r.get('account_id') or '')[-6:]} "
                f"(${float(r.get('pnl') or 0):.2f}) {minutes - left:.0f}min ago — same-direction re-entries paused on ALL accounts for {max(left, 0):.0f} more min.")
    return None
