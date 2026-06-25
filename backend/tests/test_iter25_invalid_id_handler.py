"""Iter25 — global InvalidId exception handler (belt-and-suspenders).

Even if a future route forgets to use `route_utils.parse_object_id`, the
global handler catches the resulting `bson.errors.InvalidId` and returns a
clean 404 (instead of a 500). Prevents the iter22 P2.2 class of bug from
re-emerging when new routes are added.
"""
import os
import requests
import pytest
from bson.errors import InvalidId
from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:3000").rstrip("/")
API = f"{BASE_URL}/api"


def test_invalid_id_handler_is_registered():
    """server.app must have an exception handler for InvalidId returning 404."""
    import sys
    sys.path.insert(0, "/app/backend")
    from server import app
    handlers = app.exception_handlers
    assert InvalidId in handlers, "InvalidId handler not registered on app"


def test_invalid_id_handler_returns_404_for_unprotected_route():
    """Synthetic FastAPI app that mounts our handler and a route that calls
    ObjectId() WITHOUT try/except — verify the handler converts the resulting
    InvalidId to a clean 404 JSON response (the iter22 P2.2 class of bug)."""
    from bson import ObjectId
    from server import _invalid_id_handler

    app = FastAPI()
    app.add_exception_handler(InvalidId, _invalid_id_handler)

    @app.get("/probe/{x}")
    def probe(x: str):
        # Intentionally raw — mimics a route that forgot to use parse_object_id
        return {"oid": str(ObjectId(x))}

    client = TestClient(app)
    r = client.get("/probe/not-a-hex-string")
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("detail") == "Resource not found"


def test_invalid_id_handler_does_not_affect_valid_oid():
    """Sanity: a valid hex passes through normally."""
    from bson import ObjectId
    from server import _invalid_id_handler

    app = FastAPI()
    app.add_exception_handler(InvalidId, _invalid_id_handler)

    @app.get("/probe/{x}")
    def probe(x: str):
        return {"oid": str(ObjectId(x))}

    client = TestClient(app)
    r = client.get("/probe/6a3ad0ef17f40ac1dd3eb3ef")
    assert r.status_code == 200
    assert r.json()["oid"] == "6a3ad0ef17f40ac1dd3eb3ef"
