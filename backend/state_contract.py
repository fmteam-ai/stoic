"""Canonical account/bot state contract (iter-158, review P0-1/2/3).

ONE server-side derivation of per-account truth so every surface shows the
same thing:
  - account_enabled / bot_enabled counted from the SAME config read
  - ea_connected from authoritative heartbeat age
  - position_truth: FRESH / STALE / UNKNOWN / CONFLICTED — UNKNOWN is never
    rendered as zero
  - open_positions_broker (EA PositionsTotal, authoritative when fresh)
    vs open_positions_local (projection) — compared, not merged
  - effective_state: server-derived (PANIC/OFF/DISCONNECTED/BLOCKED/
    OBSERVING/ACTIVE) so "BOT ON" is never shown without what it can DO
"""
from datetime import datetime, timezone

HEARTBEAT_FRESH_S = 180
TRUTH_RANK = {"FRESH": 0, "STALE": 1, "UNKNOWN": 2, "CONFLICTED": 3}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _age_s(iso) -> float | None:
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now_dt() - ts).total_seconds()
    except Exception:
        return None


def account_truth(acc: dict, open_local: int) -> dict:
    """Pure per-account derivation from the account doc + local count."""
    paper = acc.get("mode") == "paper"
    hb_age = _age_s(acc.get("last_heartbeat"))
    ea_connected = paper or (hb_age is not None and hb_age < HEARTBEAT_FRESH_S)
    if paper:
        truth, broker = "FRESH", open_local
    elif hb_age is None:
        truth, broker = "UNKNOWN", None
    elif hb_age >= HEARTBEAT_FRESH_S:
        truth, broker = "STALE", None       # stale broker count is NOT truth
    else:
        broker = int(acc.get("open_positions") or 0)
        truth = "CONFLICTED" if broker != open_local else "FRESH"
    if paper:
        authority = "FULL"
    elif not ea_connected:
        authority = "NONE"
    elif not acc.get("verified_identity"):
        authority = "REDUCED"
    else:
        authority = "FULL"
    return {"ea_connected": ea_connected,
            "heartbeat_age_seconds":
                round(hb_age, 1) if hb_age is not None else None,
            "position_truth": truth,
            "open_positions_broker": broker,
            "open_positions_local": open_local,
            "execution_authority": authority}


def effective_state(*, bot_enabled: bool, tripped: bool, truth: dict,
                    operational_mode: str) -> str:
    if tripped:
        return "PANIC"
    if not bot_enabled:
        return "OFF"
    if not truth["ea_connected"]:
        return "DISCONNECTED"
    if truth["position_truth"] != "FRESH" \
            or truth["execution_authority"] != "FULL":
        return "BLOCKED"
    if operational_mode in ("observe", "shadow"):
        return "OBSERVING"
    return "ACTIVE"


async def contract(db, user_id: str) -> dict:
    """Full per-account state contract + totals, from ONE read."""
    accounts = [a async for a in db.accounts.find({"user_id": user_id})]
    cfgs = {c.get("account_id"): c async for c in
            db.bot_configs.find({"user_id": user_id})}
    global_cfg = cfgs.get(None) or {}
    rows, worst = [], "FRESH"
    tot = {"accounts_total": 0, "accounts_enabled": 0, "bots_enabled": 0,
           "eas_connected": 0, "open_local": 0, "open_broker": 0}
    broker_known = True
    for a in accounts:
        aid = str(a["_id"])
        open_local = await db.trades.count_documents(
            {"account_id": aid, "status": "open"})
        truth = account_truth(a, open_local)
        cfg = cfgs.get(aid) or global_cfg
        bot_on = bool(cfg.get("active"))
        tripped = bool(cfg.get("tripped_at"))
        mode = str(cfg.get("operational_mode") or "observe")
        eff = effective_state(bot_enabled=bot_on, tripped=tripped,
                              truth=truth, operational_mode=mode)
        enabled = a.get("trading_enabled") is not False
        rows.append({"account_id": aid, "label": a.get("label"),
                     "mode": a.get("mode"),
                     "account_enabled": enabled, "bot_enabled": bot_on,
                     "operational_mode": mode, "effective_state": eff,
                     "force_trade_allowed":
                         enabled and truth["ea_connected"]
                         and truth["position_truth"] == "FRESH"
                         and truth["execution_authority"] == "FULL",
                     **truth})
        tot["accounts_total"] += 1
        tot["accounts_enabled"] += 1 if enabled else 0
        tot["bots_enabled"] += 1 if bot_on else 0
        tot["eas_connected"] += 1 if truth["ea_connected"] else 0
        tot["open_local"] += open_local
        if truth["open_positions_broker"] is None:
            broker_known = False
        else:
            tot["open_broker"] += truth["open_positions_broker"]
        if TRUTH_RANK[truth["position_truth"]] > TRUTH_RANK[worst]:
            worst = truth["position_truth"]
    if not broker_known:
        tot["open_broker"] = None      # UNKNOWN must never collapse to 0
    return {"as_of": _now_dt().isoformat(), "position_truth": worst,
            "accounts": rows, "totals": tot}


async def quick_truth(db, user_id: str, open_local: int) -> dict:
    """Compact truth block for the sticky quick-actions bar."""
    c = await contract(db, user_id)
    tot = c["totals"]
    states = [r["effective_state"] for r in c["accounts"]
              if r["bot_enabled"]]
    order = ["PANIC", "DISCONNECTED", "BLOCKED", "OBSERVING", "ACTIVE"]
    eff = next((s for s in order if s in states), "OFF")
    truth = c["position_truth"]
    broker = tot["open_broker"]
    # PANIC must be available whenever exposure exists OR cannot be ruled out
    panic_available = (open_local > 0 or (broker or 0) > 0
                       or truth != "FRESH")
    return {"position_truth": truth,
            "open_trades_broker": broker,
            "effective_state": eff,
            "panic_available": panic_available,
            "accounts_total": tot["accounts_total"],
            "accounts_enabled": tot["accounts_enabled"],
            "bots_enabled": tot["bots_enabled"],
            "eas_connected": tot["eas_connected"],
            "as_of": c["as_of"]}
