"""Certification Center API (iter-154).

  GET  /api/certification/center?account_id=   — six-pillar scored view + issued certs (owner/admin)
  POST /api/certification/center/issue         — issue a public hash-chained certificate
  POST /api/certification/center/revoke        — revoke one (owner/admin)
  GET  /api/public/certificate/{cert_id}       — PUBLIC verification endpoint (no auth)
"""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(tags=["certification-center"])


async def _owned_account(db, user, account_id: str) -> dict:
    from route_utils import parse_object_id
    q = {"_id": parse_object_id(account_id, "Account")}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    acc = await db.accounts.find_one(q)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return acc


@router.get("/certification/center")
async def certification_center(account_id: str,
                               user=Depends(get_current_user)):
    from certification_center import pillar_scores, public_view
    db = get_db()
    acc = await _owned_account(db, user, account_id)
    scores = await pillar_scores(db, acc.get("user_id") or user["id"], acc)
    certs = [public_view(c) async for c in db.public_certificates.find(
        {"account_id": str(acc["_id"])}).sort("seq", -1).limit(20)]
    return {**scores, "certificates": certs}


@router.post("/certification/center/issue")
async def issue_certificate(payload: dict,
                            user=Depends(get_current_user)):
    from certification_center import issue_public, public_view
    db = get_db()
    acc = await _owned_account(db, user,
                               str(payload.get("account_id") or ""))
    owner = user
    if user.get("role") == "admin" and acc.get("user_id") != user["id"]:
        owner = {"id": acc["user_id"], "role": "admin"}
    cert = await issue_public(db, owner, acc)
    return public_view(cert)


@router.post("/certification/center/revoke")
async def revoke_certificate(payload: dict,
                             user=Depends(get_current_user)):
    from certification_center import revoke_public
    cert_id = str(payload.get("cert_id") or "")
    if not cert_id:
        raise HTTPException(status_code=400, detail="cert_id required")
    out = await revoke_public(get_db(), cert_id, user,
                              str(payload.get("reason") or ""))
    if not out["ok"]:
        raise HTTPException(status_code=404,
                            detail="not found or already revoked")
    return out


@router.get("/public/certificate/{cert_id}")
async def public_certificate(cert_id: str):
    """PUBLIC — anyone with the link can verify tier, pillar scores,
    validity and the hash chain. No tenant data beyond the masked ref."""
    from certification_center import public_view, verify_certificate
    db = get_db()
    cert = await db.public_certificates.find_one({"cert_id": cert_id})
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    view = public_view(cert)
    view["hash_verified"] = verify_certificate(cert)
    return view
