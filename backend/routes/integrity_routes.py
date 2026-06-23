from fastapi import APIRouter, Depends
from auth import get_current_user
from integrity import snapshot

router = APIRouter(prefix="/integrity", tags=["integrity"])


@router.get("/snapshot")
async def integrity_snapshot(user=Depends(get_current_user)):
    """Read-only broker↔DB drift snapshot. Used by the Dashboard widget."""
    return await snapshot(user["id"])
