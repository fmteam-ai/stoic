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


def effective_connection_state(acc: dict,
                               now: datetime | None = None) -> dict:
    """Review P0-2 — THE single server-side connection-truth rule.
    Every surface (header, Accounts, Data Freshness, Bot Doctor, Bot
    Health, go-live checks) must use this, never its own threshold."""
    now = now or _now_dt()
    if acc.get("mode") == "paper":
        return {"state": "PAPER", "connected": True,
                "heartbeat_age_seconds": None,
                "threshold_seconds": HEARTBEAT_FRESH_S,
                "reason": "paper account — no EA required",
                "evaluated_at": now.isoformat()}
    age = _age_s(acc.get("last_heartbeat"))
    if age is None:
        state, reason = "NEVER_CONNECTED", "no heartbeat ever received"
    elif age < HEARTBEAT_FRESH_S:
        state = "CONNECTED"
        reason = f"fresh heartbeat ({age:.0f}s < {HEARTBEAT_FRESH_S}s)"
    elif age < 3600:
        state = "STALE"
        reason = (f"heartbeat {age:.0f}s old "
                  f"(threshold {HEARTBEAT_FRESH_S}s)")
    else:
        state, reason = "DISCONNECTED", f"heartbeat {age / 3600:.1f}h old"
    return {"state": state, "connected": state == "CONNECTED",
            "heartbeat_age_seconds":
                round(age, 1) if age is not None else None,
            "threshold_seconds": HEARTBEAT_FRESH_S,
            "reason": reason, "evaluated_at": now.isoformat()}


def account_truth(acc: dict, open_local: int) -> dict:
    """Pure per-account derivation from the account doc + local count."""
    paper = acc.get("mode") == "paper"
    hb_age = _age_s(acc.get("last_heartbeat"))
    ea_connected = effective_connection_state(acc)["connected"]
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
        authority, authority_reason = "FULL", None
    elif not ea_connected:
        authority = "NONE"
        authority_reason = ("EA not connected — no heartbeat, "
                            "no execution authority")
    elif not acc.get("verified_identity"):
        authority = "REDUCED"
        authority_reason = ("broker identity not verified — click TRUST "
                            "THIS TERMINAL in the blocker panel (one "
                            "click), or pair via Accounts → Quick Install")
    else:
        authority, authority_reason = "FULL", None
    return {"ea_connected": ea_connected,
            "heartbeat_age_seconds":
                round(hb_age, 1) if hb_age is not None else None,
            "position_truth": truth,
            "open_positions_broker": broker,
            "open_positions_local": open_local,
            "execution_authority": authority,
            "authority_reason": authority_reason}


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


def state_reason(*, eff: str, truth: dict, operational_mode: str) -> str | None:
    """Human-readable WHY for a non-executing effective_state."""
    if eff == "PANIC":
        return ("panic switch tripped — investigate, verify broker "
                "positions, then reset the panic switch")
    if eff == "DISCONNECTED":
        age = truth.get("heartbeat_age_seconds")
        return ("no EA heartbeat ever received — attach the EA to this "
                "account" if age is None else
                f"EA heartbeat {age:.0f}s old — terminal/agent offline")
    if eff == "BLOCKED":
        if truth["position_truth"] != "FRESH":
            return (f"position truth {truth['position_truth']} — a fresh "
                    "broker snapshot plus reconciliation is required "
                    "before opening orders")
        return (truth.get("authority_reason")
                or f"execution authority {truth['execution_authority']} — "
                   "full authority required to execute")
    if eff == "OBSERVING":
        return (f"operational mode is {operational_mode} — signals only, "
                "no live orders until switched to live")
    return None


async def backfill_trading_enabled(db) -> int:
    """Audit P1-6 — explicit enablement migration. Accounts lacking
    `trading_enabled` are stamped from their owner-confirmed bot config
    (active → True) so nothing running today silently switches off; from
    now on a MISSING flag reads as disabled everywhere."""
    n = 0
    async for a in db.accounts.find({"trading_enabled": {"$exists": False}},
                                    {"user_id": 1}):
        cfg = await db.bot_configs.find_one(
            {"user_id": a.get("user_id"), "account_id": str(a["_id"])},
            {"active": 1}) or await db.bot_configs.find_one(
            {"user_id": a.get("user_id"), "account_id": None},
            {"active": 1}) or {}
        await db.accounts.update_one(
            {"_id": a["_id"]},
            {"$set": {"trading_enabled": bool(cfg.get("active")),
                      "trading_enabled_backfilled_at":
                          _now_dt().isoformat()}})
        n += 1
    return n


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
        # N13 — a PANIC lock on the account reads as tripped, whatever `active` says
        tripped = bool(cfg.get("tripped_at")) or (a.get("authority_lock") or {}).get("reason") == "panic"
        mode = str(cfg.get("operational_mode") or "observe")
        eff = effective_state(bot_enabled=bot_on, tripped=tripped,
                              truth=truth, operational_mode=mode)
        enabled = a.get("trading_enabled") is True  # audit P1-6: missing = OFF
        rows.append({"account_id": aid, "label": a.get("label"),
                     "mode": a.get("mode"),
                     "account_enabled": enabled, "bot_enabled": bot_on,
                     "operational_mode": mode, "effective_state": eff,
                     "state_reason": state_reason(
                         eff=eff, truth=truth, operational_mode=mode),
                     "trust_eligible": (truth["ea_connected"]
                                        and truth["execution_authority"]
                                        == "REDUCED"
                                        and a.get("mode") != "paper"),
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
    eff_reason = next((r.get("state_reason") for r in c["accounts"]
                       if r["bot_enabled"] and r["effective_state"] == eff
                       and r.get("state_reason")), None)
    truth = c["position_truth"]
    broker = tot["open_broker"]
    # PANIC must be available whenever exposure exists OR cannot be ruled out
    panic_available = (open_local > 0 or (broker or 0) > 0
                       or truth != "FRESH")
    return {"position_truth": truth,
            "open_trades_broker": broker,
            "effective_state": eff,
            "effective_reason": eff_reason,
            "panic_available": panic_available,
            "accounts_total": tot["accounts_total"],
            "accounts_enabled": tot["accounts_enabled"],
            "bots_enabled": tot["bots_enabled"],
            "eas_connected": tot["eas_connected"],
            "as_of": c["as_of"]}


async def inventory(db, user_id: str) -> dict:
    """Audit v3 P0-2 — ONE canonical inventory object. Every surface
    (header, Dashboard, Bot Pulse, Bot Health, Data Freshness,
    Certification, Portfolio) must consume these counters — never
    recompute their own."""
    c = await contract(db, user_id)
    accounts = {str(a["_id"]): a async for a in
                db.accounts.find({"user_id": user_id})}
    ea = {"fresh": 0, "stale": 0, "offline": 0, "paper": 0}
    bots_effective: dict = {}
    environments: dict = {}
    rows = []
    for r in c["accounts"]:
        acc = accounts.get(r["account_id"]) or {}
        conn = effective_connection_state(acc)
        bucket = ("paper" if conn["state"] == "PAPER" else
                  "fresh" if conn["state"] == "CONNECTED" else
                  "stale" if conn["state"] == "STALE" else "offline")
        ea[bucket] += 1
        eff = r["effective_state"]
        bots_effective[eff] = bots_effective.get(eff, 0) + 1
        env = str(acc.get("mode") or "demo")
        environments[env] = environments.get(env, 0) + 1
        rows.append({"account_id": r["account_id"], "label": r.get("label"),
                     "environment": env,
                     "account_enabled": r["account_enabled"],
                     "bot_requested_enabled": r["bot_enabled"],
                     "bot_effective_state": eff,
                     "ea_connection_state": conn["state"],
                     "ea_heartbeat_age_seconds":
                         conn["heartbeat_age_seconds"]})
    installs = await db.installations.count_documents(
        {"user_id": user_id, "revoked": {"$ne": True}})
    tot = c["totals"]
    return {"as_of": c["as_of"],
            "accounts_configured": tot["accounts_total"],
            "accounts_enabled": tot["accounts_enabled"],
            "bots_requested_on": tot["bots_enabled"],
            "bots_effective": bots_effective,
            "ea_installation_count": installs,
            "ea_connection": {**ea,
                              "threshold_seconds": HEARTBEAT_FRESH_S},
            "environments": environments,
            "accounts": rows,
            "note": "Canonical inventory — consume, never recompute."}
