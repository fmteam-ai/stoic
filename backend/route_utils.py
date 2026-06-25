"""Shared parsers for FastAPI routes — avoid 500s on malformed input."""
from fastapi import HTTPException
from bson import ObjectId
from bson.errors import InvalidId


def parse_object_id(value: str, resource: str = "Resource") -> ObjectId:
    """Convert a path/body string into a Mongo ObjectId or raise 404.

    Catches both InvalidId and any TypeError / value error from bson.
    Returns a clean 404 (resource shape implies non-existence) instead of
    leaking a 500 to the client.
    """
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=404, detail=f"{resource} not found")
    try:
        return ObjectId(value)
    except (InvalidId, TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"{resource} not found")
