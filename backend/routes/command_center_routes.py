"""Command Center API — /api/command-center (admin-only aggregation +
hash-chained evidence report export)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/command-center", tags=["command-center"])


def _require_admin(user):
    from auth import require_admin
    require_admin(user)  # role + mandatory TOTP MFA (SEC-001)


@router.get("/status")
async def command_center_status(user=Depends(get_current_user)):
    _require_admin(user)
    from command_center import status
    return await status(get_db())


@router.get("/evidence-export")
async def evidence_export(user=Depends(get_current_user)):
    _require_admin(user)
    from command_center import evidence_report
    report = await evidence_report(get_db(),
                                   user.get("email") or user["id"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        content=report,
        headers={"Cache-Control": "no-store",
                 "Content-Disposition":
                 f'attachment; filename="stoic-evidence-{stamp}.json"'})
