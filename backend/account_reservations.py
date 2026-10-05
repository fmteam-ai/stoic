"""A13 P1-02 — ONE account-wide entry reservation service.

Every entry path (main bot MT5, paper, crypto, scalp) must PRESENT a reservation id
before any broker call. Capacity (open-position count, automated-trade cap, daily
trade capacity, reserved risk) is checked and the reservation written under a
per-account lease lock: two workers counting then inserting inside separate Mongo
transactions do NOT conflict under snapshot isolation, so the serialisation
primitive is an atomic single-document lease, which is race-free on standalone
and replica-set deployments alike. Reservations live in db.risk_reservations
(scalp.risk_reservations state machine) — one collection, one truth."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from scalp import risk_reservations as rr

logger = logging.getLogger("account.reservations")

LOCK_TTL_SEC = 5
LOCK_RETRIES = 40          # 40 × 50 ms = 2 s worst case before "busy"


def _now():
    return datetime.now(timezone.utc)


def _day_start_iso(now=None) -> str:
    n = now or _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


async def ensure_reservation_lock_indexes(db) -> None:
    await db.account_reservation_locks.create_index("locked_until")


async def _acquire_lock(db, account_id: str, owner: str) -> bool:
    col = db.account_reservation_locks
    for _ in range(LOCK_RETRIES):
        now = _now()
        lease = now + timedelta(seconds=LOCK_TTL_SEC)
        if await col.find_one({"_id": account_id}, {"_id": 1}) is None:
            try:
                await col.insert_one({"_id": account_id, "locked_until": lease, "owner": owner})
                return True
            except DuplicateKeyError:
                pass                  # another worker created the lease doc first
        res = await col.update_one(
            {"_id": account_id, "$or": [{"locked_until": {"$lt": now}}, {"locked_until": None}]},
            {"$set": {"locked_until": lease, "owner": owner}})
        if res.matched_count == 1:
            return True
        await asyncio.sleep(0.05)
    return False


async def _release_lock(db, account_id: str, owner: str) -> None:
    await db.account_reservation_locks.update_one(
        {"_id": account_id, "owner": owner}, {"$set": {"locked_until": None, "owner": None}})


async def capacity_snapshot(db, *, account_id: str, user_id: str) -> dict:
    """Current exposure as the caps see it: DB positions + reservations invisible to them."""
    base = {"user_id": user_id, "account_id": account_id, "status": {"$in": ["pending", "open"]}}
    total_open = await db.trades.count_documents(base)
    auto_open = await db.trades.count_documents({**base, "origin": "auto"})
    unaccounted = await rr.unaccounted_count(db, account_id)
    today = await db.trades.count_documents(
        {"user_id": user_id, "account_id": account_id, "origin": "auto",
         "opened_at": {"$gte": _day_start_iso()}})
    agg = await db.risk_reservations.aggregate([
        {"$match": {"account_id": account_id, "state": {"$in": list(rr.ACTIVE_STATES)}}},
        {"$group": {"_id": None, "risk": {"$sum": "$risk_usd"}}}]).to_list(1)
    return {"total_open": total_open + unaccounted, "auto_open": auto_open + unaccounted,
            "today_auto": today + unaccounted, "reserved_risk_usd": float((agg or [{}])[0].get("risk", 0) or 0)}


def _violations(snap: dict, caps: dict, risk_usd: float) -> list[str]:
    out = []
    auto_cap = int(caps.get("auto_cap") or 0)
    if auto_cap > 0 and snap["auto_open"] >= auto_cap:
        out.append(f"automated positions {snap['auto_open']}/{auto_cap}")
    total_cap = int(caps.get("total_cap") or 0)
    if total_cap > 0 and snap["total_open"] >= total_cap:
        out.append(f"total positions {snap['total_open']}/{total_cap}")
    daily_cap = int(caps.get("daily_cap") or 0)
    if daily_cap > 0 and snap["today_auto"] >= daily_cap:
        out.append(f"daily trade capacity {snap['today_auto']}/{daily_cap}")
    risk_cap = float(caps.get("risk_cap_usd") or 0)
    if risk_cap > 0 and snap["reserved_risk_usd"] + float(risk_usd or 0) > risk_cap:
        out.append(f"reserved risk ${snap['reserved_risk_usd'] + float(risk_usd or 0):.2f} > ${risk_cap:.2f}")
    return out


async def reserve_entry(db, *, account_id: str, user_id: str, source: str, decision_id: str,
                        risk_usd: float = 0.0, lot: float = 0.0, caps: dict | None = None) -> dict:
    """Check every cap and write the reservation atomically (per-account lease).
    Returns {"ok": True, "reservation": doc, "capacity": snap} or
    {"ok": False, "blocked": "reservation_capacity|reservation_busy", ...}."""
    caps = caps or {}
    owner = f"{source}:{decision_id}"
    if not await _acquire_lock(db, account_id, owner):
        logger.warning("reservation lease busy account=%s source=%s", account_id, source)
        return {"ok": False, "blocked": "reservation_busy", "reason": "account entry lease busy — retry next cycle"}
    try:
        snap = await capacity_snapshot(db, account_id=account_id, user_id=user_id)
        bad = _violations(snap, caps, risk_usd)
        if bad:
            logger.warning("reservation refused account=%s source=%s: %s", account_id, source, "; ".join(bad))
            return {"ok": False, "blocked": "reservation_capacity", "violations": bad, "capacity": snap,
                    "inflight": snap["auto_open"], "cap": int(caps.get("auto_cap") or 0),
                    "total_inflight": snap["total_open"], "total_cap": int(caps.get("total_cap") or 0)}
        try:
            doc = await rr.reserve(db, account_id=account_id, user_id=user_id, decision_id=decision_id,
                                   risk_usd=risk_usd, lot=lot)
        except DuplicateKeyError:
            return {"ok": False, "blocked": "duplicate_reservation",
                    "reason": "an ACTIVE reservation already exists for this decision"}
        await db.risk_reservations.update_one(
            {"reservation_id": doc["reservation_id"]},
            {"$set": {"source": source, "caps_snapshot": caps, "capacity_snapshot": snap}})
        return {"ok": True, "reservation": doc, "capacity": snap}
    finally:
        await _release_lock(db, account_id, owner)


async def present(db, reservation_id: str, *, account_id: str) -> dict | None:
    """An entry path presenting an existing reservation: must be ACTIVE for this account."""
    return await db.risk_reservations.find_one(
        {"reservation_id": str(reservation_id or ""), "account_id": account_id,
         "state": {"$in": list(rr.ACTIVE_STATES)}})


async def link_trade(db, reservation_id: str, trade_id: str) -> str:
    return await rr.transition(db, reservation_id, "SLOT_LINKED", trade_id=str(trade_id))


async def release(db, reservation_id: str, reason: str) -> str:
    return await rr.transition(db, reservation_id, "RELEASED", release_reason=str(reason)[:60])


def total_positions_buffer() -> int:
    return int(os.environ.get("EXEC_TOTAL_POSITIONS_BUFFER", "2"))


async def caps_for(db, *, user_id: str, cfg_account_id: str | None, max_concurrent: int) -> dict:
    """Engine caps: config automated cap, hard total cap, daily trade capacity."""
    caps = {"auto_cap": int(max_concurrent or 0),
            "total_cap": (int(max_concurrent) + total_positions_buffer()) if max_concurrent else 0,
            "daily_cap": 0}
    if cfg_account_id:
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": cfg_account_id},
                                            {"trade_of_day_cap": 1})
        if cfg and cfg.get("trade_of_day_cap"):
            caps["daily_cap"] = int(cfg["trade_of_day_cap"])
    return caps


async def guard_entry(db, *, account: dict, user_id: str, signal: dict, source: str,
                      max_concurrent: int, cfg_account_id: str | None, run) -> dict:
    """Wrap an engine stage: present-or-reserve BEFORE it runs, link on success,
    release on refusal/error. `run(reservation_id)` performs the engine stage."""
    account_id = str(account.get("_id") or cfg_account_id or "")
    presented = signal.get("_reservation_id")
    if presented:
        resv = await present(db, presented, account_id=account_id)
        if resv is None:
            return {"blocked": "reservation_missing",
                    "reason": "entry path presented a reservation that is not ACTIVE for this account"}
        rid, owned = resv["reservation_id"], False
    else:
        caps = await caps_for(db, user_id=user_id, cfg_account_id=cfg_account_id, max_concurrent=max_concurrent)
        decision_id = str(signal.get("signal_id") or signal.get("scalp_decision_id")
                          or f"{source}:{account_id}:{signal.get('symbol')}:{signal.get('action')}:{_now().timestamp():.0f}")
        got = await reserve_entry(db, account_id=account_id, user_id=user_id, source=source,
                                  decision_id=decision_id, risk_usd=float(signal.get("risk_usd") or 0),
                                  lot=float(signal.get("lot_size") or 0), caps=caps)
        if not got["ok"]:
            if got["blocked"] == "reservation_capacity":
                got = {**got, "blocked": "max_concurrent_cap"}      # keep the runner's refusal vocabulary
            return {k: v for k, v in got.items() if k != "ok"}
        rid, owned = got["reservation"]["reservation_id"], True
    try:
        out = await run(rid)
    except Exception:
        if owned:
            await release(db, rid, "error")
        raise
    if isinstance(out, dict) and out.get("blocked"):
        if owned:
            await release(db, rid, f"refused:{out['blocked']}")
    elif isinstance(out, dict) and out.get("id"):
        if owned:                                   # a presenting path owns its own lifecycle
            await link_trade(db, rid, out["id"])
        out["reservation_id"] = rid
    return out
