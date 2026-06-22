"""WebSocket connection manager — broadcasts live events to authenticated users.

Auth via the same `access_token` cookie used by REST endpoints. Each user
gets their own channel; the bot runner & API routes call `broadcast(user_id, event)`
to push real-time updates to that user's connected clients.
"""
import asyncio
import json
import logging
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger("ws-manager")


class WSManager:
    def __init__(self):
        self._connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(user_id, set()).add(ws)

    async def disconnect(self, user_id: str, ws: WebSocket):
        async with self._lock:
            conns = self._connections.get(user_id)
            if conns and ws in conns:
                conns.discard(ws)
                if not conns:
                    self._connections.pop(user_id, None)

    async def broadcast(self, user_id: str, event_type: str, payload: dict):
        """Send `{type, payload}` to all of one user's connected clients."""
        async with self._lock:
            conns = list(self._connections.get(user_id, set()))
        if not conns:
            return
        msg = json.dumps({"type": event_type, "payload": payload}, default=str)
        dead = []
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception as e:
                logger.debug("ws send failed: %s", e)
                dead.append(ws)
        if dead:
            async with self._lock:
                bucket = self._connections.get(user_id)
                if bucket:
                    for ws in dead:
                        bucket.discard(ws)


# Singleton
manager = WSManager()
