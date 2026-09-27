"""Broker statement reconciliation ledger API (audit round 13 P2-05)."""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("/statements")
async def list_ledger(account_id: str | None = None, user=Depends(get_current_user)):
    from broker_statement_ledger import ledger_rows, ledger_gate, RETURN_FORMULA_VERSION
    db = get_db()
    return {"rows": await ledger_rows(db, user["id"], account_id), "gate_reasons": await ledger_gate(db, user["id"]),
            "return_formula_version": RETURN_FORMULA_VERSION}


@router.post("/statements")
async def import_statement(payload: dict, request: Request, user=Depends(get_current_user)):
    """Import a SIGNED broker statement and reconcile it to the cent (admin + step-up MFA)."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = get_db()
    from step_up import require_step_up
    await require_step_up(db, user, request, "release_trust")
    from broker_statement_ledger import reconcile
    st = payload.get("statement")
    if not isinstance(st, dict):
        raise HTTPException(status_code=400, detail={"code": "statement_rejected", "problems": ["statement object required"]})
    target = str(payload.get("user_id") or user["id"])
    row = await reconcile(db, target, st, str(payload.get("signature_hex") or ""), user.get("email", ""))
    row["id"] = row.pop("_id")
    row.pop("signature_hex", None)
    return row
