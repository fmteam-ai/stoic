"""Phase G · Trace observability endpoint — one view per trade."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/trace", tags=["trace"])


@router.get("/{trace_id}")
async def get_trace(trace_id: str, user=Depends(get_current_user)):
    """trace_id = decision_id or trade_id. Answers 'why did this trade
    happen?' from the durable projections in a single payload."""
    db = get_db()
    import trade_trace as trace_mod
    out = await trace_mod.assemble_trace(db, trace_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    uid = str(user.get("id") or user.get("_id"))
    if out.get("user_id") and str(out["user_id"]) != uid \
            and user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Trace not found")
    return out
