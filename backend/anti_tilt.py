"""#39 — ONE anti-tilt evaluation shared by bot_runner (the gate) and Bot Health / status / pulse (the display).

On 16 Oct Bot Health showed "freeze 46m" while bot_runner had 0 anti-tilt blocks: the banner used the OLD rule
(all(pnl<0) over the last N closed trades of ANY origin) while the runner judges only origin=auto, non-excluded
trades with streak_losses.is_streak_loss. Both now call `evaluate()`; the result lists which trades count and why."""
from datetime import datetime, timedelta, timezone

from streak_losses import STREAK_FIELDS, classify_loss

DISPLAY_N = 5


def settings(cfg: dict) -> dict:
    return {"enabled": bool(cfg.get("anti_tilt_enabled", True)),
            "n": int(cfg.get("anti_tilt_consecutive_losses", 3) or 0),
            "hours": int(cfg.get("anti_tilt_freeze_hours", 4) or 0)}


def streak_query(user_id: str, account_id: str | None) -> dict:
    """Exactly the runner's filter: the bot is judged by its OWN trades (origin=auto), excluded rows ignored."""
    q = {"user_id": user_id, "status": "closed", "origin": "auto", "stats_excluded": {"$ne": True}}
    if account_id:
        q["account_id"] = account_id
    return q


def _iso(ts) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def classify(recent: list, cfg: dict, now: datetime | None = None, account: dict | None = None) -> dict:
    """Pure. `recent` = closed origin=auto trades, newest first. Freeze iff the newest `n` ALL count as losses and
    the newest close is within `hours`."""
    now = now or datetime.now(timezone.utc)
    st = settings(cfg)
    rows = []
    for t in recent[:max(st["n"], DISPLAY_N)]:
        c = classify_loss(t, account)
        rows.append({"trade_id": str(t.get("_id")), "symbol": t.get("symbol"), "action": t.get("action"),
                     "pnl": float(t.get("pnl") or 0), "r": c["r"], "closed_at": t.get("closed_at"),
                     "counts_as_loss": c["counts"], "why": c["why"]})
    loss_streak = 0
    for r in rows:
        if not r["counts_as_loss"]:
            break
        loss_streak += 1
    win_streak = 0
    for r in rows:
        if r["pnl"] <= 0:
            break
        win_streak += 1
    judged = rows[:st["n"]]
    active, frozen_until, remaining = False, None, None
    if st["enabled"] and st["n"] > 0 and st["hours"] > 0 and len(judged) == st["n"] and all(r["counts_as_loss"] for r in judged):
        lc = _iso(judged[0]["closed_at"])
        if lc is not None:
            until = lc + timedelta(hours=st["hours"])
            left = (until - now).total_seconds()
            if left > 0:
                active, frozen_until, remaining = True, until.isoformat(), int(left)
    eta = None
    if remaining is not None:
        hrs, mins = divmod(remaining // 60, 60)
        eta = f"{hrs}h {mins}m" if hrs else f"{mins}m"
    reason = (f"Anti-tilt freeze — last {st['n']} trades lost. Resumes in <{st['hours']}h." if active else None)
    return {**st, "active": active, "frozen_until": frozen_until, "remaining_seconds": remaining, "eta": eta,
            "reason": reason, "loss_streak": loss_streak, "win_streak": win_streak, "trades": rows,
            "rule": f"origin=auto · not stats_excluded · loss = gave back > 0.1R of planned risk · newest {st['n']} all losses within {st['hours']}h"}


async def evaluate(db, user_id: str, cfg: dict, now: datetime | None = None) -> dict:
    account_id = cfg.get("account_id")
    st = settings(cfg)
    limit = max(st["n"], DISPLAY_N)
    recent = await db.trades.find(streak_query(user_id, account_id), STREAK_FIELDS).sort(
        "closed_at", -1).limit(limit).to_list(length=limit)
    account = None
    if account_id:
        try:
            from bson import ObjectId
            account = await db.accounts.find_one({"_id": ObjectId(str(account_id))}, {"symbol_specs": 1})
        except Exception:  # noqa: BLE001 — specs are an optional refinement of the R math
            account = None
    return classify(recent, cfg, now, account)
