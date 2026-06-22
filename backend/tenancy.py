"""Multi-tenancy helpers.

Use these instead of raw `_id` lookups whenever a resource is user-owned.
They make every access explicit and impossible to skip the user_id filter.
"""
from fastapi import HTTPException
from bson import ObjectId, errors as bson_errors


def _safe_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (bson_errors.InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid resource id")


async def get_owned_or_404(collection, doc_id: str, user_id: str) -> dict:
    """Fetch a document by id strictly scoped to the calling user.

    Raises 404 if either the id is bad, the doc doesn't exist, or it belongs
    to a different user. Eliminates the class of cross-tenant ID-guessing bugs.
    """
    oid = _safe_object_id(doc_id)
    doc = await collection.find_one({"_id": oid, "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Resource not found")
    return doc


async def update_owned_or_404(collection, doc_id: str, user_id: str, update: dict) -> int:
    oid = _safe_object_id(doc_id)
    result = await collection.update_one(
        {"_id": oid, "user_id": user_id},
        {"$set": update},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Resource not found")
    return result.modified_count


async def delete_owned_or_404(collection, doc_id: str, user_id: str) -> None:
    oid = _safe_object_id(doc_id)
    result = await collection.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Resource not found")
