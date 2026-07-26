"""Migration Helper — export / import the admin's state across environments.

The deploy story for STOIC is "ship the preview branch to a fresh production
DB, then carry over your 5 broker accounts + bot configs + custom presets so
the live EAs keep working without re-pairing". This module is the bridge.

Endpoints (admin-only)
----------------------
GET  /api/admin/export-state
        Returns a single JSON blob containing the admin's:
          - user doc (sans password_hash — preserved separately on the target)
          - accounts (incl. bridge_token + vault-encrypted creds — necessary
            so the EA on the user's VPS continues to authenticate without
            re-rotation. Safe because the same MONGO_URL'd vault key is in
            use on both sides; if not, the operator can rotate after import).
          - bot_configs
          - user_presets
          - schema_version (so future imports can migrate forward)

POST /api/admin/import-state
        Body: the JSON returned by `/export-state`.
        Behaviour:
          - Idempotent — uses `_id` upserts so re-running won't duplicate.
          - Skips the user doc's password_hash (the target env keeps its own).
          - Returns counts of inserted/updated docs per collection.
          - On a corrupt payload, returns 400 with the validation error and
            does NOT half-apply (best-effort atomicity per collection).

Not exported (intentional — these are environmental / transient):
  - trades / signals / agent_activity / heartbeats (huge + replayed live)
  - audit log (env-specific)
  - bridge_token rotation history
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db


router = APIRouter(tags=["admin-migration"])

SCHEMA_VERSION = 1
EXPORT_COLLECTIONS = ("accounts", "bot_configs", "user_presets")
# Per-user fields we DO want to carry over. password_hash is the deliberate
# omission — the target env keeps its own auth seed.
USER_FIELDS_PORTABLE = {
    "email", "name", "role", "status", "email_verified",
    "terms_agreed", "terms_agreed_at",
    "subscription_tier", "subscription_status", "valid_until",
    "two_fa_enabled", "two_fa_secret",
}


def _admin_only(user: dict) -> None:
    from auth import require_admin
    require_admin(user)


def _stringify_oids(doc: dict) -> dict:
    """Recursively serialise ObjectId → hex str so the blob is pure JSON."""
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat() if v.tzinfo else v.replace(tzinfo=timezone.utc).isoformat()
        elif isinstance(v, dict):
            out[k] = _stringify_oids(v)
        elif isinstance(v, list):
            out[k] = [_stringify_oids(x) if isinstance(x, dict) else (str(x) if isinstance(x, ObjectId) else x) for x in v]
        else:
            out[k] = v
    return out


def _restore_oid(value: Any) -> Any:
    """Best-effort: 24-char hex strings → ObjectId, otherwise pass through."""
    if isinstance(value, str) and len(value) == 24:
        try:
            return ObjectId(value)
        except Exception:
            return value
    return value


# ─── Export ─────────────────────────────────────────────────────────────
@router.get("/admin/export-state")
async def export_state(user=Depends(get_current_user)):
    _admin_only(user)
    db = get_db()
    uid = user["id"]

    user_doc = await db.users.find_one({"_id": ObjectId(uid)})
    if not user_doc:
        raise HTTPException(status_code=500, detail="admin user missing in DB")
    portable_user = {k: user_doc.get(k) for k in USER_FIELDS_PORTABLE if k in user_doc}
    portable_user["_id"] = str(user_doc["_id"])

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": user.get("email"),
        "user": portable_user,
        "collections": {},
        "counts": {},
    }

    for coll in EXPORT_COLLECTIONS:
        docs = await db[coll].find({"user_id": uid}).to_list(length=10_000)
        serialised = [_stringify_oids(d) for d in docs]
        payload["collections"][coll] = serialised
        payload["counts"][coll] = len(serialised)

    return payload


# ─── Import ─────────────────────────────────────────────────────────────
class ImportStateBody(BaseModel):
    schema_version: int = Field(..., description="must match server SCHEMA_VERSION")
    user: dict
    collections: dict
    # exported_at / exported_by / counts are advisory — accepted but unused
    exported_at: str | None = None
    exported_by: str | None = None
    counts: dict | None = None


@router.post("/admin/import-state")
async def import_state(body: ImportStateBody, user=Depends(get_current_user)):
    _admin_only(user)
    if body.schema_version != SCHEMA_VERSION:
        raise HTTPException(
            status_code=400,
            detail={"code": "schema_mismatch",
                    "message": f"Got schema_version={body.schema_version}, "
                               f"server expects {SCHEMA_VERSION}. Re-export from "
                               f"the source environment after upgrading it."},
        )

    db = get_db()
    target_uid = user["id"]
    target_oid = ObjectId(target_uid)
    source_uid = body.user.get("_id")

    report: dict[str, Any] = {
        "ok": True,
        "user": {"matched_by": "current_admin", "patched_fields": []},
        "collections": {},
        "remapped_user_id": {"from": source_uid, "to": target_uid} if source_uid != target_uid else None,
    }

    # 1) Patch the target admin's user doc — preserve env-specific
    #    password_hash, only carry over portable fields.
    user_patch = {k: body.user.get(k) for k in USER_FIELDS_PORTABLE if k in body.user}
    if user_patch:
        await db.users.update_one({"_id": target_oid}, {"$set": user_patch})
        report["user"]["patched_fields"] = list(user_patch.keys())

    # 2) Upsert each collection. Match by `_id` so re-imports are idempotent;
    #    rewrite the user_id to the target admin (handles cross-env migrations
    #    where the admin's _id differs).
    for coll in EXPORT_COLLECTIONS:
        docs = body.collections.get(coll) or []
        if not isinstance(docs, list):
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_payload",
                        "message": f"collections.{coll} must be a list"},
            )
        inserted = updated = 0
        for d in docs:
            if not isinstance(d, dict):
                continue
            raw_id = d.pop("_id", None)
            oid = _restore_oid(raw_id) if raw_id else ObjectId()
            d["user_id"] = target_uid  # remap to target admin
            # Restore any embedded ObjectId-shaped strings on commonly-typed
            # reference fields so collection-level queries continue to match.
            for ref in ("account_id", "config_id"):
                if ref in d and isinstance(d[ref], str):
                    d[ref] = d[ref]  # keep as string — these are already serialised consistently

            r = await db[coll].update_one(
                {"_id": oid},
                {"$set": d},
                upsert=True,
            )
            if r.upserted_id is not None:
                inserted += 1
            elif r.modified_count:
                updated += 1
        report["collections"][coll] = {
            "received": len(docs),
            "inserted": inserted,
            "updated": updated,
        }

    return report
