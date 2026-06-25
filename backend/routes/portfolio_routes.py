"""Portfolio risk routes — snapshot + manual deleverage trigger.

  GET  /api/portfolio/snapshot           — full risk snapshot for the user's active account
  POST /api/portfolio/deleverage         — execute the snapshot's pending actions

The snapshot is read-only; the deleverage endpoint requires explicit
confirmation (caller passes `{confirm: true}`) and applies the actions
returned by the latest snapshot.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_db
from portfolio.risk_manager import build_snapshot, execute_deleveraging_actions

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _resolve_account(db, user_id: str, account_id: str | None):
    if account_id:
        from bson import ObjectId
        try:
            acc = await db.accounts.find_one({"_id": ObjectId(account_id),
                                              "user_id": user_id})
        except Exception:
            acc = None
    else:
        acc = await db.accounts.find_one({"user_id": user_id})
    return acc


@router.get("/snapshot")
async def portfolio_snapshot(
    account_id: str | None = Query(None),
    user=Depends(get_current_user),
):
    db = get_db()
    acc = await _resolve_account(db, user["id"], account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    q = {"user_id": user["id"], "status": {"$in": ["open", "pending"]},
         "account_id": str(acc["_id"])}
    open_positions = await db.trades.find(q).to_list(length=200)
    snap = await build_snapshot(db, account=acc, open_positions=open_positions)
    snap["account_id"] = str(acc["_id"])
    snap["account_label"] = acc.get("label") or acc.get("account_number")
    return snap


@router.post("/deleverage")
async def trigger_deleverage(
    payload: dict,
    user=Depends(get_current_user),
):
    """Execute the actions returned by /portfolio/snapshot.

    Body: {"account_id": str?, "confirm": true, "actions"?: [...]}
    If `actions` is omitted, we recompute the snapshot fresh — defence
    against UI-stale action lists.
    """
    if not payload.get("confirm"):
        raise HTTPException(status_code=400, detail="confirm=true required")

    db = get_db()
    acc = await _resolve_account(db, user["id"], payload.get("account_id"))
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    actions = payload.get("actions")
    if not actions:
        q = {"user_id": user["id"], "status": {"$in": ["open", "pending"]},
             "account_id": str(acc["_id"])}
        open_positions = await db.trades.find(q).to_list(length=200)
        snap = await build_snapshot(db, account=acc, open_positions=open_positions)
        actions = snap.get("actions") or []

    if not actions:
        return {"closed": 0, "skipped": 0, "actions": 0,
                "message": "Nothing to deleverage — portfolio within all limits."}

    result = await execute_deleveraging_actions(db, user_id=user["id"], actions=actions)
    return {**result, "account_id": str(acc["_id"])}
