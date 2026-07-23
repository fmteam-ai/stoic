"""Verified live performance — broker-truth statistics computed ONLY from
broker_deals (the EA-reported deal ledger), never from estimated P&L.
Includes a data-integrity stamp and an optional revocable public share link."""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/performance", tags=["performance"])
public_router = APIRouter(prefix="/public", tags=["public-performance"])


async def _verified_payload(db, user_id: str, mask: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    accounts = {}
    async for a in db.accounts.find(
            {"user_id": user_id, "status": {"$ne": "deleted"}},
            {"label": 1, "broker": 1, "mode": 1, "last_heartbeat": 1}):
        accounts[str(a["_id"])] = a

    per = {}
    daily = {}
    total = {"net": 0.0, "wins": 0, "losses": 0, "deals": 0,
             "first": None, "last": None}
    async for d in db.broker_deals.find(
            {"user_id": user_id},
            {"account_id": 1, "deal_time": 1, "profit": 1, "commission": 1,
             "swap": 1, "deal_entry": 1}).sort("deal_time", 1).limit(20000):
        realized = (float(d.get("profit") or 0)
                    + float(d.get("commission") or 0)
                    + float(d.get("swap") or 0))
        ts = d.get("deal_time")
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        aid = d.get("account_id")
        p = per.setdefault(aid, {"net": 0.0, "wins": 0, "losses": 0,
                                 "deals": 0, "first": None, "last": None})
        for row in (p, total):
            row["net"] += realized
            row["deals"] += 1
            row["first"] = row["first"] or day
            row["last"] = day
        if d.get("deal_entry") == "out":
            outcome = "wins" if realized > 0 else "losses"
            p[outcome] += 1
            total[outcome] += 1
        daily[day] = daily.get(day, 0.0) + realized

    curve, cum, peak, max_dd = [], 0.0, 0.0, 0.0
    for day in sorted(daily):
        cum += daily[day]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append({"date": day, "net": round(daily[day], 2),
                      "cum": round(cum, 2)})

    def _stats(row):
        closed = row["wins"] + row["losses"]
        return {"net_pnl": round(row["net"], 2), "deals": row["deals"],
                "closed_positions": closed,
                "win_rate": (round(row["wins"] / closed * 100, 1)
                             if closed else None),
                "first_deal": row["first"], "last_deal": row["last"]}

    account_rows = []
    for i, (aid, p) in enumerate(sorted(per.items(),
                                        key=lambda kv: -kv[1]["net"])):
        a = accounts.get(aid, {})
        account_rows.append({
            "label": (f"ACCOUNT-{i + 1}" if mask
                      else (a.get("label") or f"ACCOUNT-{i + 1}")),
            "broker": a.get("broker"),
            "mode": a.get("mode"),
            **_stats(p)})

    # Integrity stamp — how much of the record is broker-verified truth.
    trades_closed = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto"})
    verified = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto",
         "broker_deal_id": {"$ne": None}})
    estimated = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto",
         "$or": [{"pnl_estimated": True}, {"pnl_unknown": True}]})
    hb_age = None
    for a in accounts.values():
        try:
            d = datetime.fromisoformat(str(a.get("last_heartbeat")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            age = int((now - d).total_seconds())
            hb_age = age if hb_age is None else min(hb_age, age)
        except Exception:
            pass
    integrity = {
        "source": "broker_deals",
        "closed_trades": trades_closed,
        "broker_verified_trades": verified,
        "verified_pct": (round(verified / trades_closed * 100, 1)
                         if trades_closed else None),
        "estimated_or_unknown_excluded": estimated,
        "freshest_heartbeat_age_sec": hb_age,
    }

    return {"generated_at": now.isoformat(),
            "overall": _stats(total),
            "max_drawdown": round(max_dd, 2),
            "equity_curve": curve[-365:],
            "accounts": account_rows,
            "integrity": integrity}


@router.get("/verified")
async def verified(user=Depends(get_current_user)):
    db = get_db()
    payload = await _verified_payload(db, user["id"])
    share = await db.performance_shares.find_one(
        {"user_id": user["id"], "revoked": {"$ne": True}})
    payload["share"] = ({"share_id": share["share_id"],
                         "created_at": share.get("created_at")}
                        if share else None)
    return payload


@router.post("/share")
async def create_share(user=Depends(get_current_user)):
    """Create (or rotate) the public read-only share link."""
    db = get_db()
    share_id = secrets.token_urlsafe(16)
    await db.performance_shares.update_many(
        {"user_id": user["id"]}, {"$set": {"revoked": True}})
    await db.performance_shares.insert_one({
        "user_id": user["id"], "share_id": share_id, "revoked": False,
        "created_at": datetime.now(timezone.utc).isoformat()})
    return {"share_id": share_id}


@router.delete("/share")
async def revoke_share(user=Depends(get_current_user)):
    db = get_db()
    res = await db.performance_shares.update_many(
        {"user_id": user["id"], "revoked": {"$ne": True}},
        {"$set": {"revoked": True}})
    return {"revoked": res.modified_count > 0}


@public_router.get("/performance/{share_id}")
async def public_performance(share_id: str):
    """Unauthenticated read-only verified track record (masked labels)."""
    db = get_db()
    share = await db.performance_shares.find_one(
        {"share_id": share_id, "revoked": {"$ne": True}})
    if not share:
        raise HTTPException(status_code=404, detail="Share link not found or revoked")
    payload = await _verified_payload(db, share["user_id"], mask=True)
    payload["shared"] = True
    return payload
