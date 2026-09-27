"""iter-108 · Stacked ML Ensemble API — candidate/production are SEPARATE (round 12 P1-01)."""
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/ml", tags=["ml"])


def _uid(user) -> str:
    return str(user.get("id") or user.get("_id"))


def _with_manifest(state: dict) -> dict:
    from model_manifest import public_summary
    return {**state, "manifest": public_summary()}


@router.get("/ensemble")
async def get_ensemble_status(user=Depends(get_current_user)):
    from ml_ensemble import get_meta, public_state
    return _with_manifest(public_state(await get_meta(get_db(), _uid(user))))


@router.get("/learning-pipeline")
async def learning_pipeline_status(user=Depends(get_current_user)):
    """Phase 5 — staged continuous-learning pipeline state: freeze guard,
    recent gated retrain runs, shadow-lab queue, rollback versions."""
    from learning_pipeline import pipeline_status
    return await pipeline_status(get_db(), _uid(user))


@router.post("/train")
async def retrain_ensemble(user=Depends(get_current_user)):
    """Training produces a CANDIDATE awaiting two-admin approval — never a promotion."""
    from ml_ensemble import train_ensemble
    return _with_manifest(await train_ensemble(get_db(), _uid(user)))


async def _admin_step_up(db, user, request, target_user_id: str) -> str:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from step_up import require_step_up
    await require_step_up(db, user, request, "model_promotion")
    return target_user_id or _uid(user)


@router.post("/candidate/approve")
async def approve_candidate_ep(payload: dict, request: Request, user=Depends(get_current_user)):
    """Authenticated approval (audit-chained) of the exact candidate digest. Two distinct admins required."""
    from ml_ensemble import approve_candidate
    db = get_db()
    target = await _admin_step_up(db, user, request, str(payload.get("user_id") or ""))
    return await approve_candidate(db, target, user.get("email", ""), str(payload.get("note") or ""))


@router.post("/candidate/promote")
async def promote_candidate_ep(payload: dict, request: Request, user=Depends(get_current_user)):
    """Atomic activation: signed manifest + two authenticated approvals + build binding + exact bytes."""
    from ml_ensemble import promote_candidate
    db = get_db()
    target = await _admin_step_up(db, user, request, str(payload.get("user_id") or ""))
    return _with_manifest(await promote_candidate(db, target, user.get("email", "")))
